"""Provider request contracts retain the server's no-write and source gates."""
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from psycopg import sql

from app.automatic import DECISION_SYSTEM, validate_decision
from app.errors import MemoryError
from app.model import MiniMax, structured_tool
from app.reflection_types import (ReflectionProposal, reflection_review_units,
                                  validate_reflection_review)
from app.service import MemoryService, SYSTEM
from conftest import FakeEmbeddings


def update():
    return {'domain': 'belief', 'key': '依据', 'text': '重复不是新证据。',
            'reason': '依据本轮论点', 'quote': '重复不是新证据', 'confidence': .8}


def test_intake_tool_explicitly_prioritizes_nonretention(monkeypatch):
    transport = MiniMax(None)
    captured = {}

    def request(system, messages, tool=None):
        captured.update(system=system, messages=messages, tool=tool)
        return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use',
            'name': 'propose_memories', 'input': {'retain_turn': False, 'actions': [],
                'experience': None, 'self_updates': [], 'episodes': [], 'rehearsals': []}}]}

    monkeypatch.setattr(transport, '_request', request)
    text = '重复不是新证据。本轮不要保存新记录。'
    result = transport.decide_memories(DECISION_SYSTEM, json.dumps({'current_question': text}))
    decision = validate_decision(result['decision'], text, [])
    assert not decision.retain_turn and not decision.self_updates
    schema = captured['tool']['input_schema']
    assert {'retain_turn', 'self_updates'} <= set(schema['required'])
    assert 'retain_turn=false' in schema['properties']['self_updates']['description']
    assert '必须为[]' in schema['properties']['self_updates']['description']
    assert '必须为null' in schema['properties']['retain_turn']['description']
    assert '不留存规则优先' in captured['system']
    assert '不要同时返回 experience、episodes、self_updates' not in captured['system']


@pytest.mark.parametrize('development', [
    {'self_updates': [update()]},
    {'experience': {'title': '观点', 'summary': '讨论证据'}},
    {'episodes': [{'title': '观点', 'gist': '讨论证据', 'quote': '重复不是新证据'}]},
    {'rehearsals': [{'episode_id': str(uuid4()), 'quote': '重复不是新证据'}]},
])
def test_nonretained_development_still_fails_closed(development):
    with pytest.raises(MemoryError) as caught:
        validate_decision({'retain_turn': False, **development}, '重复不是新证据', [])
    assert caught.value.code == 'memory_decision_invalid'
    assert str(caught.value.__cause__) == 'non-retained turn cannot develop self'


def test_nonretained_turn_keeps_explicit_forget_but_rejects_new_facts():
    target = str(uuid4())
    text = '请忘记我喝咖啡，不记录这轮。'
    forget = {'operation': 'forget', 'target_id': target, 'expected_revision': 1,
              'confidence': .9, 'quote': text}
    decision = validate_decision({'retain_turn': False, 'actions': [forget], 'self_updates': []},
                                 text, [{'id': target, 'revision': 1}])
    assert decision.actions[0].operation == 'forget'
    with pytest.raises(MemoryError):
        validate_decision({'retain_turn': False, 'actions': [{'operation': 'create',
            'text': '用户喝咖啡', 'quote': '我喝咖啡', 'confidence': .9}]}, '我喝咖啡', [])


def counts(repo):
    with repo.pool.connection() as conn:
        return {table: conn.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(
            sql.Identifier(table))).fetchone()['n'] for table in
            ('messages', 'experiences', 'memories', 'self_observations', 'episodes', 'episode_rehearsals',
             'context_links', 'episode_evidence', 'memory_evidence', 'events', 'outbox')}


def test_nonretained_invalid_provider_update_writes_nothing(repo, model):
    before = counts(repo)
    model.decision = {'retain_turn': False, 'self_updates': [update()]}
    with pytest.raises(MemoryError) as caught:
        MemoryService(repo, FakeEmbeddings(), model).chat('重复不是新证据。本轮不要保存新记录。')
    assert caught.value.code == 'memory_decision_invalid'
    assert counts(repo) == before
    assert not model.requests


def test_nonretained_answer_receives_no_planned_updates(repo, model):
    before = counts(repo)
    model.decision = {'retain_turn': False, 'actions': [], 'experience': None,
                      'self_updates': [], 'episodes': [], 'rehearsals': []}
    result = MemoryService(repo, FakeEmbeddings(), model).chat('重复不是新证据。本轮不要保存新记录。')
    assert result['history_saved'] is False and counts(repo) == before
    context = json.loads(model.requests[-1][-1]['content'])
    assert context['retain_this_turn'] is False
    assert context['planned_memory_changes'] == context['planned_self_updates'] == []
    assert not model.reassessment_requests
    assert 'role=user原话中的“我”是用户' in SYSTEM
    assert '嵌套引语、作品或转述中的人称按实际说话人与受话人解释' in SYSTEM


@pytest.mark.parametrize('development', [
    {'experience': {}},
    {'experience': {'title': '不应留存的标题', 'summary': '不应留存的轮廓'},
     'self_updates': [update()],
     'episodes': [{'title': '不应留存的事件', 'gist': '讨论证据', 'quote': '重复不是新证据'}],
     'rehearsals': [{'episode_id': str(uuid4()), 'quote': '重复不是新证据'}]},
])
def test_server_executes_explicit_nonretention_before_development_validation(repo, model, development):
    model.decision = {'retain_turn': False, 'actions': [], 'recall': [{'query': '既有依据'}],
                      'clarification': '需要区分哪个旧观点？', **development}
    original = deepcopy(model.decision)
    before, epoch = counts(repo), repo.epoch()
    result = MemoryService(repo, FakeEmbeddings(), model).chat('重复不是新证据。本轮不要保存新记录。')
    assert result['answer'] and result['history_saved'] is False
    assert counts(repo) == before and repo.epoch() == epoch
    assert model.decision == original
    context = json.loads(model.requests[-1][-1]['content'])
    assert context['retain_this_turn'] is False
    assert context['planned_self_updates'] == context['planned_memory_changes'] == []
    assert context['memory_clarification'] is None
    assert '不应留存的' not in json.dumps(model.requests, ensure_ascii=False)
    assert not model.reassessment_requests


@pytest.mark.parametrize('raw', [
    {'retain_turn': True, 'actions': [], 'experience': {}},
    {'retain_turn': 'false', 'actions': [], 'self_updates': [update()]},
    {'retain_turn': False, 'actions': {'item': []}, 'self_updates': [update()]},
    {'retain_turn': False, 'actions': [], 'experience': {}, 'unexpected_permission': True},
    {'retain_turn': False, 'actions': [], 'experience': {}, 'recall': [{'unknown': '不能吞掉'}]},
    {'retain_turn': False, 'actions': [], 'experience': {}, 'clarification': {'unknown': '不能吞掉'}},
    {'retain_turn': False, 'actions': [{'operation': 'create', 'text': '用户喝咖啡',
        'quote': '我喝咖啡', 'confidence': .9}]},
    {'retain_turn': False, 'actions': [{'operation': 'update', 'text': '用户喝咖啡',
        'quote': '我喝咖啡', 'confidence': .9, 'target_id': str(uuid4()), 'expected_revision': 1}]},
    {'retain_turn': False, 'actions': [{'operation': 'forget', 'quote': '我喝咖啡',
        'confidence': .9, 'target_id': str(uuid4()), 'expected_revision': 1}], 'self_updates': [update()]},
])
def test_readonly_branch_does_not_swallow_other_protocol_errors(repo, model, raw):
    before, epoch = counts(repo), repo.epoch()
    model.decision = raw
    with pytest.raises(MemoryError) as caught:
        MemoryService(repo, FakeEmbeddings(), model).chat('我喝咖啡。重复不是新证据。本轮不要保存新记录。')
    assert caught.value.code == 'memory_decision_invalid'
    assert counts(repo) == before and repo.epoch() == epoch
    assert not model.requests


def test_readonly_answer_cannot_inherit_fabricated_amount_from_intake_hint(repo, model):
    question='合成小说：银戒指花了52元，小吃花了37元。不要保存本轮，分别花了多少钱？'
    model.decision={'retain_turn':False,'actions':[],
        'clarification':'另一次是小吃52元，需要用户确认哪个版本。'}
    before=counts(repo)
    result=MemoryService(repo,FakeEmbeddings(),model).chat(question)
    context=json.loads(model.requests[-1][-1]['content'])
    assert context['memory_clarification'] is None
    assert '小吃52元' not in json.dumps(context,ensure_ascii=False)
    assert context['current_question']==question
    assert not result['history_saved'] and counts(repo)==before


def reflection_payload():
    first, second, self_id = (str(uuid4()) for _ in range(3))
    proposals = [{'id': 'p1', 'domain': 'belief', 'key': '第一条', 'text': '第一条认识。',
        'reason': '第一条原文。', 'confidence': .7, 'evidence_ids': [first]},
        {'id': 'p2', 'domain': 'belief', 'key': '第二条', 'text': '第二条认识。',
        'reason': '第二条原文。', 'confidence': .7, 'evidence_ids': [second]}]
    proposal = ReflectionProposal.model_validate({'updates': proposals})
    payload = {'episodes': [{'id': str(uuid4()), 'evidence': [{'id': first}, {'id': second},
        {'id': 'cb7140fc-926b-4829-9b6-b4e80640bf3b'}]}],
        'self_state': [{'id': self_id}], 'target_versions': [{'id': self_id}],
        'proposals': proposals, 'review_units': reflection_review_units(proposal)}
    return payload, proposal, first, second, self_id


@pytest.mark.parametrize('phase,method,name,decision', [
    ('reflection_proposal', 'propose_reflection', 'propose_reflection', {'updates': []}),
    ('reflection_review', 'review_reflection', 'review_reflection', {'reviews': []}),
])
def test_reflection_transport_uses_frozen_id_enums(monkeypatch, phase, method, name, decision):
    payload, _, first, second, self_id = reflection_payload()
    transport, captured = MiniMax(None), {}

    def request(system, messages, tool=None):
        captured['tool'] = tool
        return {'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'name': name, 'input': decision}]}

    monkeypatch.setattr(transport, '_request', request)
    getattr(transport, method)('frozen sources', json.dumps(payload))
    assert captured['tool'] == structured_tool(phase, payload)
    schema = captured['tool']['input_schema']
    if phase == 'reflection_proposal':
        candidates = schema['properties']['updates']['items']['properties']['evidence_ids']['items']['enum']
    else:
        fields = schema['properties']['reviews']['items']['properties']
        assert fields['proposal_id']['enum'] == ['p1', 'p2']
        check = fields['checks']['items']['properties']
        candidates = check['evidence_ids']['items']['enum']
        assert check['unit_id']['enum'] == [unit['unit_id'] for unit in payload['review_units']]
        mapping = json.loads(fields['checks']['description'].split('对应关系：', 1)[1])
        assert mapping['p1']['evidence_ids'] == [first]
        assert mapping['p2']['evidence_ids'] == [second]
    assert set(candidates) == {first, second}
    assert self_id not in candidates and 'cb7140fc-926b-4829-9b6-b4e80640bf3b' not in candidates


def test_global_review_enum_does_not_allow_cross_proposal_sources():
    payload, proposal, first, second, _ = reflection_payload()
    reviews = [{'proposal_id': row.id, 'accept': True, 'reason': '逐句检查',
        'checks': [{'unit_id': unit['unit_id'], 'support': 'supported',
            'evidence_ids': [first if row.id == 'p1' else second], 'note': '原文支持'}
            for unit in payload['review_units'] if unit['proposal_id'] == row.id]}
        for row in proposal.updates]
    assert validate_reflection_review({'reviews': reviews}, proposal)
    reviews[0]['checks'][0]['evidence_ids'] = [second]
    with pytest.raises(MemoryError) as caught:
        validate_reflection_review({'reviews': reviews}, proposal)
    assert caught.value.code == 'reflection_review_invalid'
    assert str(caught.value.__cause__) == 'reflection_review_source_not_in_proposal'
