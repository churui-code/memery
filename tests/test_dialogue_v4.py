"""End-to-end coordination contracts, using the actual repository and composer."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from psycopg import sql

from app.errors import MemoryError
from app.service import MemoryService
from conftest import FakeEmbeddings


@pytest.mark.parametrize('decision', [False, True])
def test_wire_serialization_preserves_input_and_isolates_returned_messages(decision):
    from app.source_units import source_units
    payload = {'system': '合成规则', 'tools': [{'name': 'synthetic'}], 'schema': {'type': 'object'},
        'current_question': '第一句。第二句。',
        'history': [{'role': 'user', 'content': '合成旧问题', 'source_ids': ['synthetic-source']}],
        'memory_evidence': [{'id': 'synthetic-source', 'revision': 1, 'text': '合成事实'}],
        'experience_evidence': [], 'self_state': []}
    before = deepcopy(payload)
    system, contents = MemoryService._wire({'payload': payload}, decision=decision)
    assert system == before['system'] and payload == before
    expected = {key: value for key, value in before.items() if key not in ('system', 'tools', 'schema')}
    expected['current_speaker'] = 'user'
    if decision:
        expected['current_question'] = source_units(before['current_question'])
        assert json.loads(contents) == expected
    else:
        expected.pop('history')
        assert contents[0] == {'role': 'user', 'content': '合成旧问题'}
        assert contents[-1]['role'] == 'user' and json.loads(contents[-1]['content']) == expected
        contents[0]['content'] = '改写返回对象'
        contents.append({'role': 'assistant', 'content': '仅修改返回列表'})
        assert payload == before
    assert MemoryService._wire({'payload': payload}, decision=decision) == MemoryService._wire(
        {'payload': before}, decision=decision)


def test_single_item_provider_arrays_keep_strict_element_validation():
    from uuid import uuid4
    from app.life import SelfUpdate
    source = uuid4()
    data = {'domain': 'belief', 'key': '承诺', 'text': '承诺需要行动', 'reason': '依据当前讨论',
            'quote': '承诺需要行动', 'confidence': .8, 'source_experience_ids': {'item': str(source)}}
    assert SelfUpdate.model_validate(data).source_experience_ids == [source]
    data['source_experience_ids'] = {'item': '无效的来源编号'}
    with pytest.raises(ValueError):
        SelfUpdate.model_validate(data)


def test_provider_tool_schema_inlines_references_without_removing_title_field():
    from app.model import structured_tool
    schema = structured_tool('intake')['input_schema']
    assert '$ref' not in json.dumps(schema) and '$defs' not in schema
    assert 'self_updates' in schema['required']
    event = schema['properties']['episodes']['items']
    assert set(event['required']) == {'title', 'gist', 'quote_refs'}
    assert 'anyOf' not in event and 'quote' not in event['properties']
    assert 'occurrence' not in event['properties']
    assert event['properties']['title']['type'] == 'string'
    assert 'quote_refs' in event['properties']['details']['items']['properties']
    assert 'time_quote' in event['properties'] and 'occurred_at' not in event['properties']


def test_discussion_cannot_be_appended_to_a_fictional_event():
    from uuid import uuid4
    from app.automatic import validate_decision
    eid = str(uuid4())
    proposal = {'episodes': [{'title': '讨论', 'gist': '继续讨论承诺',
        'quote': '继续讨论承诺', 'context_type': 'conversation', 'target_id': eid}]}
    with pytest.raises(MemoryError) as caught:
        validate_decision(proposal, '继续讨论承诺', [], episodes=[{'id': eid, 'context_type': 'fiction'}])
    assert str(caught.value.__cause__) == 'episode_target_context_type_mismatch'


def test_sourced_discussion_theme_is_a_detail_without_becoming_a_personal_fact():
    from app.automatic import validate_decision
    text = '我们讨论合成故事中的承诺。'
    decision = validate_decision({'episodes': [{'title': '主题讨论', 'gist': text,
        'context_type': 'conversation', 'quote_refs': ['u0001'],
        'details': [{'kind': 'theme', 'quote_refs': ['u0001'], 'meaning': '讨论承诺主题'}]}]}, text, [])
    assert decision.actions == []
    assert decision.episodes[0].details[0].kind == 'theme'
    assert decision.episodes[0].details[0].quote == text


@pytest.mark.parametrize('kind', ['amount', 'speech', 'theme', 'emotion', 'sensory', 'phrase', 'fact'])
def test_same_evidence_category_can_be_a_detail_and_a_retrieval_cue(kind):
    from app.automatic import validate_decision
    text = '合成故事：戒指花了37元。'
    data = {'episodes': [{'title': '合成记录', 'gist': text, 'quote_refs': ['u0001'],
        'context_type': 'fiction', 'details': [{'kind': kind, 'quote_refs': ['u0001']}],
        'cues': [{'kind': kind, 'text': '37元', 'quote_refs': ['u0001']}]}]}
    decision = validate_decision(data, text, [])
    assert decision.episodes[0].details[0].kind == decision.episodes[0].cues[0].kind == kind
    assert decision.actions == []
    data['episodes'][0]['cues'][0]['kind'] = 'execute_code'
    with pytest.raises(MemoryError):
        validate_decision(data, text, [])


def test_provider_timestamp_without_source_becomes_unknown_not_a_fact():
    from app.episodes import EpisodeProposal
    from app.life import ExperienceDescription
    event = EpisodeProposal(title='观点交流', gist='讨论承诺', quote='讨论承诺',
                            occurred_at='2025-01-02T12:00:00+08:00')
    assert event.occurred_at is None and event.time_precision == 'unknown'
    original = ExperienceDescription(title='观点交流', summary='讨论承诺',
                                    occurred_at='2025-01-02T12:00:00+08:00')
    assert original.occurred_at is None


def test_hint_only_source_cannot_be_used_as_displayed_original_evidence():
    from uuid import uuid4
    from app.automatic import validate_reassessment
    episode_id, experience_id = str(uuid4()), str(uuid4())
    event = {'id': episode_id, 'source_experience_ids': [experience_id],
             'supported_source_experience_ids': [], 'details': [], 'level': 'hint'}
    update = {'domain': 'belief', 'key': '承诺', 'text': '承诺需要行动', 'reason': '当前讨论',
              'quote': '承诺需要行动', 'confidence': .8, 'source_experience_ids': [experience_id]}
    with pytest.raises(MemoryError):
        validate_reassessment({'self_updates': [update]}, '承诺需要行动', [], [event])
    update['source_experience_ids'] = []
    update['source_episode_ids'] = [episode_id]
    assert validate_reassessment({'self_updates': [update]}, '承诺需要行动', [], [event]).self_updates


def counts(repo):
    with repo.pool.connection() as conn:
        return {name: conn.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(
            sql.Identifier(name))).fetchone()['n'] for name in
            ('experiences', 'episodes', 'memories', 'self_observations', 'episode_rehearsals', 'messages')}


def expanded_service(repo, model, monkeypatch):
    service = MemoryService(repo, FakeEmbeddings(), model)
    saved = service.chat('合成往事：紫藤戒指上刻着星光，礼物花了37元。')
    model.requests.clear()
    model.decision_requests.clear()
    original = repo.episode_candidates
    monkeypatch.setattr(repo, 'episode_candidates', lambda query, **kwargs:
                        [] if query == '那个礼物多少钱？' else original(query, **kwargs))
    model.decision = {'recall': [{'query': '紫藤戒指'}]}
    return service, saved


def test_unchanged_evidence_uses_two_calls_and_logs_only_source_manifest(repo, model):
    result = MemoryService(repo, FakeEmbeddings(), model).chat('合成经历：今天去看了一朵蓝色的花。')
    assert [call['phase'] for call in result['calls']] == ['intake', 'answer']
    assert not model.reassessment_requests
    assert 'payload' not in result['recall_trace']
    assert result['episode_ids']
    with repo.pool.connection() as conn:
        rows = conn.execute('SELECT phase,source_refs,estimated_input_tokens FROM model_calls ORDER BY created_at').fetchall()
    assert [row['phase'] for row in rows] == ['intake', 'answer']
    assert all(row['estimated_input_tokens'] > 0 for row in rows)


def test_source_unit_references_preserve_full_input_and_repeated_quote_position(repo, model):
    text = '我爱你。我们讨论了这句话。我爱你。'
    model.decision = {'episodes': [{'title': '再次说出同一句话', 'gist': '末尾再次出现我爱你。',
        'quote_refs': ['u0003'], 'details': [{'kind': 'speech', 'quote_refs': ['u0003']}]}],
        'self_updates': [{'domain': 'belief', 'key': '语言语境', 'text': '相同话语需要结合语境理解。',
            'reason': '当前讨论重复话语', 'quote_refs': ['u0002'], 'confidence': .8}]}
    result = MemoryService(repo, FakeEmbeddings(), model).chat(text)
    intake = json.loads(model.decision_requests[0])
    assert ''.join(unit['text'] for unit in intake['current_question']) == text
    answer = json.loads(model.requests[-1][-1]['content'])
    assert answer['current_question'] == text
    event = repo.episode(result['episode_ids'][0])
    assert event['evidence'][0]['quote'] == '我爱你。'
    assert event['evidence'][0]['start'] == text.rindex('我爱你。')
    assert event['details'][0]['start'] == text.rindex('我爱你。')
    assert repo.experience(result['experience_id'])['user_text'] == text


def test_http_chat_preserves_boundary_whitespace_in_model_input_and_archive(client, repo, model):
    text = ' \t合成记录：我们讨论了原话。\n我爱你。\n\t '
    response = client.post('/v1/chat', json={'text': text})
    assert response.status_code == 200, response.json()
    result = response.json()
    intake = json.loads(model.decision_requests[0])
    assert ''.join(unit['text'] for unit in intake['current_question']) == text
    answer = json.loads(model.requests[-1][-1]['content'])
    assert answer['current_question'] == text
    assert repo.experience(result['experience_id'])['user_text'] == text
    before = counts(repo)
    assert client.post('/v1/chat', json={'text': ' \t\r\n '}).status_code == 422
    assert counts(repo) == before


@pytest.mark.parametrize('length', [2001, 8000])
def test_long_complete_source_unit_is_valid_for_all_development_quotes(length):
    from uuid import uuid4
    from app.automatic import validate_decision
    from app.source_units import source_units

    text = '合成叙述' + '甲' * (length - 4)
    episode_id = str(uuid4())
    assert source_units(text) == [{'id': 'u0001', 'start': 0, 'end': length, 'text': text}]
    raw = {
        'experience': {'title': '完整长句', 'summary': '合成长句的讨论经历',
            'anchors': [{'quote_refs': ['u0001'], 'meaning': '保留完整依据'}]},
        'self_updates': [{'domain': 'belief', 'key': '完整依据', 'text': '观点需要完整依据',
            'reason': '当前长句带来新的认识', 'quote_refs': ['u0001'], 'confidence': .8,
            'source_episode_ids': [episode_id]}],
        'rehearsals': [{'episode_id': episode_id, 'quote_refs': ['u0001'], 'kind': 'discussion'}],
    }
    decision = validate_decision(raw, text, [], episodes=[{'id': episode_id}])
    assert decision.experience.anchors[0].quote == text
    assert decision.self_updates[0].quote == text
    assert decision.rehearsals[0].quote == text
    assert str(decision.self_updates[0].source_episode_ids[0]) == episode_id
    assert str(decision.rehearsals[0].episode_id) == episode_id
    assert 'quote' not in raw['self_updates'][0]


def test_expansion_reassesses_final_evidence_before_answer(repo, model, monkeypatch):
    service, saved = expanded_service(repo, model, monkeypatch)
    result = service.chat('那个礼物多少钱？')
    assert [call['phase'] for call in result['calls']] == ['intake', 'reassessment', 'answer']
    intake = json.loads(model.decision_requests[0])
    reeval = json.loads(model.reassessment_requests[0])
    answer = json.loads(model.requests[-1][-1]['content'])
    assert intake['experience_evidence'] == []
    assert reeval['experience_evidence'] == answer['experience_evidence']
    assert '37元' in json.dumps(answer['experience_evidence'], ensure_ascii=False)
    assert str(saved['experience_id']) in json.dumps(answer['experience_evidence'])


@pytest.mark.parametrize('forbidden', [
    {'actions': []}, {'retain_turn': False}, {'recall': [{'query': '继续扩大'}]},
    {'conflict': '合成证据存在冲突，请核对。'},
])
def test_invalid_or_conflicting_reassessment_never_commits_half_a_turn(repo, model, monkeypatch, forbidden):
    service, _ = expanded_service(repo, model, monkeypatch)
    before = counts(repo)
    model.reassessment = forbidden
    if 'conflict' in forbidden:
        with pytest.raises(MemoryError) as exc:
            service.chat('那个礼物多少钱？')
        assert exc.value.code == 'memory_evidence_conflict'
        assert model.requests == []
    else:
        result = service.chat('那个礼物多少钱？')
        assert result['memory_status'] == 'failed' and not result['history_saved']
        assert result['warnings'] and result['memory_changes'] == []
        assert len(result['calls']) == 3
    assert counts(repo) == before


def test_do_not_record_cannot_be_relaxed_by_additional_evidence(repo, model, monkeypatch):
    service, _ = expanded_service(repo, model, monkeypatch)
    before = counts(repo)
    model.decision['retain_turn'] = False
    result = service.chat('那个礼物多少钱？')
    assert not result['history_saved'] and result['experience_id'] is None
    assert counts(repo) == before
    assert not model.reassessment_requests
    assert [call['phase'] for call in result['calls']] == ['intake', 'answer']


def test_overall_deadline_prevents_answer_and_commit(repo, model, monkeypatch):
    import app.service as module
    tick = [100.0]
    monkeypatch.setattr(module, 'monotonic', lambda: tick[0])
    repo.settings = replace(repo.settings, chat_deadline_seconds=1)
    original = model.decide_memories
    def expired(*args):
        result = original(*args)
        tick[0] += 2
        return result
    model.decide_memories = expired
    before = counts(repo)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo, FakeEmbeddings(), model).chat('合成超时测试')
    assert exc.value.code == 'chat_deadline'
    assert counts(repo) == before and not model.requests


@pytest.mark.parametrize('protocol_error', ['invalid_tool', 'invalid_input'])
def test_intake_protocol_failure_retries_once_and_keeps_failed_call(repo, model, monkeypatch, protocol_error):
    from app.model import MiniMax
    transport = MiniMax(repo.settings)
    attempts = []
    def request(system, messages, tool=None):
        attempts.append(messages)
        if len(attempts) == 1:
            usage={'input_tokens':321,'output_tokens':17,'thinking':'synthetic private usage text'}
            if protocol_error == 'invalid_tool':
                return {'stop_reason': 'end_turn', 'content': [], 'usage':usage}
            return {'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'name': tool['name'], 'input': []}], 'usage':usage}
        return {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'name': tool['name'],
             'input': {'actions': [], 'retain_turn': True, 'self_updates': []}}], 'usage': {}}
    monkeypatch.setattr(transport, '_request', request)
    monkeypatch.setattr(model, 'decide_memories', transport.decide_memories)
    result = MemoryService(repo, FakeEmbeddings(), model).chat('合成协议重试经历。')
    assert [call['phase'] for call in result['calls']] == ['intake', 'intake', 'answer']
    assert [call['status'] for call in result['calls']] == ['failed', 'completed', 'completed']
    assert result['calls'][0]['error'] == 'memory_decision_invalid'
    assert result['calls'][0]['elapsed_seconds'] >= 0
    assert result['calls'][0]['usage']=={'input_tokens':321,'output_tokens':17}
    assert 'synthetic private usage text' not in str(result['calls'])
    assert attempts[0] == attempts[1]
    assert result['history_saved'] and counts(repo)['experiences'] == 1
    with repo.pool.connection() as conn:
        rows = {row['id']: row for row in conn.execute('SELECT * FROM model_calls').fetchall()}
    first, retried = [rows[call['id']] for call in result['calls'][:2]]
    assert first['status'] == 'memory_decision_invalid' and retried['status'] == 'intake_completed'
    assert first['input_tokens']==321 and first['output_tokens']==17
    assert retried['input_tokens'] is None and retried['output_tokens'] is None
    assert first['source_refs'] == retried['source_refs']


def test_invalid_answer_usage_survives_retry_and_final_audit(repo,model,monkeypatch):
    from app.model import MiniMax
    transport=MiniMax(repo.settings)
    attempts=[]
    def request(*args,**kwargs):
        attempts.append(1)
        if len(attempts)==1:
            return {'stop_reason':'max_tokens','content':[{'type':'text','text':'synthetic partial answer'}],
                    'usage':{'input_tokens':411,'output_tokens':23,'body':'synthetic private body'}}
        return {'stop_reason':'end_turn','content':[{'type':'text','text':'合成完整回答。'}],
                'usage':{'input_tokens':412,'output_tokens':24}}
    monkeypatch.setattr(transport,'_request',request)
    monkeypatch.setattr(model,'chat',transport.chat)
    result=MemoryService(repo,FakeEmbeddings(),model).chat('合成回答协议重试。')
    assert [call['phase'] for call in result['calls']]==['intake','answer','answer']
    failed=result['calls'][1]
    assert failed['error']=='model_invalid_response' and failed['usage']=={'input_tokens':411,'output_tokens':23}
    assert len(attempts)==2 and counts(repo)['experiences']==1
    assert 'synthetic private body' not in str(result['calls'])
    with repo.pool.connection() as conn:
        rows={row['id']:row for row in conn.execute('SELECT * FROM model_calls').fetchall()}
    assert rows[failed['id']]['input_tokens']==411 and rows[failed['id']]['output_tokens']==23
    assert rows[result['calls'][-1]['id']]['status']=='completed'
    assert rows[result['calls'][-1]['id']]['input_tokens']==412


@pytest.mark.parametrize('phase',['intake','answer'])
def test_final_protocol_failure_preserves_usage_without_committing(repo,model,monkeypatch,phase):
    from app.model import MiniMax
    transport=MiniMax(repo.settings)
    attempts=[]
    def request(*args,**kwargs):
        attempts.append(1)
        return {'stop_reason':'max_tokens','content':[],
                'usage':{'input_tokens':501,'output_tokens':len(attempts)}}
    monkeypatch.setattr(transport,'_request',request)
    monkeypatch.setattr(model,'decide_memories' if phase=='intake' else 'chat',
                        transport.decide_memories if phase=='intake' else transport.chat)
    before=counts(repo)
    with pytest.raises(MemoryError):
        MemoryService(repo,FakeEmbeddings(),model).chat('合成协议持续失败。')
    assert len(attempts)==2 and counts(repo)==before
    with repo.pool.connection() as conn:
        rows=conn.execute('SELECT * FROM model_calls WHERE phase=%s',(phase,)).fetchall()
    assert len(rows)==2 and {row['input_tokens'] for row in rows}=={501}
    assert {row['output_tokens'] for row in rows}=={1,2}
    assert all(row['status'] in ('memory_decision_invalid','model_invalid_response') for row in rows)


def test_answer_timeout_retries_once_with_three_recorded_calls(repo, model, monkeypatch):
    original = model.chat
    def flaky(*args):
        result = original(*args)
        if len(model.requests) == 1:
            raise MemoryError('model_timeout', '合成瞬时超时', 504)
        return result
    monkeypatch.setattr(model, 'chat', flaky)
    result = MemoryService(repo, FakeEmbeddings(), model).chat('合成回答重试经历。')
    assert [call['phase'] for call in result['calls']] == ['intake', 'answer', 'answer']
    assert result['calls'][1]['status'] == 'failed'
    assert result['calls'][1]['error'] == 'model_timeout'
    assert result['calls'][1]['elapsed_seconds'] >= 0
    assert result['calls'][1]['usage']=={}
    with repo.pool.connection() as conn:
        failed=conn.execute('SELECT input_tokens,output_tokens FROM model_calls WHERE id=%s',
                            (result['calls'][1]['id'],)).fetchone()
    assert failed['input_tokens'] is None and failed['output_tokens'] is None
    assert model.requests[0] == model.requests[1]
    assert result['history_saved'] and counts(repo)['experiences'] == 1


@pytest.mark.parametrize('phase', ['intake', 'answer'])
def test_repeated_transient_failure_stops_once_without_partial_commit(repo, model, monkeypatch, phase):
    before = counts(repo)
    attempts = []
    def failed(*args):
        attempts.append(args)
        if phase == 'intake':
            raise MemoryError('memory_decision_invalid', '合成协议错误', 502) from ValueError('invalid_tool')
        raise MemoryError('model_timeout', '合成超时', 504)
    monkeypatch.setattr(model, 'decide_memories' if phase == 'intake' else 'chat', failed)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo, FakeEmbeddings(), model).chat('合成连续失败经历。')
    assert exc.value.code == ('memory_decision_invalid' if phase == 'intake' else 'model_timeout')
    assert len(attempts) == 2 and counts(repo) == before
    with repo.pool.connection() as conn:
        rows = conn.execute('SELECT phase,status FROM model_calls').fetchall()
    assert len(rows) == (2 if phase == 'intake' else 3)
    assert sum(row['status'] == exc.value.code for row in rows) == 2
    assert all(row['status'] != 'running' for row in rows)


def test_intake_retry_reserves_answer_and_blocks_unaffordable_reassessment(repo, model, monkeypatch):
    service, _ = expanded_service(repo, model, monkeypatch)
    before = counts(repo)
    original = model.decide_memories
    attempts = []
    def flaky(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise MemoryError('model_unavailable', '合成暂时断网', 502)
        return original(*args)
    monkeypatch.setattr(model, 'decide_memories', flaky)
    with repo.pool.connection() as conn:
        previous_calls = conn.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']
    result = service.chat('那个礼物多少钱？')
    assert result['memory_status'] == 'failed' and not result['history_saved']
    assert len(attempts) == 2 and model.reassessment_requests == [] and len(model.requests) == 1
    assert len(result['calls']) == 3
    assert counts(repo) == before
    with repo.pool.connection() as conn:
        assert conn.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n'] == previous_calls + 3


def test_invalid_local_quote_is_rejected_on_both_proposals_without_writes(repo, model):
    model.decision = {'actions': [{'operation': 'create', 'quote': '输入中没有的原话',
                                  'text': '合成个人事实', 'confidence': 1}]}
    before = counts(repo)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo, FakeEmbeddings(), model).chat('完全不同的当前输入。')
    assert exc.value.code == 'memory_decision_invalid'
    assert len(model.decision_requests) == 2 and model.requests == []
    prompts=[json.loads(value) for value in model.decision_requests]
    assert prompts[0]['current_question']==prompts[1]['current_question']
    assert prompts[1]['proposal_errors']==[{'code':'action_quote_not_in_current_source','path':['actions','quote']}]
    with repo.pool.connection() as c:
        calls=c.execute('SELECT phase,status FROM model_calls').fetchall()
    assert len(calls)==2 and all(row['phase']=='intake' and row['status']=='memory_decision_invalid' for row in calls)
    assert counts(repo) == before


@pytest.mark.parametrize('code', ['model_rate_limited', 'context_changed', 'memory_decision_invalid'])
def test_nontransient_intake_failure_is_not_retried(repo, model, monkeypatch, code):
    attempts = []
    def failed(*args):
        attempts.append(args)
        raise MemoryError(code, '合成不可重试失败', 503)
    monkeypatch.setattr(model, 'decide_memories', failed)
    before = counts(repo)
    with pytest.raises(MemoryError) as exc:
        MemoryService(repo, FakeEmbeddings(), model).chat('合成禁止重试检查。')
    assert exc.value.code == code
    assert len(attempts) == 1 and counts(repo) == before and model.requests == []


def test_reassessment_timeout_is_not_retried(repo, model, monkeypatch):
    service, _ = expanded_service(repo, model, monkeypatch)
    before = counts(repo)
    attempts = []
    def failed(*args):
        attempts.append(args)
        raise MemoryError('model_timeout', '合成重评超时', 504)
    monkeypatch.setattr(model, 'reassess_memories', failed)
    with pytest.raises(MemoryError) as exc:
        service.chat('那个礼物多少钱？')
    assert exc.value.code == 'model_timeout'
    assert len(attempts) == 1 and model.requests == [] and counts(repo) == before


def test_retry_rechecks_deadline_and_epoch_before_next_request(repo, model, monkeypatch):
    import app.service as module
    for changed in ('deadline', 'epoch'):
        with monkeypatch.context() as patch:
            tick = [100.0]
            epoch = [repo.epoch()]
            patch.setattr(module, 'monotonic', lambda: tick[0])
            patch.setattr(repo, 'epoch', lambda: epoch[0])
            attempts = []
            def failed(*args):
                attempts.append(args)
                if changed == 'deadline':
                    tick[0] += repo.settings.chat_deadline_seconds + 1
                else:
                    epoch[0] += 1
                raise MemoryError('model_timeout', '合成超时', 504)
            patch.setattr(model, 'decide_memories', failed)
            before = counts(repo)
            with pytest.raises(MemoryError) as exc:
                MemoryService(repo, FakeEmbeddings(), model).chat('合成重试前状态检查。')
            assert exc.value.code == ('chat_deadline' if changed == 'deadline' else 'context_changed')
            assert len(attempts) == 1 and counts(repo) == before and model.requests == []


def test_preview_and_episode_routes_are_read_only_and_production_has_no_lab(client, repo):
    chat = client.post('/v1/chat', json={'text': '合成经历：今天在松风公园买了矿泉水。'}).json()
    before = counts(repo)
    episodes = client.get('/v1/episodes').json()
    assert episodes
    eid = chat['episode_ids'][0]
    assert client.get('/v1/episodes/' + eid).status_code == 200
    response = client.post('/v1/recall/preview', json={'query': '松风公园', 'recall_mode': 'deep'})
    assert response.status_code == 200 and response.json()['read_only']
    assert counts(repo) == before
    assert client.get('/v1/lab/scenarios').status_code == 404
    assert client.get('/v1/episodes', headers={'Authorization': 'Bearer wrong'}).status_code == 401
