from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from uuid import uuid4
import pytest
from app.errors import MemoryError
from app.repository import Repository,now
from app.service import MemoryService
from app.worker import process_one
from conftest import FakeEmbeddings,FakeModel


def note(repo,text='我喜欢卧室空调26度',**kwargs):
    return repo.remember({'text':text,**kwargs})[0]


def test_auth(client):
    assert client.get('/health',headers={'Authorization':''}).status_code==200
    assert client.get('/v1/memories',headers={'Authorization':''}).status_code==401
    assert client.get('/v1/memories',headers={'Authorization':'Bearer wrong'}).status_code==401


def test_save_get_and_validation(client):
    assert client.post('/v1/memories',json={'text':'   '}).status_code==422
    assert client.post('/v1/memories',json={'text':'x','valid_until':'2026-10-23T12:00:00'}).status_code==422
    result=client.post('/v1/memories',json={'text':'我喜欢咖啡'})
    assert result.status_code==201
    item=result.json()
    assert item['storage_status']=='durable' and item['index_status']=='pending'
    assert client.get('/v1/memories/'+item['id']).json()['text']=='我喜欢咖啡'
    assert client.get('/v1/memories').json()['total']==1


def test_idempotent_concurrent(repo):
    def save(_): return repo.remember({'text':'不吃香菜'},'stable-key')[0]['id']
    with ThreadPoolExecutor(max_workers=4) as pool: ids=list(pool.map(save,range(20)))
    assert len(set(ids))==1
    assert repo.listing()['total']==1
    with pytest.raises(MemoryError) as e: repo.remember({'text':'吃香菜'},'stable-key')
    assert e.value.status==409
    repo.forget(ids[0])
    with pytest.raises(MemoryError): repo.remember({'text':'不吃香菜'},'stable-key')


def test_commit_failure_rolls_back(repo,monkeypatch):
    def fail(*args): raise RuntimeError('simulated')
    monkeypatch.setattr(repo,'_enqueue',fail)
    with pytest.raises(RuntimeError): note(repo)
    assert repo.listing()['total']==0
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM events').fetchone()['n']==0


def test_new_connection_persistence(repo):
    saved=note(repo)
    other=Repository(repo.settings)
    try:
        other.migrate()
        assert other.get(saved['id'])['text']==saved['text']
    finally: other.close()


def test_keyword_available_before_embedding(repo):
    saved=note(repo,'我不吃香菜')
    assert repo.search('香菜')[0]['id']==saved['id']
    assert repo.search('香菜')[0]['methods']==['keyword']


def test_semantic_and_current_version(repo):
    saved=note(repo)
    assert process_one(repo,FakeEmbeddings())
    assert repo.get(saved['id'])['index_status']=='done'
    vector=FakeEmbeddings().embed(['卧室应该调到几度'])[0]
    assert repo.search('调温',vector)[0]['id']==saved['id']
    changed=repo.correct(saved['id'],1,{'text':'我喜欢空调28度'})
    assert changed['revision']==2
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM projections').fetchone()['n']==0
    assert process_one(repo,FakeEmbeddings())
    assert '28' in repo.search('空调',vector)[0]['text']
    assert len(repo.get(saved['id'],history=True)['history'])==2


def test_revision_conflict(repo):
    saved=note(repo)
    def update(text):
        try: return repo.correct(saved['id'],1,{'text':text})['revision']
        except MemoryError as e: return e.code
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(update,['26度','28度']))
    assert sorted(map(str,results))==['2','revision_conflict']


def test_old_job_cannot_overwrite_correction(repo):
    saved=note(repo)
    old=repo.claim_job()
    repo.correct(saved['id'],1,{'text':'更新后的28度'})
    assert not repo.finish_index(old,['旧内容'],FakeEmbeddings().embed(['旧内容']))
    assert repo.get(saved['id'])['text']=='更新后的28度'


def test_deleted_job_cannot_resurrect(repo):
    saved=note(repo)
    job=repo.claim_job()
    repo.forget(saved['id'])
    assert not repo.finish_index(job,['旧内容'],FakeEmbeddings().embed(['旧内容']))
    assert repo.search('空调')==[]


def test_delete_scrubs_all_versions_and_derived_history(repo):
    saved=note(repo)
    repo.correct(saved['id'],1,{'text':'私人内容28度'})
    process_one(repo,FakeEmbeddings())
    conv=uuid4()
    repo.save_answer(conv,'问题','私人内容的回答',[saved['id']],repo.epoch())
    repo.forget(saved['id'])
    assert repo.history(conv)==[]
    with repo.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM events WHERE content<>''").fetchone()
        assert not c.execute('SELECT 1 FROM projections').fetchone()
    assert '私人' not in repo.journal.path.read_text()
    with pytest.raises(MemoryError): repo.get(saved['id'],history=True)


def test_restore_replay_wipes_old_snapshot(repo):
    saved=note(repo,'删除之前的内容')
    repo.forget(saved['id'])
    # Simulate a pre-delete DB snapshot without changing the independent journal.
    with repo.pool.connection() as c:
        c.execute("UPDATE memories SET deleted_at=NULL,text='删除之前的内容',terms=to_tsvector('simple','内容') WHERE id=%s",(saved['id'],))
        c.execute('DELETE FROM deletion_ledger')
    repo.replay_deletions(required=True)
    assert repo.listing()['total']==0


def test_missing_or_truncated_journal_fails_closed(repo):
    repo.journal.path.unlink()
    with pytest.raises(MemoryError): repo.replay_deletions(required=True)
    repo.journal.path.write_text('{"broken":')
    with pytest.raises(MemoryError): repo.replay_deletions(required=True)


def test_journal_failure_does_not_ack_delete(repo,monkeypatch):
    saved=note(repo)
    def fail(*args): raise MemoryError('journal_unavailable','failed',503)
    monkeypatch.setattr(repo.journal,'append',fail)
    with pytest.raises(MemoryError): repo.forget(saved['id'])
    assert repo.get(saved['id'])['text']==saved['text']


def test_expired_future_and_subject_filters(repo):
    note(repo,'过去的温度',valid_from=now()-timedelta(days=2),valid_until=now()-timedelta(days=1))
    note(repo,'未来的温度',valid_from=now()+timedelta(days=1))
    current=note(repo,'妹妹的温度',subject='妹妹')
    assert [r['id'] for r in repo.search('温度')]==[current['id']]
    assert not repo.search('温度',subject='user')


def test_local_only_never_sent_even_through_history(repo,model):
    private=note(repo,'秘密香菜暗号',allow_cloud=False)
    public=note(repo,'喜欢香菜')
    service=MemoryService(repo,FakeEmbeddings(),model)
    response=service.chat('香菜的偏好是什么')
    sent=json.dumps(model.requests,ensure_ascii=False)
    assert '秘密香菜暗号' not in sent
    assert str(private['id']) not in sent
    assert str(public['id']) in sent
    repo.correct(public['id'],1,{'allow_cloud':False})
    assert repo.history(response['conversation_id'])==[]


@pytest.mark.parametrize('operation',['delete','correct','clear'])
def test_inflight_answer_is_rejected(repo,model,operation):
    saved=note(repo)
    conv=uuid4()
    if operation=='delete': model.callback=lambda:repo.forget(saved['id'])
    elif operation=='correct': model.callback=lambda:repo.correct(saved['id'],1,{'text':'改为28度'})
    else: model.callback=lambda:repo.clear_conversation(conv)
    service=MemoryService(repo,FakeEmbeddings(),model)
    with pytest.raises(MemoryError) as e: service.chat('空调几度',conv)
    assert e.value.code=='context_changed'
    assert repo.history(conv)==[]


def test_model_failure_does_not_break_storage(repo,model):
    model.error=MemoryError('model_timeout','timeout',504)
    saved=note(repo)
    with pytest.raises(MemoryError): MemoryService(repo,FakeEmbeddings(),model).chat('空调几度')
    assert repo.get(saved['id'])['text']==saved['text']
    assert repo.listing()['total']==1


def test_embedding_failure_falls_back_and_retries(repo,model):
    class Broken:
        def embed(self,*args,**kwargs): raise RuntimeError('offline')
    saved=note(repo)
    service=MemoryService(repo,Broken(),model)
    result=service.search('空调')
    assert result['mode']=='keyword' and result['items'][0]['id']==saved['id']
    process_one(repo,Broken())
    assert repo.get(saved['id'])['index_status']=='pending'
    repo.reindex(saved['id'])
    process_one(repo,FakeEmbeddings())
    assert repo.get(saved['id'])['index_status']=='done'


def test_cleared_conversation_can_be_reused_without_startup_erasing_new_messages(repo):
    conv=uuid4()
    repo.save_answer(conv,'旧问题','旧回答',[],repo.epoch())
    repo.clear_conversation(conv)
    repo.save_answer(conv,'新问题','新回答',[],repo.epoch())
    repo.replay_deletions(required=True)
    assert [r['content'] for r in repo.history(conv)]==['新问题','新回答']


def test_retention_is_replayed_on_restore(repo):
    conv=uuid4()
    repo.save_answer(conv,'旧聊天','旧回复',[],repo.epoch())
    with repo.pool.connection() as c:
        c.execute("UPDATE messages SET created_at=now()-interval '31 days'")
    repo.expire_history()
    with repo.pool.connection() as c:
        assert not c.execute('SELECT 1 FROM messages').fetchone()
    assert any(r['target_type']=='expiry' for r in repo.journal.records())


def test_api_correct_delete_chat(client):
    saved=client.post('/v1/memories',json={'text':'我喜欢咖啡'}).json()
    url='/v1/memories/'+saved['id']
    assert client.patch(url,json={'expected_revision':1,'text':'我不喝咖啡'}).status_code==200
    assert client.patch(url,json={'expected_revision':1,'text':'旧修改'}).status_code==409
    chat=client.post('/v1/chat',json={'text':'咖啡偏好'}).json()
    assert chat['sources'][0]['revision']==2
    assert client.get('/v1/conversations/'+chat['conversation_id']).json()['messages']
    assert client.delete(url).status_code==200
    assert client.get(url).status_code==404
    assert client.get('/v1/conversations/'+chat['conversation_id']).json()['messages']==[]
