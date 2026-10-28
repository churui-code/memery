import json
from uuid import uuid4
import pytest
from app.service import MemoryService
from app.corrections import EpisodeCorrection
from app.errors import MemoryError
from app.automatic import validate_decision
from app.worker import process_one
from conftest import FakeEmbeddings
from test_dialogue_v4 import expanded_service, counts


TEXT = '10月18日我和林禾去青石巷。修伞花了46元。林禾买书签花了12元。她说：“下雨也别急，慢慢走就好。”'
FIX = '之前修伞金额说错了，实际是64元。'


def setup_story(repo, model):
    model.decision = {'episodes':[{'title':'青石巷修伞46元','gist':TEXT,'quote':TEXT,
        'details':[{'quote':part,'kind':'amount' if '元' in part else 'speech'} for part in
                   ['修伞花了46元。','林禾买书签花了12元。','她说：“下雨也别急，慢慢走就好。”']]}]}
    service = MemoryService(repo,FakeEmbeddings(),model)
    first = service.chat(TEXT)
    event = repo.episode(first['episode_ids'][0])
    detail = next(d for d in event['details'] if '46' in d['quote'])
    correction = {'target_id':str(event['id']),'expected_revision':event['revision'],
        'experience_id':str(detail['experience_id']),'start':detail['start'],'end':detail['end'],'quote':FIX}
    return service, first, correction


@pytest.mark.parametrize('indexed',[False,True])
def test_correction_filters_every_cloud_path_and_keeps_unaffected_details(repo,model,indexed):
    service,first,correction = setup_story(repo,model)
    if indexed:
        while process_one(repo,FakeEmbeddings()): pass
    # A previous answer and its derived episode must not resurrect the old price.
    model.decision = {}
    service.chat('青石巷修伞多少钱？',first['conversation_id'])
    model.decision = {'corrections':[correction], 'rehearsals':[{'episode_id':correction['target_id'],
        'kind':'correction','quote':FIX}]}
    result = service.chat(FIX,first['conversation_id'])
    assert result['episode_corrections'] and result['history_saved']
    payload = json.loads(model.requests[-1][-1]['content'])
    assert '46元' not in json.dumps(payload,ensure_ascii=False)
    assert '12元' in json.dumps(payload,ensure_ascii=False)
    assert repo.experience(first['experience_id'])['user_text'] == TEXT
    assert not repo.history(first['conversation_id'],cloud_only=True)[0]['content'] == TEXT
    assert all(row['id'] != first['experience_id'] for row in repo.search_experiences('青石巷',cloud_only=True))
    model.decision = {}
    service.chat('青石巷修伞花费和安慰原话？',uuid4())
    payload = json.loads(model.requests[-1][-1]['content'])
    encoded = json.dumps(payload,ensure_ascii=False)
    assert '46元' not in encoded and '64元' in encoded and '12元' in encoded and '下雨也别急' in encoded
    while process_one(repo,FakeEmbeddings()): pass
    with repo.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM episode_chunks WHERE content LIKE '%46元%'").fetchone()
        assert c.execute('SELECT count(*) AS n FROM episode_corrections').fetchone()['n']==1
        version=c.execute('SELECT v.source_reliability FROM episode_versions v JOIN episodes e ON e.id=v.episode_id AND e.revision=v.revision WHERE e.id=%s',(first['episode_ids'][0],)).fetchone()
        assert version['source_reliability']==repo.episode(first['episode_ids'][0])['source_reliability']
    # Deleting the correction cannot reactivate old wrong evidence.
    repo.forget_experience(result['experience_id'])
    safe = repo.episode(first['episode_ids'][0])
    assert '46元' not in json.dumps(safe,default=str,ensure_ascii=False)
    repo.replay_deletions()
    assert '46元' not in json.dumps(repo.episode(first['episode_ids'][0]),default=str,ensure_ascii=False)


def test_correction_transaction_conflict_rolls_back(repo,model):
    service,first,correction = setup_story(repo,model)
    before = counts(repo)
    bad = EpisodeCorrection(**{**correction,'expected_revision':99})
    with pytest.raises(MemoryError):
        repo.save_answer(uuid4(),FIX,'确认',[],repo.epoch(),corrections=[bad])
    assert counts(repo)==before
    assert '46元' in repo.episode(first['episode_ids'][0])['gist']


def test_correction_requires_displayed_exact_target(repo,model):
    service,first,correction = setup_story(repo,model)
    event = repo.episode(first['episode_ids'][0])
    for changed in [{'target_id':str(uuid4())},{'start':0},{'quote':'我没有说过这句'}]:
        with pytest.raises(MemoryError):
            validate_decision({'corrections':[{**correction,**changed}]},FIX,[],[],[event])


@pytest.mark.parametrize('raw',[
    {'episodes':[{'title':'秘密值不能出现在日志','gist':'无效','quote':'不存在的私密原话'}]},
    {'用户秘密字段':'不可记录'},
])
def test_failed_reassessment_returns_answer_without_any_turn_writes(repo,model,monkeypatch,caplog,raw):
    service,_ = expanded_service(repo,model,monkeypatch)
    model.reassessment = raw
    before = counts(repo)
    result = service.chat('那个礼物多少钱？')
    assert counts(repo)==before
    assert result['memory_status']=='failed' and result['experience_id'] is None
    assert not result['history_saved'] and result['memory_changes']==[] and result['self_changes']==[]
    assert len(result['calls'])==3 and result['warnings'][0]['diagnostics']
    assert '本轮记忆整理失败，回答未保存' in result['answer']
    assert result['answer'].startswith('本轮记忆整理失败，回答未保存。本轮记忆变更均未提交。')
    payload=json.loads(model.requests[-1][-1]['content'])
    assert not payload['retain_this_turn'] and not payload['planned_memory_changes'] and not payload['planned_self_updates']
    assert payload['memory_failure']=={'code':'memory_reassessment_failed','changes_committed':False,'history_saved':False}
    assert '私密' not in caplog.text and '秘密' not in caplog.text and '不可记录' not in caplog.text


@pytest.mark.parametrize('text,quote,subject,expected',[
    ('给这个本子起名轻松一点，不用太文艺。',None,'user',0),
    ('我平时喜欢轻松、不太文艺的名字。','我平时喜欢轻松、不太文艺的名字。','user',1),
    ('妹妹平时不喜欢太文艺的名字。','妹妹平时不喜欢太文艺的名字。','妹妹',1),
    ('如果我不喜欢文艺名字呢？',None,'user',0),
])
def test_durable_profile_requires_explicit_scope_evidence(repo,model,text,quote,subject,expected):
    model.decision={'actions':[{'operation':'create','quote':text,'text':text,'kind':'preference',
        'subject':subject,'confidence':.99,'durability_quote':quote}]}
    result=MemoryService(repo,FakeEmbeddings(),model).chat(text)
    assert len(result['memory_changes'])==expected
    assert result['history_saved']
    if expected: assert repo.get(result['memory_changes'][0]['id'])['subject']==subject


def test_schema6_upgrade_preserves_archive_and_is_repeatable(repo,model):
    _,first,_=setup_story(repo,model)
    original=repo.experience(first['experience_id'])
    with repo.pool.connection() as c:
        c.execute('DROP TABLE episode_corrections')
        c.execute('ALTER TABLE experiences DROP COLUMN correction_history_blocked')
        c.execute('DELETE FROM schema_migrations WHERE version=7')
    repo.migrate()
    repo.migrate()
    assert repo.experience(first['experience_id'])==original
    with repo.pool.connection() as c:
        assert c.execute('SELECT max(version) AS n FROM schema_migrations').fetchone()['n']==7
        assert c.execute('SELECT count(*) AS n FROM episode_corrections').fetchone()['n']==0


def test_repeated_or_foreign_event_correction_cannot_change_other_event(repo,model):
    service,first,correction=setup_story(repo,model)
    model.decision={}
    other=service.chat('另一天在青石巷修书花了85元。')
    other_before=repo.episode(other['episode_ids'][0])
    model.decision={'corrections':[correction]}
    result=service.chat(FIX)
    assert result['episode_corrections']
    before=counts(repo)
    with pytest.raises(MemoryError):
        repo.save_answer(uuid4(),FIX,'确认',[],repo.epoch(),corrections=[EpisodeCorrection(**correction)])
    assert counts(repo)==before
    assert not repo.episode(other['episode_ids'][0])['context_stale']
    assert '85元' in repo.episode(other['episode_ids'][0])['gist']


def test_failed_reassessment_answer_failure_and_epoch_conflict_do_not_write(repo,model,monkeypatch):
    service,_=expanded_service(repo,model,monkeypatch)
    model.reassessment={'actions':[]}
    before=counts(repo)
    model.error=MemoryError('model_timeout','模拟超时',504)
    with pytest.raises(MemoryError) as exc: service.chat('那个礼物多少钱？')
    assert exc.value.code=='model_timeout' and counts(repo)==before
    assert len(model.requests)==1  # intake + reassessment already used both reserved slots
    model.error=None
    def concurrent_change():
        with repo.pool.connection() as c: c.execute('UPDATE system_state SET epoch=epoch+1')
    model.callback=concurrent_change
    with pytest.raises(MemoryError) as exc: service.chat('那个礼物多少钱？')
    assert exc.value.code=='context_changed'
    after=counts(repo)
    assert after==before


def test_live_budget_counts_failures_and_survives_restart(tmp_path):
    from scripts.verify_chat_repairs import RequestLedger
    path=tmp_path/'ledger.json'
    ledger=RequestLedger(path,2)
    one=ledger.reserve()
    ledger.finish(one,'failed',error='timeout')
    RequestLedger(path,2).reserve()
    with pytest.raises(RuntimeError): RequestLedger(path,2).reserve()
    assert len(json.loads(path.read_text())['calls'])==2


def test_corrected_time_does_not_leave_derived_date_in_current_view():
    from app.corrections import sanitize_episode
    item={'source_experience_ids':['source'], 'title':'旧日期', 'gist':'旧日期',
          'time_quote':'2026年10月18日','occurred_at':'2026-10-18','time_precision':'day',
          'details':[{'experience_id':'source','role':'user','quote':'2026年10月18日。','start':0,'end':12}],
          'evidence':[{'experience_id':'source','role':'user','quote':'2026年10月18日。','start':0,'end':12}]}
    result=sanitize_episode(item,[{'experience_id':'source','start':0,'end':12}])
    assert result['occurred_at'] is None and result['time_precision']=='unknown'


def test_snapshot_restore_then_deletion_replay_keeps_correction_tombstones(repo,model):
    from dataclasses import replace
    from psycopg import sql
    from app.repository import Repository
    from app.lab import LabManager
    service,first,correction=setup_story(repo,model)
    model.decision={'corrections':[correction]}
    fixed=service.chat(FIX)
    restored=Repository(replace(repo.settings,db_schema='test_restore_'+uuid4().hex))
    try:
        restored.migrate()
        LabManager._copy_schema(restored,repo.settings.db_schema,restored.settings.db_schema)
        repo.forget_experience(fixed['experience_id'])
        # The restored snapshot shares the newer deletion journal, as restore does.
        restored.replay_deletions(required=True)
        assert '46元' not in json.dumps(restored.episode(first['episode_ids'][0]),default=str,ensure_ascii=False)
        with restored.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM episode_corrections').fetchone()['n']==1
            assert c.execute('SELECT deleted_at FROM experiences WHERE id=%s',
                             (fixed['experience_id'],)).fetchone()['deleted_at'] is not None
    finally:
        with restored.pool.connection() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(restored.settings.db_schema)))
        restored.close()


@pytest.mark.parametrize('fault',['missing_top_quote','conflicting_quote','cue_outside_event','historical_time','unsupported_quote'])
def test_initial_shape_error_retries_with_diagnostics_and_commits_only_valid_proposal(repo,model,monkeypatch,fault):
    text='这次我拿到了蓝色小碗。后来我去了公园。'
    good={'episodes':[{'title':'拿到作品','gist':'用户拿到作品','quote_refs':['u0001']}],
          'retain_turn':True,'self_updates':[]}
    invalid=json.loads(json.dumps(good))
    if fault=='missing_top_quote': invalid['episodes'][0].pop('quote_refs')
    elif fault=='conflicting_quote': invalid['episodes'][0]['quote']='这句与原文不同。'
    elif fault=='cue_outside_event':
        invalid['episodes'][0]['cues']=[{'kind':'place','text':'公园','quote_refs':['u0002']}]
    elif fault=='historical_time':
        invalid['experience']={'title':'回忆旧事件','summary':text,'time_quote':'10月17日'}
    else:
        invalid['episodes'][0].pop('quote_refs')
        invalid['episodes'][0]['quote']='这句与原文不同。'
    prompts=[]
    def propose(system,prompt):
        prompts.append(json.loads(prompt))
        return {'decision':invalid if len(prompts)==1 else good,'usage':{'input_tokens':5,'output_tokens':3}}
    monkeypatch.setattr(model,'decide_memories',propose)
    result=MemoryService(repo,FakeEmbeddings(),model).chat(text)
    assert result['history_saved'] and len(result['calls'])==3
    assert result['calls'][0]['status']=='failed' and result['calls'][0]['error']=='memory_decision_invalid'
    assert prompts[1]['proposal_errors'] and prompts[1]['current_question']==prompts[0]['current_question']
    if fault=='cue_outside_event':
        assert prompts[1]['proposal_errors']==[{'code':'episode_detail_outside_event_scope',
                                               'path':['episodes',0,'cues',0,'quote']}]
    if fault=='historical_time':
        assert prompts[1]['proposal_errors']==[{'code':'unsupported_event_time','path':['experience','time_quote']}]
    assert '这句与原文不同' not in json.dumps(prompts[1],ensure_ascii=False)
    assert repo.experience(result['experience_id'])['user_text']==text
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM experiences').fetchone()['n']==1
        assert c.execute("SELECT status FROM model_calls WHERE id=%s",(result['calls'][0]['id'],)).fetchone()['status']=='memory_decision_invalid'


def test_corrected_cue_cannot_bias_candidates_with_superseded_quote(repo,model):
    service,first,correction=setup_story(repo,model)
    # Add a real cue to the encoded source, then correct its evidence range.
    with repo.pool.connection() as c:
        evidence=c.execute('SELECT evidence_id FROM episode_details WHERE episode_id=%s AND kind=%s LIMIT 1',
                           (first['episode_ids'][0],'amount')).fetchone()
        # Pick the exact old amount regardless of row ordering.
        evidence=c.execute("SELECT id FROM episode_evidence WHERE episode_id=%s AND quote='修伞花了46元。'",(first['episode_ids'][0],)).fetchone()
        c.execute('INSERT INTO episode_cues(id,episode_id,evidence_id,kind,text) VALUES(%s,%s,%s,%s,%s)',
                  (uuid4(),first['episode_ids'][0],evidence['id'],'amount','46元'))
    model.decision={'corrections':[correction]}
    service.chat(FIX)
    candidates=repo.episode_candidates('46元')
    assert all('46元' not in json.dumps(row.get('matched_cues',[]),ensure_ascii=False) for row in candidates)


def test_same_sentence_correction_preserves_other_person_amount_without_restating_it(repo,model):
    text='我和周岚去河湾街花店。她花76元买了一盆薄荷，我花14元买了一包种子。'
    shared='她花76元买了一盆薄荷，我花14元买了一包种子。'
    model.decision={'episodes':[{'title':'花店购物','gist':text,'quote':text,
        'details':[{'quote':shared,'kind':'amount'}]}]}
    service=MemoryService(repo,FakeEmbeddings(),model)
    first=service.chat(text)
    event=repo.episode(first['episode_ids'][0]);detail=event['details'][0]
    fix='刚才种子的金额说错了，实际是41元。'
    model.decision={'corrections':[{'target_id':str(event['id']),'expected_revision':1,
        'experience_id':str(first['experience_id']),'start':detail['start'],'end':detail['end'],
        'old_quote':'我花14元买了一包种子。','quote':fix}]}
    result=service.chat(fix,first['conversation_id'])
    assert result['episode_corrections'] and 'old_quote' not in result['episode_corrections'][0]
    assert '14元' not in json.dumps(model.requests[-1],ensure_ascii=False)
    model.decision={}
    service.chat('河湾街花店里我和周岚各花多少钱，分别买了什么？',uuid4())
    context=json.loads(model.requests[-1][-1]['content']);encoded=json.dumps(context,ensure_ascii=False)
    assert '14元' not in encoded, encoded
    assert '76元' in encoded, encoded
    assert '41元' in encoded, encoded
    assert '薄荷' in encoded and '种子' in encoded
    assert any(d['quote']=='她花76元买了一盆薄荷，' for e in context['experience_evidence'] for d in e.get('details',[]))


@pytest.mark.parametrize('ranges,accepted',[
    ([(0,10),(0,10)],False), ([(0,10),(2,8)],False), ([(0,8),(6,10)],False),
    ([(0,5),(5,10)],True),
])
def test_overlapping_correction_batch_is_rejected_before_commit(ranges,accepted):
    from app.corrections import validate_corrections
    source,target=uuid4(),uuid4()
    event={'id':target,'revision':1,'details':[{'experience_id':source,'role':'user','start':a,'end':b,'quote':'甲'*(b-a)} for a,b in ranges]}
    proposals=[EpisodeCorrection(target_id=target,expected_revision=1,experience_id=source,start=a,end=b,quote='更正') for a,b in ranges]
    if accepted: validate_corrections(proposals,'更正',[event])
    else:
        with pytest.raises(ValueError,match='correction_ranges_overlap'):validate_corrections(proposals,'更正',[event])


def test_repeated_correction_remains_recallable_from_original_place(repo,model):
    service,first,correction=setup_story(repo,model)
    correction['old_quote']='修伞花了46元。'
    model.decision={'corrections':[correction]}
    second=service.chat(FIX,first['conversation_id'])
    second_event=repo.episode(second['episode_ids'][0])
    d=next(d for d in second_event['details'] if '64元' in d['quote'])
    third_text='刚才修伞金额仍然记错了，核对下来实际是83元。'
    model.decision={'corrections':[{'target_id':str(second_event['id']),'expected_revision':second_event['revision'],
        'experience_id':str(second['experience_id']),'start':d['start'],'end':d['end'],
        'old_quote':d['quote'],'quote':third_text}]}
    third=service.chat(third_text,first['conversation_id'])
    assert third['episode_corrections']
    model.decision={}
    service.chat('青石巷那次修伞实际花了多少钱？',uuid4())
    payload=json.loads(model.requests[-1][-1]['content']);encoded=json.dumps(payload,ensure_ascii=False)
    assert '83元' in encoded and '64元' not in encoded and '46元' not in encoded


@pytest.mark.parametrize('has_fact',[False,True])
def test_cloud_fact_actions_cannot_use_episode_ids(has_fact):
    from app.model import CloudContractError, structured_tool, validate_cloud_contract
    fact,event=str(uuid4()),str(uuid4())
    payload={'memory_evidence':[{'id':fact}] if has_fact else [],
             'experience_evidence':[{'id':event}]}
    schema=structured_tool('intake',payload)['input_schema']
    action={'operation':'update','target_id':event,'expected_revision':1,'confidence':.95,
            'text':'核对金额','kind':'fact','durability_quote':None,'quote_refs':['u0001']}
    decision={'actions':[action],'retain_turn':True,'self_updates':[]}
    with pytest.raises(CloudContractError) as exc:
        validate_cloud_contract(decision,schema)
    assert exc.value.diagnostics[0]['code']=='cloud_value_not_allowed'
    assert exc.value.diagnostics[0]['path'][:2]==['actions',0]
    if has_fact:
        action['target_id']=fact
    else:
        action.update(operation='create',target_id=None)
        action.pop('expected_revision')
    validate_cloud_contract(decision,schema)


def test_actual_adapter_retries_forbidden_fact_target_without_losing_audit(repo,model,monkeypatch):
    from app.model import MiniMax, request_payload
    from app.context import estimate_tokens
    adapter=MiniMax(repo.settings)
    observed=[]
    event=str(uuid4())
    def provider(system,messages,tool=None):
        observed.append((system,messages,tool))
        if tool is None:
            return {'stop_reason':'end_turn','content':[{'type':'text','text':'按当前输入回答。'}]}
        fields=tool['input_schema']['properties']['actions']['items']['properties']
        assert fields['operation']['enum']==['create'] and fields['target_id']['type']=='null'
        decision={'actions':[],'retain_turn':True,'self_updates':[]}
        if len(observed)==1:
            decision['actions']=[{'operation':'update','target_id':event,'expected_revision':1,
                'text':'错误事件目标','confidence':.95,'durability_quote':None,'quote_refs':['u0001']}]
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':decision}]}
    monkeypatch.setattr(adapter,'_request',provider)
    result=MemoryService(repo,FakeEmbeddings(),adapter).chat('核对收据，金额实际是46元。')
    assert result['history_saved'] and result['memory_changes']==[]
    assert [c['status'] for c in result['calls']]==['failed','completed','completed']
    assert len(observed)==3
    diagnostic=json.loads(observed[1][1][0]['content'])['proposal_errors']
    assert diagnostic==[{'code':'cloud_value_not_allowed','path':['actions',0,'operation']},
                        {'code':'cloud_value_not_allowed','path':['actions',0,'target_id']}]
    for call,(system,messages,tool) in zip(result['calls'],observed):
        assert call['estimated_input_tokens']==estimate_tokens(request_payload(repo.settings,system,messages,tool))
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM memories').fetchone()['n']==0
        assert c.execute('SELECT count(*) AS n FROM experiences').fetchone()['n']==1


def test_repeated_scope_error_never_widens_evidence_or_commits(repo,monkeypatch):
    from app.model import MiniMax
    adapter=MiniMax(repo.settings)
    attempts=[]
    def provider(system,messages,tool=None):
        attempts.append(json.loads(messages[0]['content']))
        proposal={'actions':[],'retain_turn':True,'self_updates':[],
            'episodes':[{'title':'购买纸张','gist':'第一件事','quote_refs':['u0001'],
                         'cues':[{'kind':'place','text':'公园','quote_refs':['u0002']}]}]}
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
    monkeypatch.setattr(adapter,'_request',provider)
    before=counts(repo)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo,FakeEmbeddings(),adapter).chat('我买了纸张。后来去了公园。')
    assert exc.value.code=='memory_decision_invalid'
    assert len(attempts)==2 and counts(repo)==before
    assert attempts[0]['current_question']==attempts[1]['current_question']
    with repo.pool.connection() as c:
        rows=c.execute('SELECT phase,status FROM model_calls').fetchall()
    assert len(rows)==2 and all(row['phase']=='intake' and row['status']=='memory_decision_invalid' for row in rows)


@pytest.mark.parametrize('cloud_adapter',[False,True])
def test_reassessment_append_to_correction_target_degrades_before_commit(repo,model,monkeypatch,cloud_adapter):
    from app.model import MiniMax
    service,first,correction=setup_story(repo,model)
    target=correction['target_id']
    before=counts(repo)
    original=repo.episode(first['episode_ids'][0])
    proposal={'episodes':[{'title':'核对修伞金额','gist':FIX,'target_id':target,'quote_refs':['u0001']}],
              'self_updates':[]}
    model.decision={'corrections':[correction]}
    if cloud_adapter:
        adapter=MiniMax(repo.settings)
        def provider(system,messages,tool=None):
            field=tool['input_schema']['properties']['episodes']['items']['properties']['target_id']
            assert field['type']=='null' or target not in field['anyOf'][0]['enum']
            return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
        monkeypatch.setattr(adapter,'_request',provider)
        monkeypatch.setattr(model,'reassess_memories',adapter.reassess_memories)
    else:
        model.reassessment=proposal
    result=service.chat(FIX,first['conversation_id'])
    assert [row['phase'] for row in result['calls']]==['intake','reassessment','answer']
    assert result['memory_status']=='failed' and not result['history_saved']
    assert result['episode_corrections']==[] and counts(repo)==before
    assert repo.episode(first['episode_ids'][0])==original
    payload=json.loads(model.requests[-1][-1]['content'])
    assert payload['planned_episode_corrections']==[] and payload['planned_self_updates']==[]
    with repo.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM episode_corrections').fetchone()['n']==0


def test_repository_rejects_correction_append_conflict_before_writes(repo,model):
    _,first,correction=setup_story(repo,model)
    before=counts(repo)
    with pytest.raises(MemoryError) as exc:
        repo.save_answer(uuid4(),FIX,'核对回答',[],repo.epoch(),
            corrections=[EpisodeCorrection(**correction)],
            episode_proposals=[{'target_id':correction['target_id'],'title':'核对','gist':FIX,'quote':FIX}],
            context_roots=[('episode',first['episode_ids'][0])])
    assert exc.value.code=='memory_decision_invalid' and counts(repo)==before


def test_optional_empty_time_stays_unknown_without_discarding_valid_event_date(repo,model):
    first='2026年10月16日我在陶器铺修茶壶。'
    second='离开时朋友递来竹篮。'
    model.decision={'experience':{'title':'陶器铺经历','summary':first+second,'time_quote':'',
                                  'occurred_at':'2026-10-16T12:00:00+08:00'},
                    'episodes':[{'title':'修茶壶','gist':first,'quote':first,'time_quote':'2026年10月16日'},
                                {'title':'递竹篮','gist':second,'quote':second,'time_quote':'',
                                 'occurred_at':'2026-10-16T12:00:00+08:00'}]}
    result=MemoryService(repo,FakeEmbeddings(),model).chat(first+second)
    assert result['history_saved']
    source=repo.experience(result['experience_id'])
    assert source['time_quote'] is None and source['occurred_at'] is None
    dated,undated=[repo.episode(eid) for eid in result['episode_ids']]
    assert dated['time_quote']=='2026年10月16日' and dated['time_precision']=='day'
    assert undated['time_quote'] is None and undated['occurred_at'] is None
    assert undated['time_precision']=='unknown'


@pytest.mark.parametrize('field',['experience','episode'])
def test_nonempty_invalid_time_is_not_normalized_away(field):
    first='10月16日我在陶器铺修茶壶。'
    second='离开时朋友递来竹篮。'
    raw=({'experience':{'title':'经历','summary':first+second,'time_quote':'10月13日'}} if field=='experience'
         else {'episodes':[{'title':'递竹篮','gist':second,'quote':second,'time_quote':'10月16日'}]})
    with pytest.raises(MemoryError):
        validate_decision(raw,first+second,[])


@pytest.mark.parametrize('late_failure',[False,True])
def test_fact_and_event_correction_preserve_independent_user_sources_atomically(repo,model,monkeypatch,late_failure):
    model.decision={'actions':[{'operation':'create','kind':'fact','text':'用户修伞花了46元。',
                               'quote':TEXT,'confidence':.99}],
                    'episodes':[{'title':'青石巷修伞','gist':TEXT,'quote':TEXT,
                        'details':[{'quote':part,'kind':'amount' if '元' in part else 'speech'} for part in
                            ['修伞花了46元。','林禾买书签花了12元。','她说：“下雨也别急，慢慢走就好。”']]},
                        {'title':'早已失效的记录','gist':TEXT,'quote':TEXT}]}
    service=MemoryService(repo,FakeEmbeddings(),model)
    first=service.chat(TEXT)
    fact=first['memory_changes'][0]
    event=repo.episode(first['episode_ids'][0])
    detail=next(d for d in event['details'] if '46元' in d['quote'])
    stale_id=first['episode_ids'][1]
    with repo.pool.connection() as c:
        c.execute('UPDATE episodes SET context_stale=true WHERE id=%s',(stale_id,))
    other='另一天林禾在青石巷花85元买了地图。'
    model.decision={'episodes':[{'title':'另一天买地图','gist':other,'quote':other,
        'details':[{'kind':'amount','quote':other,'meaning':'在46元修伞后另买地图'}]}]}
    separate=service.chat(other,first['conversation_id'])
    correction={'target_id':str(event['id']),'expected_revision':event['revision'],
        'experience_id':str(first['experience_id']),'start':detail['start'],'end':detail['end'],
        'old_quote':'修伞花了46元。','quote':FIX}
    model.decision={'actions':[{'operation':'update','target_id':str(fact['id']),
        'expected_revision':fact['revision'],'kind':'fact','text':'用户修伞实际花了64元。',
        'quote':FIX,'confidence':.99}], 'corrections':[correction],
        'rehearsals':[{'episode_id':str(event['id']),'kind':'correction','quote':FIX}]}
    if late_failure:
        from psycopg import sql
        def snapshot():
            with repo.pool.connection() as c:
                return {table:c.execute(sql.SQL('SELECT to_jsonb(t) AS row FROM {} t ORDER BY t::text').format(
                    sql.Identifier(table))).fetchall() for table in
                    ('memories','events','memory_evidence','outbox','experiences','experience_jobs','context_links',
                     'episodes','episode_versions','episode_evidence','episode_details','episode_cues','episode_jobs',
                     'episode_corrections','episode_associations','episode_rehearsals','messages','self_observations','system_state')}
        before=snapshot()
        def fail(*args,**kwargs):
            raise RuntimeError('synthetic_late_commit_failure')
        monkeypatch.setattr(repo,'_save_self_updates',fail)
        with pytest.raises(MemoryError) as exc:
            service.chat(FIX+'书签和安慰原话也请再说一遍。',first['conversation_id'])
        assert exc.value.code=='memory_commit_failed'
        assert snapshot()==before
        return
    result=service.chat(FIX+'书签和安慰原话也请再说一遍。',first['conversation_id'])
    assert result['memory_status']=='committed' and result['history_saved']
    assert result['episode_corrections'] and result['memory_changes'][0]['revision']==2
    context=json.dumps(json.loads(model.requests[-1][-1]['content']),ensure_ascii=False)
    assert '46元' not in context,context
    assert '12元' in context and '下雨也别急' in context,context
    assert not repo.episode(separate['episode_ids'][0])['context_stale']
    assert repo.episode(stale_id)['context_stale']
    model.decision={}
    service.chat('青石巷修伞和买地图各花多少钱，书签多少钱，安慰原话是什么？',uuid4())
    context=json.dumps(json.loads(model.requests[-1][-1]['content']),ensure_ascii=False)
    assert '46元' not in context and all(value in context for value in ('64元','12元','85元','下雨也别急'))


def test_recalled_correction_candidates_keep_old_amount_evidence():
    from app.recall import present_episode
    source=str(uuid4())
    where='10月15日我和陆禾去了枫桥路修鞋店。'
    price='我花68元修短靴，陆禾花27元买了鞋刷。'
    event={'id':str(uuid4()),'revision':1,'title':'修鞋店','gist':where+price,
           'context_type':'reported','source_experience_ids':[source],'cues':[],
           'details':[{'id':'place','kind':'place','quote':where,'experience_id':source,
                       'role':'user','start':0,'end':len(where)},
                      {'id':'price','kind':'amount','quote':price,'experience_id':source,
                       'role':'user','start':len(where),'end':len(where+price)}]}
    presented=present_episode(event, {'activation_score':.8})
    assert 'price' in presented['required_detail_ids']
    assert any(d['quote']==price for d in presented['details'])


def test_corrected_event_does_not_reactivate_assistant_paraphrases():
    from app.corrections import sanitize_episode
    source,assistant=str(uuid4()),str(uuid4())
    user={'experience_id':source,'role':'user','start':0,'end':8,'quote':'修伞花了46元。'}
    derived={'experience_id':assistant,'role':'assistant','start':0,'end':9,'quote':'你修伞花了46元。'}
    event={'source_experience_ids':[source,assistant],'details':[user,derived],
           'evidence':[user,derived],'cues':[{**derived,'text':'46元'}]}
    safe=sanitize_episode(event,[{'experience_id':source,'start':0,'end':8}])
    assert '46元' not in json.dumps(safe,ensure_ascii=False)
    assert safe['details']==[] and safe['evidence']==[] and safe['cues']==[]


@pytest.mark.parametrize('second_invalid',[False,True])
def test_reproposal_keeps_query_out_of_reported_event(repo,model,monkeypatch,second_invalid):
    model.decision={'episodes':[{'title':'青石巷往事','gist':TEXT,'quote':TEXT,'context_type':'reported'}]}
    service=MemoryService(repo,FakeEmbeddings(),model)
    first=service.chat(TEXT)
    original=repo.episode(first['episode_ids'][0])
    before=counts(repo)
    attempts=[]
    question='上次在青石巷修伞花了多少钱，安慰原话是什么？'
    def propose(system,prompt):
        attempts.append(json.loads(prompt))
        episode={'title':'询问旧事','gist':question,'quote_refs':['u0001'],'context_type':'conversation'}
        if len(attempts)==1:
            episode['target_id']=str(first['episode_ids'][0])
        elif second_invalid:
            episode.pop('quote_refs')
            episode['quote']='不属于本轮输入的原话。'
        return {'decision':{'episodes':[episode]},'usage':{}}
    monkeypatch.setattr(model,'decide_memories',propose)
    if second_invalid:
        with pytest.raises(MemoryError) as exc:
            service.chat(question,uuid4())
        assert exc.value.code=='memory_decision_invalid' and counts(repo)==before
        assert len(model.requests)==1  # Only the original story answer.
    else:
        result=service.chat(question,uuid4())
        assert result['history_saved'] and result['episode_ids']!=first['episode_ids']
        assert len(result['calls'])==3
    assert len(attempts)==2
    assert attempts[0]['current_question']==attempts[1]['current_question']
    assert attempts[1]['proposal_errors']==[{'code':'episode_target_context_type_mismatch',
                                           'path':['episodes','target_id']}]
    assert repo.episode(first['episode_ids'][0])==original


def test_source_change_before_reproposal_stops_before_second_request(repo,model,monkeypatch):
    service,first,_=setup_story(repo,model)
    model.decision={'episodes':[{'title':'无效提案','gist':'不应写入','quote':'不存在的原话。'}]}
    original_finish=repo.finish_call
    deleted=[]
    def finish(call_id,status,usage=None):
        result=original_finish(call_id,status,usage)
        if status=='memory_decision_invalid' and not deleted:
            deleted.append(True)
            repo.forget_experience(first['experience_id'])
        return result
    monkeypatch.setattr(repo,'finish_call',finish)
    before=len(model.decision_requests)
    with pytest.raises(MemoryError) as exc:
        service.chat('青石巷那次花费是什么？')
    assert exc.value.code=='context_changed'
    assert len(model.decision_requests)==before+1 and deleted


def test_cloud_contract_reports_all_known_errors_without_values_or_unknown_keys():
    from app.model import CloudContractError, structured_tool, validate_cloud_contract
    schema=structured_tool('intake',{'experience_evidence':[],'memory_evidence':[]})['input_schema']
    proposal={'actions':[],'retain_turn':True,'self_updates':[],
        'episodes':[{'title':'测试','gist':'测试','quote_refs':['u0001'],
            'details':[{'kind':'秘密非法类型','quote_refs':['u0001']},
                       {'kind':'另一非法类型','quote_refs':['u0001']}]}],
        '私密字段名称':'私密内容'}
    with pytest.raises(CloudContractError) as exc:
        validate_cloud_contract(proposal,schema)
    diagnostics=exc.value.diagnostics
    assert {'code':'cloud_value_not_allowed','path':['episodes',0,'details',0,'kind']} in diagnostics
    assert {'code':'cloud_value_not_allowed','path':['episodes',0,'details',1,'kind']} in diagnostics
    assert {'code':'cloud_extra_field_forbidden','path':['field']} in diagnostics
    assert '私密' not in str(diagnostics) and '非法类型' not in str(diagnostics)
    proposal['episodes'][0]['details']*=10
    with pytest.raises(CloudContractError) as exc:
        validate_cloud_contract(proposal,schema)
    assert len(exc.value.diagnostics)==12


def test_cloud_correction_reference_binds_frozen_detail_then_commits(repo,model,monkeypatch):
    from app.model import MiniMax, request_payload
    from app.context import estimate_tokens
    service,first,_=setup_story(repo,model)
    adapter=MiniMax(repo.settings)
    observed=[]
    def provider(system,messages,tool=None):
        observed.append((system,messages,tool))
        if tool is None:
            return {'stop_reason':'end_turn','content':[{'type':'text','text':'本次修伞金额为64元。'}]}
        payload=json.loads(messages[0]['content'])
        proposal={'self_updates':[], 'episodes':[{'title':'核对修伞金额','gist':FIX,'quote_refs':['u0001']}]}
        if tool['name']=='propose_memories':
            targets=payload['correction_targets']
            ref=next(key for key,row in targets.items() if '46元' in row['quote'])
            assert targets[ref]['target_id']==str(first['episode_ids'][0])
            fields=tool['input_schema']['properties']['corrections']['items']['properties']
            assert ref in fields['detail_ref']['enum'] and 'start' not in fields
            proposal.update(actions=[],retain_turn=True,corrections=[{
                'detail_ref':ref,'old_quote':'46元','quote_refs':['u0001']}])
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
    monkeypatch.setattr(adapter,'_request',provider)
    result=MemoryService(repo,FakeEmbeddings(),adapter).chat(FIX,first['conversation_id'])
    assert result['memory_status']=='committed' and result['history_saved']
    assert len(result['episode_corrections'])==1 and len(observed)==3
    for call,(system,messages,tool) in zip(result['calls'],observed):
        assert call['estimated_input_tokens']==estimate_tokens(request_payload(repo.settings,system,messages,tool))
    assert repo.experience(first['experience_id'])['user_text']==TEXT
    model.decision={}
    service.chat('青石巷那次修伞花费、书签价格和安慰原话？',uuid4())
    context=json.dumps(json.loads(model.requests[-1][-1]['content']),ensure_ascii=False)
    assert '46元' not in context and '64元' in context and '12元' in context and '下雨也别急' in context


@pytest.mark.parametrize('fault',['unknown_ref','coordinates','foreign_old_quote','overlap'])
def test_invalid_cloud_correction_reference_does_not_gain_authority(repo,model,monkeypatch,fault):
    from app.model import MiniMax
    _,first,_=setup_story(repo,model)
    before=counts(repo)
    adapter=MiniMax(repo.settings)
    observed=[]
    def provider(system,messages,tool=None):
        assert tool['name']=='propose_memories'
        payload=json.loads(messages[0]['content']);observed.append(payload)
        ref=next(key for key,row in payload['correction_targets'].items() if '46元' in row['quote'])
        correction={'detail_ref':ref,'old_quote':'46元','quote_refs':['u0001']}
        if fault=='unknown_ref': correction['detail_ref']='c9999'
        if fault=='coordinates': correction.update(target_id=str(uuid4()),start=0,end=9999)
        if fault=='foreign_old_quote': correction['old_quote']='12元'
        proposal={'actions':[],'retain_turn':True,'self_updates':[],'corrections':[correction]}
        if fault=='overlap': proposal['corrections'].append(dict(correction))
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
    monkeypatch.setattr(adapter,'_request',provider)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo,FakeEmbeddings(),adapter).chat(FIX,first['conversation_id'])
    assert exc.value.code=='memory_decision_invalid' and counts(repo)==before
    assert len(observed)==2 and observed[0]['correction_targets']==observed[1]['correction_targets']
    assert '46元' in repo.episode(first['episode_ids'][0])['gist']


def test_correction_targets_exclude_assistant_legacy_and_deduplicate_without_mutation():
    from copy import deepcopy
    from app.corrections import correction_targets
    user={'role':'user','experience_id':str(uuid4()),'start':0,'end':6,'quote':'修伞46元。'}
    episode={'id':str(uuid4()),'revision':3,'details':[user,user,{**user,'role':'assistant'}]}
    payload={'experience_evidence':[episode,{**episode,'id':str(uuid4()),'legacy_projection':True}]}
    before=deepcopy(payload)
    targets=correction_targets(payload)
    assert payload==before and list(targets)==['c0001']
    assert targets['c0001']=={'target_id':episode['id'],'expected_revision':3,
        'experience_id':user['experience_id'],'start':0,'end':6,'quote':'修伞46元。'}


def test_cloud_reference_version_is_rechecked_after_provider_returns(repo,model,monkeypatch):
    from app.model import MiniMax
    _,first,_=setup_story(repo,model)
    before=counts(repo)
    adapter=MiniMax(repo.settings)
    def provider(system,messages,tool=None):
        payload=json.loads(messages[0]['content'])
        ref=next(key for key,row in payload['correction_targets'].items() if '46元' in row['quote'])
        # Simulate a revision change after the frozen evidence was sent.
        with repo.pool.connection() as c:
            c.execute('UPDATE episodes SET revision=revision+1 WHERE id=%s',(first['episode_ids'][0],))
        proposal={'actions':[],'retain_turn':True,'self_updates':[],
                  'corrections':[{'detail_ref':ref,'old_quote':'46元','quote_refs':['u0001']}]}
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
    monkeypatch.setattr(adapter,'_request',provider)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo,FakeEmbeddings(),adapter).chat(FIX,first['conversation_id'])
    assert exc.value.code=='context_changed' and counts(repo)==before
    assert repo.episode(first['episode_ids'][0])['revision']==2


def test_cloud_no_record_discards_invalid_development_without_resolving_references(repo,monkeypatch):
    from app.model import MiniMax
    adapter=MiniMax(repo.settings)
    def provider(system,messages,tool=None):
        if tool is None:
            return {'stop_reason':'end_turn','content':[{'type':'text','text':'本轮不保存。'}]}
        proposal={'actions':[],'retain_turn':False,'self_updates':[],
                  'corrections':[{'detail_ref':'不存在','old_quote':'无法授权'}]}
        return {'stop_reason':'tool_use','content':[{'type':'tool_use','name':tool['name'],'input':proposal}]}
    monkeypatch.setattr(adapter,'_request',provider)
    before=counts(repo)
    result=MemoryService(repo,FakeEmbeddings(),adapter).chat('这轮不要保存。')
    assert not result['history_saved'] and counts(repo)==before and result['episode_corrections']==[]
