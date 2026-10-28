import json
from datetime import timedelta
from uuid import uuid4
import pytest
from psycopg import sql
from app.automatic import MemoryAction, validate_decision
from app.errors import MemoryError
from reflection_storage_helpers import drop_reflection_schema
from app.repository import now
from app.service import MemoryService
from conftest import FakeEmbeddings


def action(quote='我平时不吃香菜', **changes):
    return {'operation':'create','quote':quote,'text':'用户平时不吃香菜','kind':'preference',
            'confidence':.99, 'durability_quote':quote, **changes}


def chat(repo, model, text='我平时不吃香菜', conversation=None):
    return MemoryService(repo,FakeEmbeddings(),model).chat(text,conversation)


def test_chat_automatically_commits_source_and_index_job(repo,model):
    model.decision={'actions':[action()]}
    result=chat(repo,model)
    assert result['memory_status']=='committed'
    mid=result['memory_changes'][0]['id']
    saved=repo.get(mid,history=True)
    assert saved['origin']=='automatic' and saved['kind']=='preference'
    assert saved['text']=='用户平时不吃香菜' and saved['index_status']=='pending'
    assert saved['source']['evidence']['quote']=='我平时不吃香菜'
    assert saved['source']['evidence']['conversation_id']==result['conversation_id']
    assert all(mid in row['source_ids'] for row in repo.history(result['conversation_id']))
    assert repo.search('香菜')[0]['id']==mid


def test_duplicate_auto_create_is_suppressed_in_transaction(repo,model):
    model.decision={'actions':[action()]}
    first=chat(repo,model)
    second=chat(repo,model)
    assert repo.listing()['total']==1
    assert second['memory_changes']==[] and second['memory_status']=='unchanged'
    assert first['memory_changes'][0]['id'] in repo.history(second['conversation_id'])[0]['source_ids']


def test_automatic_update_preserves_versions_and_removes_old_chat(repo,model):
    model.decision={'actions':[action()]}
    first=chat(repo,model);mid=first['memory_changes'][0]['id']
    model.decision={'actions':[action('我现在吃香菜了',operation='update',target_id=str(mid),
        expected_revision=1,text='用户现在吃香菜')]}
    result=chat(repo,model,'我现在吃香菜了',first['conversation_id'])
    assert result['memory_changes'][0]['operation']=='update'
    saved=repo.get(mid,history=True)
    assert saved['revision']==2 and len(saved['history'])==2
    assert [r['quote'] for r in saved['evidence_history']]==['我平时不吃香菜','我现在吃香菜了']
    assert [r['content'] for r in repo.history(first['conversation_id'])]==['我现在吃香菜了','根据提供的记忆回答。']
    # Response generation sees the replacement, not the previous statement.
    prompt=json.loads(model.requests[-1][-1]['content'])
    assert prompt['memory_evidence']==[]
    assert prompt['planned_memory_changes'][0]['text']=='用户现在吃香菜'


def test_natural_forgetting_scrubs_sources_and_replays(repo,model):
    model.decision={'actions':[action()]}
    result=chat(repo,model);mid=result['memory_changes'][0]['id']
    model.decision={'actions':[action('忘掉我的香菜偏好',operation='forget',target_id=str(mid),expected_revision=1)]}
    forgotten=chat(repo,model,'忘掉我的香菜偏好',result['conversation_id'])
    assert forgotten['memory_changes']==[{'operation':'forget','id':mid}]
    assert repo.history(result['conversation_id'])==[]
    with repo.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM memory_evidence WHERE quote<>''").fetchone()
        # A backup taken before forgetting must still be scrubbed on replay.
        c.execute("UPDATE memories SET deleted_at=NULL,text='旧偏好' WHERE id=%s",(mid,))
        c.execute("UPDATE memory_evidence SET quote='旧原话' WHERE memory_id=%s",(mid,))
    repo.replay_deletions(required=True)
    assert repo.listing()['total']==0
    with repo.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM memory_evidence WHERE quote<>''").fetchone()


def test_answer_failure_does_not_commit_proposed_memory(repo,model):
    model.decision={'actions':[action()]}
    model.error=MemoryError('model_timeout','synthetic timeout',504)
    with pytest.raises(MemoryError):chat(repo,model)
    assert repo.listing()['total']==0
    with repo.pool.connection() as c:
        assert not c.execute('SELECT 1 FROM memory_evidence').fetchone()
        assert not c.execute('SELECT 1 FROM messages').fetchone()


def test_batch_failure_rolls_back_memory_evidence_and_answer(repo,model,monkeypatch):
    model.decision={'actions':[action(),action(text='用户偏爱淡茶',quote='我偏爱淡茶')]}
    enqueue=repo._enqueue
    def fail_second(c,mid,revision):
        if c.execute('SELECT count(*) AS n FROM memories').fetchone()['n']==2:raise RuntimeError('simulated')
        enqueue(c,mid,revision)
    monkeypatch.setattr(repo,'_enqueue',fail_second)
    with pytest.raises(MemoryError):chat(repo,model,'我平时不吃香菜，我偏爱淡茶')
    assert repo.listing()['total']==0
    with repo.pool.connection() as c:
        for table in ['memory_evidence','messages','events','outbox']:
            assert c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']==0


@pytest.mark.parametrize('change',[
    {'quote':'用户根本没说过的原话'}, {'confidence':.5},
    {'operation':'update','target_id':str(uuid4()),'expected_revision':1},
    {'kind':'temporary'}, {'arbitrary_sql':'drop table memories'},
])
def test_invalid_proposal_never_reaches_answer_or_storage(repo,model,change):
    model.decision={'actions':[action(**change)]}
    with pytest.raises(MemoryError) as error:chat(repo,model)
    assert error.value.code=='memory_decision_invalid'
    assert repo.listing()['total']==0 and model.requests==[]


def test_private_memory_not_sent_to_decider(repo,model):
    private=repo.remember({'text':'私密香菜口令','allow_cloud':False})[0]
    chat(repo,model,'香菜偏好')
    payload=''.join(model.decision_requests)+json.dumps(model.requests,ensure_ascii=False)
    assert private['text'] not in payload and str(private['id']) not in payload


def test_disabled_automatic_memory_and_ambiguous_clarification(client,model):
    model.decision={'actions':[action()]}
    result=client.post('/v1/chat',json={'text':'我平时不吃香菜','auto_memory':False}).json()
    assert result['memory_status']=='disabled' and model.decision_requests==[]
    model.decision={'actions':[],'clarification':'是你还是妹妹的偏好？'}
    result=client.post('/v1/chat',json={'text':'她也不吃'}).json()
    assert result['memory_changes']==[] and result['memory_clarification']=='是你还是妹妹的偏好？'


def test_user_requests_no_record_leaves_no_turn_or_fact(repo,model):
    model.decision={'actions':[],'retain_turn':False}
    result=chat(repo,model,'这段不要记录：我今天临时去朋友家。')
    assert result['history_saved'] is False
    assert repo.history(result['conversation_id'])==[] and repo.listing()['total']==0


def test_nonretained_turn_cannot_sneak_in_permanent_fact():
    with pytest.raises(MemoryError):
        validate_decision({'actions':[action()],'retain_turn':False},'我平时不吃香菜',[])


def test_temporary_does_not_overwrite_permanent_preference(repo,model):
    base=repo.remember({'text':'用户平时喝咖啡'})[0]
    model.decision={'actions':[action('今晚不喝咖啡',text='用户今晚不喝咖啡',kind='temporary',
        valid_until=(now()+timedelta(hours=6)).isoformat())]}
    result=chat(repo,model,'今晚不喝咖啡')
    saved=repo.get(result['memory_changes'][0]['id'])
    assert saved['kind']=='temporary' and saved['valid_until']
    assert repo.get(base['id'])['revision']==1 and repo.listing()['total']==2
    with repo.pool.connection() as c:
        c.execute('UPDATE memories SET valid_from=%s,valid_until=%s WHERE id=%s',
                  (now()-timedelta(hours=2),now()-timedelta(hours=1),saved['id']))
    assert saved['id'] not in [m['id'] for m in repo.search('咖啡')]


def test_decision_cannot_reintroduce_forgotten_content_in_same_batch():
    mid=uuid4()
    with pytest.raises(MemoryError):
        validate_decision({'actions':[action(operation='forget',target_id=str(mid),expected_revision=1),action()]},
                          '我平时不吃香菜',[{'id':mid,'revision':1}])


def test_automatic_write_rejects_concurrent_delete(repo,model):
    saved=repo.remember({'text':'用户不吃香菜'})[0]
    model.decision={'actions':[action(operation='update',target_id=str(saved['id']),expected_revision=1)]}
    model.callback=lambda:repo.forget(saved['id'])
    with pytest.raises(MemoryError) as error:chat(repo,model)
    assert error.value.code=='context_changed' and repo.listing()['total']==0


def test_upgrade_existing_v1_data_is_repeatable(repo):
    saved=repo.remember({'text':'迁移前的原话'})[0]
    with repo.pool.connection() as c:
        drop_reflection_schema(c)
        c.execute('ALTER TABLE messages DROP COLUMN experience_id')
        for table in ('episode_details','episode_cues','episode_evidence','episode_versions',
                      'episode_associations','episode_rehearsals','episode_chunks','episode_jobs','episodes'):
            c.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table)))
        c.execute('ALTER TABLE model_calls DROP COLUMN phase,DROP COLUMN source_refs,DROP COLUMN estimated_input_tokens,DROP COLUMN policy_version,DROP COLUMN estimator_version')
        for table in ('context_links','self_observations','experience_jobs','experience_chunks','experiences'):
            c.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table)))
        c.execute('DROP TABLE memory_evidence')
        c.execute('ALTER TABLE memories DROP COLUMN origin, DROP COLUMN kind')
        c.execute('DELETE FROM schema_migrations WHERE version>=2')
    repo.migrate();repo.migrate()
    assert repo.get(saved['id'])['text']=='迁移前的原话'
    assert repo.get(saved['id'])['origin']=='manual'
