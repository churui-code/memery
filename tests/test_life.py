import json
from datetime import timedelta
from uuid import uuid4
import pytest
from psycopg import sql
from app.automatic import validate_decision
from app.errors import MemoryError
from app.life import observation_view
from app.repository import Repository,now
from app.service import MemoryService
from app.worker import process_experience
from conftest import FakeEmbeddings


TEXT='我们昨天一起讨论了那部电影，你听完我的解释，改变了对结局的看法。'


def update(domain='belief',key='电影结局',text='我开始把结局理解为告别，而非失败',quote=TEXT,**changes):
    return {'domain':domain,'key':key,'text':text,'reason':'这次解释使我重新审视了之前的理解',
            'quote':quote,'confidence':.8,**changes}


def talk(repo,model,text=TEXT,conversation=None,auto=True):
    return MemoryService(repo,FakeEmbeddings(),model).chat(text,conversation,auto)


def count(repo,table):
    with repo.pool.connection() as c:
        return c.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']


def test_unextracted_experience_survives_chat_retention_and_reopen(repo,model):
    result=talk(repo,model,'合成经历：在公园说了我爱你，并单膝跪下亲吻手、戴上戒指。')
    assert repo.listing()['total']==0
    with repo.pool.connection() as c:
        c.execute("UPDATE messages SET created_at=now()-interval '40 days'")
        c.execute("UPDATE experiences SET recorded_at=now()-interval '40 days'")
    repo.expire_history()
    assert repo.history(result['conversation_id'])==[]
    reopened=Repository(repo.settings)
    try:
        reopened.migrate();reopened.replay_deletions(required=True)
        saved=reopened.search_experiences('戒指')[0]
        assert saved['id']==result['experience_id']
        assert '我爱你' in saved['user_text'] and saved['assistant_text']=='根据提供的记忆回答。'
    finally:reopened.close()


def test_late_anchor_in_long_experience_is_searchable_before_indexing(repo,model):
    text='合成经历：'+''.join(f'路过测试地点{i}。' for i in range(600))+'最后说出紫藤戒指这个暗号。'
    result=talk(repo,model,text)
    assert repo.search_experiences('紫藤戒指')[0]['id']==result['experience_id']
    assert repo.experience(result['experience_id'])['user_text']==text


def test_hypothetical_scene_has_verbatim_anchors_without_personal_fact(repo,model):
    text='假如我们去了公园，我对你说我爱你，然后单膝跪地戴上戒指。'
    model.decision={'actions':[],'experience':{'title':'讨论一个公园场景','summary':'讨论如何保留重要细节',
        'context_type':'hypothetical','anchors':[{'quote':'我爱你，然后单膝跪地戴上戒指','meaning':'场景中的关键言语和动作'}]}}
    result=talk(repo,model,text)
    saved=repo.experience(result['experience_id'])
    assert saved['context_type']=='hypothetical' and saved['anchors'][0]['quote'] in text
    assert repo.listing()['total']==0


def test_internal_states_evolve_with_sources_and_enter_next_session(repo,model):
    model.decision={'actions':[],'self_updates':[update()]}
    first=talk(repo,model)
    model.decision={'actions':[],'self_updates':[update(text='我保留告别的理解，也开始看到希望')]}
    second=talk(repo,model)
    assert first['conversation_id']!=second['conversation_id']
    assert repo.listing()['total']==0
    states=repo.self_state()
    assert len(states)==1 and states[0]['revision']==2
    history=repo.self_history('belief','电影结局')
    assert len(history)==2 and history[1]['text']=='我开始把结局理解为告别，而非失败'
    assert {'source_type':'experience','source_id':first['experience_id']} in history[1]['sources']
    prompt=json.loads(model.requests[-1][-1]['content'])
    assert prompt['self_state'][0]['revision']==1
    assert prompt['planned_self_updates'][0]['text']==states[0]['text']
    assert any(detail['quote']==TEXT for item in prompt['experience_evidence'] for detail in item['details'])


def test_emotion_fades_without_rewriting_its_cause(repo,model):
    model.decision={'self_updates':[update(domain='emotion',key='当下心情',text='有些失落',intensity=.8,half_life_hours=2)]}
    talk(repo,model)
    row=repo.self_state()[0]
    later=observation_view(row,at=row['created_at']+timedelta(hours=4))
    assert later['current_intensity']==.2
    assert later['intensity']==.8 and later['reason']==row['reason']
    assert repo.self_history()[0]['intensity']==.8


def test_old_relevant_self_understanding_is_selected_over_unrelated_recent_states(repo,model):
    model.decision={'self_updates':[update(key='紫藤',text='我认为紫藤象征值得兑现的承诺')]}
    talk(repo,model)
    for i in range(7):
        model.decision={'self_updates':[update(key=f'其他观点{i}',text=f'关于别的话题的观点{i}')]}
        talk(repo,model)
    assert all(row['state_key']!='紫藤' for row in repo.self_state())
    model.decision={'actions':[]}
    talk(repo,model,'你对紫藤的看法是什么')
    prompt=json.loads(model.requests[-1][-1]['content'])
    assert any(row['state_key']=='紫藤' for row in prompt['self_state'])


@pytest.mark.parametrize('bad',[
    {'experience':{'title':'错误','summary':'错误','anchors':[{'quote':'从未说过的原话','meaning':'伪造'}]}},
    {'self_updates':[update(quote='从未说过的依据')]},
    {'self_updates':[update(source_experience_ids=[str(uuid4())])]},
    {'self_updates':[update(domain='emotion')]},
    {'self_updates':[update(),update()]},
    {'self_updates':[update()],'retain_turn':False},
])
def test_unsupported_development_is_rejected_atomically(repo,model,bad):
    model.decision=bad
    with pytest.raises(MemoryError):talk(repo,model)
    assert count(repo,'experiences')==count(repo,'self_observations')==0
    assert model.requests==[]


def test_answer_failure_rolls_back_experience_and_self(repo,model):
    model.decision={'self_updates':[update()]}
    model.error=MemoryError('model_timeout','synthetic timeout',504)
    with pytest.raises(MemoryError):talk(repo,model)
    for table in ('experiences','experience_jobs','self_observations','context_links','messages'):
        assert count(repo,table)==0


def test_self_write_failure_rolls_back_whole_turn(repo,model,monkeypatch):
    model.decision={'self_updates':[update()]}
    def fail(*args):raise RuntimeError('synthetic state failure')
    monkeypatch.setattr(repo,'_save_self_updates',fail)
    with pytest.raises(MemoryError):talk(repo,model)
    assert count(repo,'experiences')==count(repo,'experience_jobs')==count(repo,'messages')==0


def test_forgetting_source_scrubs_recursive_life_and_self_and_replays(repo,model):
    model.decision={'self_updates':[update()]}
    first=talk(repo,model)
    model.decision={'self_updates':[update(text='我的后续理解')]}
    second=talk(repo,model)
    process_experience(repo,FakeEmbeddings())
    repo.forget_experience(first['experience_id'])
    assert repo.search_experiences('电影')==[] and repo.self_state()==[]
    assert repo.history(second['conversation_id'])==[]
    for table in ('experiences','self_observations'):
        with repo.pool.connection() as c:
            rows=c.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table))).fetchall()
            assert all(row['deleted_at'] for row in rows)
    assert count(repo,'experience_chunks')==0
    # Simulate restored old content, then replay the independent intent.
    with repo.pool.connection() as c:
        c.execute("UPDATE experiences SET user_text='恢复的旧内容',deleted_at=NULL")
        c.execute("UPDATE self_observations SET text='恢复的旧认识',deleted_at=NULL,invalidated_at=NULL")
    repo.replay_deletions(required=True)
    assert repo.self_state()==[]
    with repo.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM experiences WHERE user_text<>''").fetchone()
        assert not c.execute("SELECT 1 FROM self_observations WHERE text<>''").fetchone()


def test_correction_preserves_archive_locally_but_revokes_old_inferences(repo,model):
    memory=repo.remember({'text':'用户把电影结局理解为失败'})[0]
    model.decision={'self_updates':[update()]}
    result=talk(repo,model)
    repo.correct(memory['id'],1,{'text':'此前电影结局记录有误'})
    saved=repo.experience(result['experience_id'])
    assert saved['user_text']==TEXT and saved['context_stale']
    assert repo.self_state()==[]
    assert repo.search_experiences('电影')==[]
    assert repo.search_experiences('电影',include_stale=True)


def test_cloud_revocation_cascades_to_experiences_and_internal_state(repo,model):
    memory=repo.remember({'text':'合成电影私密资料'})[0]
    model.decision={'self_updates':[update()]}
    first=talk(repo,model)
    repo.correct(memory['id'],1,{'allow_cloud':False})
    assert repo.experience(first['experience_id'])['allow_cloud'] is False
    assert repo.self_state()==[]
    model.decision={'actions':[]}
    model.requests.clear();model.decision_requests.clear()
    talk(repo,model,'合成电影私密资料')
    payload=''.join(model.decision_requests)+json.dumps(model.requests,ensure_ascii=False)
    assert str(first['experience_id']) not in payload
    assert TEXT not in payload


def test_indirect_private_content_cannot_leak_back_through_recent_history(repo,model):
    memory=repo.remember({'text':'合成电影私密资料'})[0]
    model.decision={'self_updates':[update()]}
    talk(repo,model)
    model.decision={'actions':[]}
    def echo(system,messages):
        model.requests.append(messages)
        return {'text':'合成电影私密资料','usage':{}}
    model.chat=echo
    second=talk(repo,model,'随便聊聊')
    with repo.pool.connection() as c:
        rows=c.execute('SELECT source_ids FROM messages WHERE conversation_id=%s',(second['conversation_id'],)).fetchall()
        assert all(not row['source_ids'] for row in rows)
    repo.correct(memory['id'],1,{'allow_cloud':False})
    model.requests.clear();model.decision_requests.clear()
    talk(repo,model,'继续吧',second['conversation_id'])
    payload=''.join(model.decision_requests)+json.dumps(model.requests,ensure_ascii=False)
    assert '合成电影私密资料' not in payload


def test_clear_conversation_deletes_archive_and_self_but_keeps_new_turn(repo,model):
    model.decision={'self_updates':[update()]}
    first=talk(repo,model)
    repo.clear_conversation(first['conversation_id'])
    assert repo.self_state()==[]
    with pytest.raises(MemoryError):repo.experience(first['experience_id'])
    model.decision={'actions':[]}
    second=talk(repo,model,'清理后新的独立经历',first['conversation_id'])
    repo.replay_deletions(required=True)
    assert repo.experience(second['experience_id'])['user_text']=='清理后新的独立经历'


def test_deleted_latest_self_state_does_not_resurrect_previous_revision(repo,model):
    model.decision={'self_updates':[update()]}
    talk(repo,model);talk(repo,model)
    latest=repo.self_state()[0]
    repo.forget_observation(latest['id'])
    assert repo.self_state()==[]
    assert len(repo.self_history())==1


def test_event_time_and_conversation_time_remain_distinct(repo,model):
    text='2020年1月2日，我在公园看到了紫藤花。'
    model.decision={'experience':{'title':'听说以前的公园经历','summary':'用户讲述往事','context_type':'reported',
        'occurred_at':'2020-01-02T12:00:00+08:00','time_quote':'2020年1月2日'}}
    result=talk(repo,model,text)
    start=now().replace(year=2020,month=1,day=1)
    end=start+timedelta(days=3)
    assert repo.search_experiences('',after=start,before=end,time_basis='occurred_at')[0]['id']==result['experience_id']
    assert repo.search_experiences('',after=start,before=end,time_basis='recorded_at')==[]


def test_recall_plan_expands_search_by_time_before_answer(repo,model):
    old=talk(repo,model,'很早以前的测试暗号是紫藤。')
    with repo.pool.connection() as c:
        c.execute("UPDATE experiences SET recorded_at=now()-interval '300 days'")
    model.decision={'recall':[{'query':'','after':(now()-timedelta(days=301)).isoformat(),
                              'before':(now()-timedelta(days=299)).isoformat()}]}
    result=talk(repo,model,'找一下很早以前的暗号')
    prompt=json.loads(model.requests[-1][-1]['content'])
    assert str(old['experience_id']) in [eid for row in prompt['experience_evidence'] for eid in row['source_experience_ids']]
    assert '紫藤' in str(prompt)


def test_no_record_and_disabled_auto_have_distinct_archive_behavior(repo,model):
    model.decision={'retain_turn':False}
    assert talk(repo,model)['experience_id'] is None
    assert count(repo,'experiences')==0
    model.decision={'self_updates':[update()]}
    result=talk(repo,model,auto=False)
    assert result['experience_id'] and not result['self_changes']
    assert repo.self_state()==[]


def test_more_than_five_facts_are_not_lost_to_old_cap():
    utterance=''.join(f'测试事实{i}。' for i in range(9))
    actions=[{'operation':'create','quote':f'测试事实{i}','text':f'测试事实{i}','confidence':.99} for i in range(9)]
    assert len(validate_decision({'actions':actions},utterance,[]).actions)==9


def test_minimax_nested_array_representation_keeps_strict_quote_validation():
    raw={'actions':{'item':[]},'experience':{'title':'讨论','summary':'讨论电影',
        'participants':{'item':['用户','AI']},'anchors':{'item':[{'quote':'电影','meaning':'讨论主题'}]}}}
    result=validate_decision(raw,TEXT,[])
    assert result.experience.participants==['用户','AI']
    assert result.experience.anchors[0].quote=='电影'
    raw['experience']['anchors']['item'][0]['quote']='伪造的原话'
    with pytest.raises(MemoryError):validate_decision(raw,TEXT,[])


def test_late_index_worker_cannot_resurrect_deleted_experience(repo,model):
    result=talk(repo,model)
    job=repo.claim_experience_job()
    repo.forget_experience(result['experience_id'])
    repo.finish_experience_index(job,['旧内容'],FakeEmbeddings().embed(['旧内容']))
    assert count(repo,'experience_chunks')==0


def test_narrative_retrieval_has_its_own_candidate_similarity_threshold(repo,model):
    result=talk(repo,model)
    process_experience(repo,FakeEmbeddings())
    vector=[0.0]*512
    vector[3]=.45
    vector[4]=(1-.45**2)**.5
    found=repo.search_experiences('无词面匹配的查询',vector=vector)
    assert found[0]['id']==result['experience_id'] and found[0]['methods']==['semantic']


def test_real_preference_evolution_keeps_old_experience_recallable(repo,model):
    memory=repo.remember({'text':'用户以前喜欢这部电影'})[0]
    first=talk(repo,model,'我以前喜欢这部电影。')
    quote='我现在不喜欢这部电影了，但以前确实喜欢。'
    model.decision={'actions':[{'operation':'update','target_id':str(memory['id']),
        'expected_revision':1,'quote':quote,'text':'用户现在不喜欢这部电影','confidence':.99,
        'kind':'preference','change_mode':'evolution'}]}
    talk(repo,model,quote)
    assert not repo.experience(first['experience_id'])['context_stale']
    assert any(row['id']==first['experience_id'] for row in repo.search_experiences('电影'))
    assert repo.get(memory['id'])['text']=='用户现在不喜欢这部电影'


def test_history_only_dependency_is_removed_when_original_experience_is_deleted(repo,model):
    service=MemoryService(repo,FakeEmbeddings(),model)
    service._initial_experiences=lambda text:[]
    first=service.chat('第一个合成话题')
    second=service.chat('继续刚才的讨论',first['conversation_id'])
    repo.forget_experience(first['experience_id'])
    with pytest.raises(MemoryError):repo.experience(second['experience_id'])
    assert repo.history(first['conversation_id'])==[]


def test_experience_and_self_api_are_authenticated_and_inspectable(client,model):
    model.decision={'self_updates':[update()]}
    result=client.post('/v1/chat',json={'text':TEXT}).json()
    eid=result['experience_id']
    assert client.get('/v1/experiences/'+eid).json()['user_text']==TEXT
    assert client.post('/v1/experiences/search',json={'query':'电影'}).json()['items']
    assert client.get('/v1/self').json()['states'][0]['domain']=='belief'
    assert client.get('/v1/self/history?domain=belief').json()['observations'][0]['sources']
    assert client.get('/v1/self',headers={'Authorization':'Bearer invalid'}).status_code==401
    assert client.delete('/v1/experiences/'+eid).status_code==200
    assert client.get('/v1/experiences/'+eid).status_code==404
    assert client.get('/v1/self').json()['states']==[]


def test_upgrade_v2_preserves_surviving_messages_with_provenance(repo):
    memory=repo.remember({'text':'迁移测试事实'})[0]
    conversation=uuid4()
    with repo.pool.connection() as c:
        c.execute('ALTER TABLE messages DROP COLUMN experience_id')
        for table in ('episode_details','episode_cues','episode_evidence','episode_versions',
                      'episode_associations','episode_rehearsals','episode_chunks','episode_jobs','episodes'):
            c.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table)))
        c.execute('ALTER TABLE model_calls DROP COLUMN phase,DROP COLUMN source_refs,DROP COLUMN estimated_input_tokens,DROP COLUMN policy_version,DROP COLUMN estimator_version')
        for table in ('context_links','self_observations','experience_jobs','experience_chunks','experiences'):
            c.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table)))
        c.execute('DELETE FROM schema_migrations WHERE version>=3')
        c.execute("INSERT INTO messages(conversation_id,role,content,source_ids) VALUES(%s,'user','迁移测试原话',%s)",
                  (conversation,[memory['id']]))
    repo.migrate();repo.migrate()
    records=repo.search_experiences('')
    assert len(records)==1 and records[0]['user_text']=='迁移测试原话'
    repo.forget(memory['id'])
    with pytest.raises(MemoryError):repo.experience(records[0]['id'])
