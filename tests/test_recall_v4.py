"""Pure policy tests: no database, network, provider or production records."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from app.context import ContextComposer, content_hash, estimate_tokens
from app.errors import MemoryError
from app.recall import RecallEngine, activation_components, detail_kind, present_episode


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


class Clock:
    def now(self):
        return NOW


class Episodes:
    def __init__(self, rows, associated=()):
        self.rows = deepcopy(rows)
        self.associated = deepcopy(list(associated))
        self.clock = Clock()
        self.calls = []

    def episode_candidates(self, query, **kwargs):
        self.calls.append(('candidate', query, kwargs))
        return deepcopy(self.rows)

    def episode_associated(self, ids, **kwargs):
        self.calls.append(('associated', ids, kwargs))
        return deepcopy(self.associated)


def episode(title='公园散步', days=0, importance=.2, details=None, **fields):
    source = str(uuid4())
    return {'id': str(uuid4()), 'revision': 1, 'title': title, 'gist': title,
        'context_type': 'reported', 'recorded_at': NOW - timedelta(days=days),
        'occurred_at': None, 'last_rehearsed_at': None, 'importance': importance,
        'emotional_salience': 0, 'source_experience_ids': [source],
        'details': details or [], 'cues': [], 'matched_cues': [], 'methods': ['keyword'],
        'similarity': 0, **fields}


def detail(quote, kind, importance=.2, **fields):
    return {'id': str(uuid4()), 'quote': quote, 'kind': kind, 'meaning': '',
        'importance': importance, 'emotional_salience': 0,
        'experience_id': str(uuid4()), 'role': 'user', 'start': 20,
        'end': 20 + len(quote), 'source_revision': 1, **fields}


def test_recent_weak_cue_wakes_but_old_ordinary_event_stays_dormant():
    recent, old = episode(days=1), episode(days=2000)
    result = RecallEngine(Episodes([old, recent]), None).preview('公园')
    assert [row['id'] for row in result['selected']] == [recent['id']]
    assert next(row for row in result['candidates'] if row['id'] == old['id'])['selection_reason'] == 'below_activation_threshold'


def test_old_event_awakens_with_distinctive_cue_without_embedding():
    old = episode('多年以前的告白', days=3000, importance=.8,
        cues=[{'kind': 'object', 'text': '紫藤戒指', 'quote': '那枚紫藤戒指'}],
        matched_cues=[{'kind': 'object', 'text': '紫藤戒指', 'quote': '那枚紫藤戒指', 'frequency': 1}])
    result = RecallEngine(Episodes([old]), None).preview('紫藤戒指')
    assert result['selected'][0]['id'] == old['id']
    assert result['selected'][0]['distinctive_cue']
    assert result['embedding_status'] == 'unavailable'


def test_multiple_matching_cues_can_awaken_old_event():
    old = episode('公园雨天火锅', days=3000)
    result = RecallEngine(Episodes([old]), None).preview('公园雨天火锅')
    assert result['selected'][0]['id'] == old['id']


def test_deep_mode_exposes_weakly_matching_old_event_without_changing_it():
    old = episode(days=3000)
    repo = Episodes([old])
    before = deepcopy(repo.rows)
    natural = RecallEngine(repo, None).preview('公园')
    deep = RecallEngine(repo, None).preview('公园', mode='deep')
    assert not natural['selected']
    assert deep['selected'][0]['id'] == old['id']
    assert repo.rows == before


def test_activation_half_life_is_importance_dependent_and_future_time_is_clamped():
    assert activation_components(episode(importance=0), '公园', NOW)['half_life_days'] == 7
    assert activation_components(episode(importance=1), '公园', NOW)['half_life_days'] == 365
    future = episode(recorded_at=NOW + timedelta(days=10))
    assert activation_components(future, '公园', NOW)['base_activation'] == 1


def test_preview_does_not_rehearse_or_inflate_emotion_on_repeated_reads():
    row = episode(importance=.8, emotional_salience=.7)
    repo = Episodes([row])
    engine = RecallEngine(repo, None)
    first = engine.preview('公园')
    for _ in range(100):
        again = engine.preview('公园')
        assert again['manifest_hash'] == first['manifest_hash']
        assert again['candidates'] == first['candidates']
    assert repo.rows == [row]


def test_amount_is_dormant_during_normal_recall_but_exact_when_requested():
    money = detail('小吃一共花了37元', 'amount')
    action = detail('我单膝跪下，亲了一下你的手', 'action', .9)
    row = episode('公园告白', details=[money, action])
    engine = RecallEngine(Episodes([row]), None)
    ordinary = engine.preview('公园告白')
    assert money['quote'] not in str(ordinary['evidence'])
    assert action['quote'] in str(ordinary['evidence'])
    exact = engine.preview('公园那天小吃花了多少钱')
    selected_money = next(d for e in exact['evidence'] for d in e['details'] if d['id'] == money['id'])
    assert selected_money['quote'] == '小吃一共花了37元'
    assert selected_money['start'] == money['start'] and selected_money['end'] == money['end']
    assert exact['evidence'][0]['level'] == 'fragment'


def test_explicit_detail_kind_preserves_speech_and_action_in_mixed_amount_unit():
    quote = '我们花了37元买花，你说我爱你，然后单膝下跪给我戴上戒指。'
    source = str(uuid4())
    speech = detail(quote, 'speech', .9, experience_id=source)
    action = detail(quote, 'action', .9, experience_id=source)
    amount = detail(quote, 'amount', experience_id=source)
    row = episode('公园告白', details=[amount, speech, action])
    assert detail_kind(speech) == 'quote' and detail_kind(action) == 'action'
    for query, requested in [('当时说了什么原话？', speech), ('当时做了什么动作？', action)]:
        presented = present_episode(row, query, {'activation_score': .9})
        assert requested['id'] in presented['required_detail_ids']
        assert amount['id'] not in {item['id'] for item in presented['details']}
        packed = ContextComposer().compose(current_input=query, episodic=[presented])
        evidence = packed['payload']['experience_evidence'][0]
        assert evidence['details'][0]['quote'] == quote
        assert len(evidence['details']) == 1
        assert evidence['required_detail_ids'] == [evidence['details'][0]['id']]


def test_untyped_amount_detection_still_applies_only_to_fallback_details():
    quote = '在37元花店单膝跪下。'
    for kind in ('amount', 'money', 'price', 'expense', 'fact', 'other', ''):
        assert detail_kind({'kind': kind, 'quote': quote}) == 'amount'
    for kind in ('person', 'place', 'object', 'time', 'sensory'):
        assert detail_kind({'kind': kind, 'quote': quote}) == kind
    assert detail_kind({'kind': 'fact', 'quote': '单膝跪下。'}) == 'action'


def test_two_similar_events_are_marked_ambiguous_not_merged():
    rows = [episode('公园告白', details=[detail('花了37元', 'amount')]),
            episode('公园告白', details=[detail('花了81元', 'amount')])]
    result = RecallEngine(Episodes(rows), None).preview('公园告白')
    assert len(result['ambiguities']) == 1
    assert len(result['evidence']) == 2
    assert all(row['ambiguity'] for row in result['evidence'])
    assert len({row['episode_id'] for row in result['evidence']}) == 2


def test_association_expands_only_one_hop_and_does_not_awaken_unrelated_event():
    first = episode('紫藤公园告白')
    second = episode('紫藤公园承诺')
    irrelevant = episode('数学考试安排')
    repo = Episodes([first], [second, irrelevant])
    result = RecallEngine(repo, None).preview('紫藤公园')
    assert sum(call[0] == 'associated' for call in repo.calls) == 1
    assert second['id'] in {row['id'] for row in result['selected']}
    assert irrelevant['id'] not in {row['id'] for row in result['selected']}
    assert next(row for row in result['candidates'] if row['id'] == second['id'])['association_hops'] == 1


def test_expansion_uses_original_question_for_detail_presentation():
    money = detail('小吃花了37元', 'amount')
    row = episode('公园的下午', details=[money])
    class Expanded(Episodes):
        def episode_candidates(self, query, **kwargs):
            return [] if query == '公园小吃花了多少钱' else deepcopy(self.rows)
    result = RecallEngine(Expanded([row]), None).preview('公园小吃花了多少钱', extra_queries=['公园'])
    assert result['evidence'][0]['details'][0]['quote'] == money['quote']
    assert result['query'] == '公园小吃花了多少钱'


def test_composer_budgets_whole_payload_and_never_slices_fact_or_quote():
    quotes = [detail('我爱你' * 100, 'quote', .9)]
    row = {'id': 'episode', 'revision': 1, 'level': 'fragment', 'title': '告白',
        'gist': '告白经过', 'details': quotes, 'required_detail_ids': [quotes[0]['id']]}
    fact = {'id': 'fact', 'revision': 1, 'text': '完整事实' * 1000}
    result = ContextComposer(2200).compose(system='规则', tools=[{'name': 'tool', 'description': '工具'}],
        schema={'properties': {'answer': {'type': 'string'}}}, current_input='那句话是什么？',
        facts=[fact], episodic=[row], required={'planned_memory_changes': []})
    assert result['budget']['estimated_tokens'] == estimate_tokens(result['payload'])
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']
    assert result['payload']['memory_evidence'] == []
    assert result['payload']['experience_evidence'][0]['details'][0]['quote'] == quotes[0]['quote']
    assert result['omitted'][0]['id'] == 'fact'
    assert result['budget']['provider_usage'] is None


def test_required_payload_overflow_is_explicit_and_not_truncated():
    with pytest.raises(MemoryError) as failure:
        ContextComposer(100).compose(current_input='必须完整保留' * 100)
    assert failure.value.code == 'context_budget_exceeded'
    assert failure.value.status == 413


def test_history_turns_are_atomic_and_deduplicated_facts_do_not_use_extra_budget():
    history = [{'role': 'user', 'content': '问题' * 500}, {'role': 'assistant', 'content': '回答' * 500}]
    fact = {'id': 'fact', 'revision': 1, 'text': '用户喜欢茶'}
    result = ContextComposer(1000).compose(current_input='你好', history=history, facts=[fact, fact])
    assert result['payload']['history'] == []
    assert result['payload']['memory_evidence'] == [fact]
    assert result['deduplicated'][0]['reason'] == 'duplicate_source'


def test_relevant_belief_survives_history_and_episode_budget_pressure_whole():
    query = '你对承诺的意义已经形成了什么看法？请说明理由。'
    belief = {'id': 'commitment-belief', 'revision': 3, 'domain': 'belief', 'state_key': '承诺与行动',
        'text': '承诺要落实为持续行动。' + '需要核对后续行动与最初表达是否一致。' * 40}
    history = [{'role': 'user', 'content': '无关的旧聊天经过。' * 40,
                'experience_id': 'recent-history'}]
    events = [{'id': f'event-{index}', 'title': '普通回忆', 'gist': '花草与天气。' * 50,
               'level': 'gist', 'details': [], 'activation_score': .9} for index in range(12)]
    standalone = ContextComposer().compose(current_input=query, self_state=[belief])
    composer = ContextComposer(standalone['budget']['estimated_tokens'], safety_margin=0)
    assert composer.compose(current_input=query, history=history)['payload']['history'] == history
    result = composer.compose(current_input=query, history=history, episodic=events, self_state=[belief])
    assert result['payload']['self_state'] == [belief]
    assert result['payload']['history'] == []
    assert result['payload']['experience_evidence'] == []
    assert any(row['source_type'] == 'self' and row['id'] == belief['id']
               and row['revision'] == 3 and row['hash'] == content_hash(belief) for row in result['manifest'])
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']


def test_generic_opinion_words_do_not_promote_unrelated_self_above_evidence():
    query = '你对承诺已经形成了什么看法？请说明观点和理由。'
    unrelated = {'id': 'coffee-belief', 'revision': 1, 'domain': 'belief', 'state_key': '咖啡偏好',
        'text': '我已经形成了这样的观点：咖啡偏好可以不同。我觉得应该说明理由。'}
    quote = detail('公园承诺的完整经过。' * 30, 'speech', .9)
    event = {'id': 'commitment-event', 'title': '公园承诺', 'gist': '一次承诺', 'level': 'anchor',
             'details': [quote], 'activation_score': .8}
    standalone = ContextComposer().compose(current_input=query, episodic=[event])
    composer = ContextComposer(standalone['budget']['estimated_tokens'], safety_margin=0)
    assert composer.compose(current_input=query, self_state=[unrelated])['payload']['self_state'] == [unrelated]
    result = composer.compose(current_input=query, self_state=[unrelated], episodic=[event])
    assert result['payload']['self_state'] == []
    assert result['payload']['experience_evidence'][0]['details'][0]['quote'] == quote['quote']
    assert not any(row['source_type'] == 'self' for row in result['manifest'])


def test_required_exact_detail_still_precedes_relevant_self_state():
    query = '那份承诺礼物花了多少钱？'
    belief = {'id': 'commitment-belief', 'revision': 1, 'state_key': '承诺',
              'text': '承诺应当体现在持续行动中。'}
    quote = detail('承诺礼物花了37元。' * 30, 'amount')
    event = {'id': 'gift', 'title': '承诺礼物', 'gist': '礼物金额', 'level': 'fragment',
             'details': [quote], 'required_detail_ids': [quote['id']]}
    standalone = ContextComposer().compose(current_input=query, episodic=[event])
    result = ContextComposer(standalone['budget']['estimated_tokens'], safety_margin=0).compose(
        current_input=query, episodic=[event], self_state=[belief])
    assert result['payload']['experience_evidence'][0]['details'][0]['quote'] == quote['quote']
    assert result['payload']['self_state'] == []


def test_manifest_changes_when_same_source_presented_span_or_level_changes():
    one = detail('我爱你', 'quote', .9)
    row = {'id': 'episode', 'revision': 1, 'level': 'anchor', 'title': '告白', 'gist': '一次告白', 'details': [one]}
    composer = ContextComposer()
    first = composer.compose(current_input='告白', episodic=[row])
    changed = deepcopy(row)
    changed['details'][0]['start'] += 1
    second = composer.compose(current_input='告白', episodic=[changed])
    assert first['manifest_hash'] != second['manifest_hash']
    changed = deepcopy(row)
    changed['level'] = 'fragment'
    third = composer.compose(current_input='告白', episodic=[changed])
    assert first['manifest_hash'] != third['manifest_hash']
    assert any(entry['source_type'] == 'experience' for entry in first['manifest'])


def test_manifest_ignores_activation_scores_and_detail_order():
    details = [detail('我爱你', 'quote', .9), detail('亲了一下手', 'action', .8)]
    row = {'id': 'episode', 'revision': 1, 'level': 'anchor', 'title': '告白',
        'gist': '一次告白', 'details': details, 'activation_score': .8}
    composer = ContextComposer()
    first = composer.compose(current_input='告白', episodic=[row])
    changed = deepcopy(row)
    changed['activation_score'] = .7
    changed['details'].reverse()
    second = composer.compose(current_input='告白', episodic=[changed])
    assert first['manifest_hash'] == second['manifest_hash']


def test_history_dedup_requires_same_source_and_keeps_supported_source_trace():
    quote = detail('我爱你', 'quote', .9)
    other_quote = detail('我爱你', 'quote', .9)
    rows = [{'id': 'first', 'level': 'anchor', 'gist': '第一次告白', 'details': [quote]},
            {'id': 'second', 'level': 'anchor', 'gist': '第二次告白', 'details': [other_quote]}]
    history = [{'role': 'user', 'experience_id': quote['experience_id'], 'content': '当时说了我爱你。'}]
    result = ContextComposer().compose(current_input='告白', history=history, episodic=rows)
    one, two = result['payload']['experience_evidence']
    assert one['details'] == []
    assert one['supported_source_experience_ids'] == [quote['experience_id']]
    assert two['details'][0]['quote'] == other_quote['quote']
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']


def test_estimator_and_hash_are_deterministic_and_include_serialized_structure():
    first = {'汉字': '我爱你', 'a': [1, 2]}
    second = {'a': [1, 2], '汉字': '我爱你'}
    assert estimate_tokens(first) == estimate_tokens(second)
    assert content_hash(first) == content_hash(second)
    assert estimate_tokens(first) > estimate_tokens('我爱你')


def test_preview_returns_source_experience_ids_separately_from_episode_id():
    quote = detail('我爱你', 'quote', .9)
    row = episode('公园告白', details=[quote], source_experience_ids=[quote['experience_id']])
    result = RecallEngine(Episodes([row]), None).preview('公园告白时原话怎么说')
    evidence = result['evidence'][0]
    assert evidence['episode_id'] == row['id']
    assert evidence['source_experience_ids'] == [quote['experience_id']]
    assert evidence['sources'][0]['id'] == quote['experience_id']
    assert evidence['sources'][0]['id'] != evidence['episode_id']


def test_private_or_stale_candidates_never_appear_in_preview_trace():
    private = episode('公园私事', allow_cloud=False)
    stale = episode('公园旧事', context_stale=True)
    result = RecallEngine(Episodes([private, stale]), None).preview('公园')
    assert result['candidates'] == []
    assert result['evidence'] == []


def test_association_does_not_escape_explicit_time_filter():
    seed = episode('公园告白', days=1)
    outside = episode('公园承诺', days=100)
    result = RecallEngine(Episodes([seed], [outside]), None).preview('公园', after=NOW-timedelta(days=2))
    assert outside['id'] not in {row['id'] for row in result['candidates']}


def test_legacy_projection_retains_experience_identity_without_inventing_episode_source():
    quote = detail('我爱你', 'quote', .9)
    row = episode('公园告白', details=[quote], legacy_projection=True,
        id=quote['experience_id'], source_experience_ids=[quote['experience_id']])
    result = RecallEngine(Episodes([row]), None).preview('公园告白时的原话')
    assert result['evidence'][0]['legacy_projection']
    assert not any(entry['source_type'] == 'episode' for entry in result['manifest'])
    assert all(entry['id'] == quote['experience_id'] for entry in result['manifest'])


def test_explicit_recall_plan_resolves_deictic_question_without_replacing_detail_intent():
    money = detail('小吃花了37元', 'amount')
    row = episode('紫藤公园', days=3000, details=[money],
        matched_cues=[{'kind': 'place', 'text': '紫藤公园', 'quote': '紫藤公园', 'frequency': 1}])
    result = RecallEngine(Episodes([row]), None).preview('那次花了多少钱', extra_queries=['紫藤公园'])
    assert 'planned_context_cue' in result['selected'][0]['reasons']
    assert result['evidence'][0]['details'][0]['quote'] == money['quote']
    unrelated = RecallEngine(Episodes([row]), None).preview('讲讲天气预报', extra_queries=['紫藤公园'])
    assert unrelated['selected'] == []


def test_requested_exact_detail_wins_over_ordinary_history_at_full_budget():
    amount = detail('小吃花了37元', 'amount')
    event = {'id': 'amount-event', 'revision': 1, 'title': '公园的小吃', 'gist': '买过小吃',
        'level': 'fragment', 'details': [amount], 'required_detail_ids': [amount['id']]}
    history = [{'role': 'user', 'content': '普通散步' * 200}]
    composer = ContextComposer(1800)
    history_only = composer.compose(current_input='花了多少元？', history=history)
    assert history_only['payload']['history'] == history
    result = composer.compose(current_input='花了多少元？', history=history, episodic=[event])
    assert result['payload']['history'] == []
    assert result['payload']['experience_evidence'][0]['details'][0]['quote'] == '小吃花了37元'
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']


def test_wire_cost_estimator_removes_optional_evidence_before_request_overflows():
    def wire_cost(payload):
        return estimate_tokens({'model': 'M3', 'max_tokens': 2048,
            'system': payload['system'], 'messages': [{'role': 'user',
                'content': json.dumps({key: value for key, value in payload.items() if key != 'system'}, ensure_ascii=False)}]})
    large = {'id': 'large', 'revision': 1, 'text': '"quoted"\\text\n' * 150}
    small = {'id': 'small', 'revision': 1, 'text': '简短事实'}
    plain = ContextComposer().compose(current_input='问题', facts=[large])
    budget = int((plain['budget']['estimated_tokens'] + 20)/.8) + 1
    assert wire_cost(plain['payload']) > int(budget * .8)
    result = ContextComposer(budget, cost_estimator=wire_cost).compose(current_input='问题', facts=[large, small])
    assert result['payload']['memory_evidence'] == [small]
    assert result['budget']['estimated_tokens'] == wire_cost(result['payload'])
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']
    assert result['budget']['estimation_scope'] == 'request_payload'
    assert result['omitted'][0]['id'] == 'large'


def test_recall_preview_passes_wire_cost_estimator_to_composer():
    calls = []
    def wire_cost(payload):
        calls.append(1)
        return estimate_tokens(payload) + 300
    result = RecallEngine(Episodes([episode()]), None).preview('公园', budget_tokens=1000, cost_estimator=wire_cost)
    assert len(calls) >= 3
    assert result['budget']['estimated_tokens'] == wire_cost(result['payload'])
    assert result['budget']['estimated_tokens'] <= result['budget']['effective_tokens']


def test_event_time_precision_and_reliability_are_presented_with_interval_overlap():
    seed = episode('公园告白')
    ranged = episode('公园旅行', occurred_at=NOW-timedelta(days=7), occurred_until=NOW+timedelta(days=1),
        time_precision='range', time_quote='那一周', source_reliability=.8, encoding_version='v4-fixture')
    result = RecallEngine(Episodes([seed], [ranged]), None).preview('公园', time_basis='occurred_at',
        after=NOW-timedelta(days=1), before=NOW+timedelta(days=1))
    item = next(row for row in result['evidence'] if row['id'] == ranged['id'])
    assert item['time_precision'] == 'range'
    assert item['occurred_until'] == NOW+timedelta(days=1)
    assert item['source_reliability'] == .8
    assert item['encoding_version'] == 'v4-fixture'


def test_contained_spans_merge_with_required_id_mapped_to_complete_parent():
    parent = detail('买小吃花了37元。', 'fact', start=58, end=67)
    child = detail('37元', 'amount', experience_id=parent['experience_id'], start=63, end=66)
    event = {'id': 'one-event', 'level': 'fragment', 'gist': '买了小吃',
        'details': [child, parent], 'required_detail_ids': [child['id']]}
    result = ContextComposer().compose(current_input='花了多少元？', episodic=[event])
    shown = result['payload']['experience_evidence'][0]
    assert len(shown['details']) == 1
    assert shown['details'][0]['quote'] == '买小吃花了37元。'
    assert shown['required_detail_ids'] == [parent['id']]
    assert shown['details'][0]['covered_detail_ids'] == [child['id']]
    assert result['deduplicated'][0]['reason'] == 'verified_contained_span'


def test_same_quote_at_different_occurrences_keeps_distinct_evidence():
    parent = detail('买小吃花了37元。', 'fact', start=58, end=67)
    first = detail('37元', 'amount', experience_id=parent['experience_id'], start=63, end=66)
    later = detail('37元', 'amount', experience_id=parent['experience_id'], start=83, end=86)
    event = {'id': 'one-event', 'level': 'fragment', 'details': [first, parent, later],
        'required_detail_ids': [first['id'], later['id']]}
    result = ContextComposer().compose(current_input='两笔分别是多少？', episodic=[event])
    shown = result['payload']['experience_evidence'][0]
    assert {row['id'] for row in shown['details']} == {parent['id'], later['id']}
    assert shown['required_detail_ids'] == [parent['id'], later['id']]


def test_containment_requires_same_role_revision_and_exact_offset_text():
    parent = detail('买小吃花了37元。', 'fact', start=58, end=67)
    for fields in ({'role': 'assistant'}, {'source_revision': 2}, {'quote': '38元'}, {'start': 62, 'end': 65}):
        child = detail('37元', 'amount', experience_id=parent['experience_id'], start=63, end=66)
        child.update(fields)
        event = {'id': 'one-event', 'level': 'fragment', 'details': [parent, child]}
        result = ContextComposer().compose(current_input='花费', episodic=[event])
        assert len(result['payload']['experience_evidence'][0]['details']) == 2


def test_containment_does_not_merge_quotes_across_different_events():
    parent = detail('买小吃花了37元。', 'fact', start=58, end=67)
    child = detail('37元', 'amount', experience_id=parent['experience_id'], start=63, end=66)
    events = [{'id': 'purchase-event', 'level': 'fragment', 'details': [parent]},
              {'id': 'accounting-event', 'level': 'fragment', 'details': [child]}]
    result = ContextComposer().compose(current_input='花费', episodic=events)
    assert [row['details'][0]['quote'] for row in result['payload']['experience_evidence']] == [parent['quote'], child['quote']]


def test_yesterday_timeline_cue_recalls_without_vectors_or_shared_words():
    class Offline:
        def embed(self, texts, query=False):
            raise RuntimeError('synthetic embedding outage')
    yesterday = episode('紫藤戒指', gist='林舟在星河公园把礼物递给阿遥',
        recorded_at=datetime.fromisoformat('2026-10-25T23:00:00+08:00'))
    today = episode('橙色书签', recorded_at=datetime.fromisoformat('2026-10-26T00:00:00+08:00'))
    previous = episode('蓝色围巾', recorded_at=datetime.fromisoformat('2026-10-24T23:59:59+08:00'))
    windows = [{'query': '', 'after': '2026-10-25T00:00:00+08:00',
        'before': '2026-10-26T00:00:00+08:00', 'time_basis': 'recorded_at'}]
    repo = Episodes([yesterday, today, previous], [today, previous])
    result = RecallEngine(repo, Offline()).preview('昨天我们聊了什么？', extra_queries=windows)
    assert result['embedding_status'] == 'unavailable'
    assert [row['id'] for row in result['selected']] == [yesterday['id']]
    assert 'bounded_time_cue' in result['selected'][0]['reasons']
    assert result['selected'][0]['lexical_match'] == 0
    assert {row['id'] for row in result['candidates']} == {yesterday['id']}


def test_time_only_expansion_cannot_awaken_unrelated_non_memory_question():
    row = episode('紫藤戒指', days=1)
    window = {'query': '', 'after': NOW-timedelta(days=2), 'before': NOW, 'time_basis': 'recorded_at'}
    result = RecallEngine(Episodes([row]), None).preview('帮我写一份天气预报', extra_queries=[window])
    assert result['selected'] == []
    assert 'bounded_time_cue' not in result['candidates'][0]['reasons']


def test_unbounded_time_expansion_does_not_become_a_timeline_activation_cue():
    row = episode('紫藤戒指', days=1)
    result = RecallEngine(Episodes([row]), None).preview('昨天我们聊了什么？',
        extra_queries=[{'query': '', 'after': NOW-timedelta(days=2)}])
    assert result['selected'] == []


def test_new_narrative_with_yesterday_keeps_content_matches_recorded_today():
    text = '我们昨天一起讨论了那部电影，你听完我的解释，改变了对结局的看法。'
    row = episode('电影结局的讨论', gist=text, recorded_at=NOW, occurred_at=None)
    window = {'query': '', 'after': NOW-timedelta(days=2), 'before': NOW-timedelta(days=1),
        'time_basis': 'recorded_at'}
    result = RecallEngine(Episodes([row]), None).preview(text, extra_queries=[window])
    assert row['id'] in {item['id'] for item in result['selected']}
    # Even a declarative sentence containing the verb "聊了" is not a
    # request to restrict all content evidence to the previous day.
    narrative = '我们昨天聊了电影结局，你听完我的解释改变了看法。'
    result = RecallEngine(Episodes([row]), None).preview(narrative, extra_queries=[window])
    assert row['id'] in {item['id'] for item in result['selected']}


def test_deep_mode_doubles_candidate_pool_without_expanding_context_budget():
    natural_repo = Episodes([episode()])
    deep_repo = Episodes([episode()])
    natural = RecallEngine(natural_repo, None).preview('公园', extra_queries=['公园'], expand=False)
    deep = RecallEngine(deep_repo, None).preview('公园', mode='deep', extra_queries=['公园'], expand=False)
    assert {call[2]['limit'] for call in natural_repo.calls} == {80}
    assert {call[2]['limit'] for call in deep_repo.calls} == {160}
    assert natural['candidate_limit'] == 80 and deep['candidate_limit'] == 160
    assert natural['budget']['effective_tokens'] == deep['budget']['effective_tokens'] == 19200
    assert len(deep['selected']) <= 12
    capped = Episodes([episode()])
    result = RecallEngine(capped, None, policy={'candidate_limit': 200}).preview('公园', mode='deep', expand=False)
    assert result['candidate_limit'] == 320
    assert capped.calls[0][2]['limit'] == 320
