"""Synthetic reflection contracts without databases, embeddings or cloud calls."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.context import content_hash
from app.episodes import source_hash
from app.errors import MemoryError
from app.model import structured_tool
from app.reflection import ReflectionService
from app.reflection_types import validate_reflection_proposal, validate_reflection_review, reflection_review_units


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def synthetic_event(text='合成经历：承诺需要后续行动。', days=0):
    sid, eid, proof = str(uuid4()), str(uuid4()), str(uuid4())
    digest = source_hash(text, '')
    source = {'id': sid, 'revision': 1, 'user_text': text, 'assistant_text': '',
              'source_hash': digest, 'allow_cloud': True}
    evidence = {'id': proof, 'experience_id': sid, 'role': 'user', 'quote': text,
                'start': 0, 'end': len(text), 'source_revision': 1, 'source_hash': digest}
    event = {'id': eid, 'revision': 1, 'title': '合成承诺', 'gist': '讨论承诺与后续行动',
             'context_type': 'reported', 'importance': .8, 'source_hash': content_hash(evidence),
             'source_experience_ids': [sid], 'recorded_at': NOW-timedelta(days=days),
             'evidence': [evidence], 'details': [], 'allow_cloud': True}
    return event, source


class ReflectionRepo:
    def __init__(self, events=None, related=None, states=None):
        pairs = events if events is not None else [synthetic_event()]
        others = related or []
        self.events = {event['id']: deepcopy(event) for event, _ in [*pairs, *others]}
        self.sources = {source['id']: deepcopy(source) for _, source in [*pairs, *others]}
        self.pending = [event['id'] for event, _ in pairs]
        self.related = [event['id'] for event, _ in others]
        self.states = deepcopy(states or [])
        self.clock = SimpleNamespace(now=lambda: NOW)
        self.settings = SimpleNamespace(model_name='MiniMax-M3', thinking='disabled',
            context_budget_tokens=24000, lab_enabled=False, db_schema='test_reflection')
        self.current_epoch = 1
        self.enabled = False
        self.processed, self.calls, self.logical_steps, self.committed = set(), [], [], []
        self.runs = {}
        self.related_requests = 0
        self.candidate_requests = []
        self.semantic = []

    def epoch(self):
        return self.current_epoch

    def reflection_settings(self):
        pending = bool(set(self.pending)-self.processed)
        return {'enabled': self.enabled, 'eligible': self.enabled and pending,
                'reasons': [] if self.enabled and pending else ['disabled' if not self.enabled else 'no_pending_evidence'],
                'manual_eligible': pending, 'manual_reasons': [] if pending else ['no_pending_evidence']}

    def set_reflection_enabled(self, enabled):
        self.enabled = enabled

    def reflection_candidates(self, limit=12):
        return [deepcopy(self.events[eid]) for eid in self.pending if eid not in self.processed][:limit]

    def episode_associated(self, ids, cloud_only=True, limit=12):
        self.related_requests += 1
        return [deepcopy(self.events[eid]) for eid in self.related][:limit]

    def reflection_targets(self):
        return [deepcopy(row) for row in self.states if not row.get('deleted_at')]

    def reflection_states(self, ids=None):
        return [deepcopy(row) for row in self.states if not row.get('invalidated_at')
                and not row.get('deleted_at') and (ids is None or str(row['id']) in ids)]

    def episode_candidates(self, query, **kwargs):
        self.candidate_requests.append({'query': query, **kwargs})
        return [deepcopy(self.events[eid]) for eid in self.semantic]

    def experience(self, source_id):
        return deepcopy(self.sources[str(source_id)])

    def episode(self, episode_id):
        return deepcopy(self.events[str(episode_id)])

    def enqueue_reflection(self, manifest, expected_epoch, target_versions, input_signature, **kwargs):
        rid = str(uuid4())
        run = {'id': rid, 'status': 'pending', 'source_manifest': deepcopy(manifest),
               'expected_epoch': expected_epoch, 'target_versions': deepcopy(target_versions),
               'input_signature': input_signature, **kwargs}
        self.runs[rid] = run
        return deepcopy(run)

    def claim_reflection(self, run_id=None):
        run = self.runs.get(run_id) if run_id else next((row for row in self.runs.values() if row['status']=='pending'), None)
        if not run or run['status'] != 'pending':
            return None
        run.update(status='running', lease_token=str(uuid4()))
        return deepcopy(run)

    def start_reflection_call(self, job, phase, **kwargs):
        if self.runs[job['id']]['status'] != 'running' or job['expected_epoch'] != self.current_epoch:
            raise MemoryError('context_changed', '合成来源已变化', 409)
        call_id = str(uuid4())
        self.calls.append({'id': call_id, 'phase': phase, 'status': 'running', **kwargs})
        return call_id

    def finish_call(self, call_id, status, usage=None):
        next(row for row in self.calls if row['id']==call_id).update(status=status, usage=usage or {})

    def finish_reflection_step(self, job, phase):
        self.logical_steps.append(phase)

    def save_reflection(self, job, updates, review=None):
        assert (review is not None) == bool(updates)
        assert job['expected_epoch'] == self.current_epoch
        self.committed.extend(deepcopy(updates))
        self.processed.update(job['pending_ids'])
        self.runs[job['id']].update(status='completed', result_ids=[str(uuid4()) for _ in updates], audit=job['audit'])
        return deepcopy(self.runs[job['id']])

    def fail_reflection(self, job, code, status='failed'):
        self.runs[job['id']].update(status=status, error_code=code, audit=job.get('audit', {}))
        return deepcopy(self.runs[job['id']])


class ReflectionModel:
    def __init__(self, empty=False, verdict=True, callback=None):
        self.empty, self.verdict, self.callback = empty, verdict, callback
        self.requests = []

    def request_scope(self, deadline):
        return nullcontext()

    def propose_reflection(self, system, prompt):
        payload = json.loads(prompt)
        self.requests.append(('proposal', payload))
        if self.callback:
            self.callback('proposal')
        evidence = next(row['evidence'][0]['id'] for row in payload['episodes'] if row['pending'])
        updates = [] if self.empty else [{'id': 'p1', 'domain': 'belief', 'key': '承诺与行动',
            'text': '承诺应结合后续行动评价。', 'reason': '新增原文说明了后续行动的重要性。',
            'confidence': .8, 'evidence_ids': [evidence], 'target_id': None, 'expected_revision': 0}]
        return {'decision': {'updates': updates}, 'usage': {'input_tokens': 20, 'output_tokens': 5}}

    def review_reflection(self, system, prompt):
        payload = json.loads(prompt)
        self.requests.append(('review', payload))
        if self.callback:
            self.callback('review')
        return {'decision': {'reviews': [{'proposal_id': row['id'], 'accept': self.verdict,
            'reason': '依据冻结原文进行复核。', 'checks': [
                {'unit_id': unit['unit_id'], 'support': 'interpretation' if self.verdict else 'unsupported',
                 'evidence_ids': row['evidence_ids'], 'note': '合成模型判定，仅测试协议。'}
                for unit in payload['review_units'] if unit['proposal_id']==row['id']]} for row in payload['proposals']]},
            'usage': {'input_tokens': 30, 'output_tokens': 5}}


def execute(repo=None, model=None):
    repo, model = repo or ReflectionRepo(), model or ReflectionModel()
    service = ReflectionService(repo, None, model)
    run = service.enqueue()
    return repo, model, service.execute(run['id'])


def test_preview_is_read_only_and_manual_does_not_require_automatic_switch():
    repo, model = ReflectionRepo(), ReflectionModel()
    service = ReflectionService(repo, None, model)
    before = deepcopy((repo.events, repo.sources, repo.states))
    preview = service.preview()
    assert preview['eligible'] and not service.preview(automatic=True)['eligible']
    assert preview['budget']['estimated_tokens'] <= 19200
    assert preview['payload']['episodes'][0]['evidence'][0]['quote'] == next(iter(repo.sources.values()))['user_text']
    assert (repo.events, repo.sources, repo.states) == before
    assert not repo.runs and not repo.calls and not repo.processed and not model.requests


def test_queue_contains_only_reconstructible_metadata_and_two_stages_freeze_evidence():
    repo, model = ReflectionRepo(), ReflectionModel()
    service = ReflectionService(repo, None, model)
    run = service.enqueue()
    serialized = json.dumps(run, ensure_ascii=False)
    assert 'payload' not in run and 'quote' not in serialized and '承诺需要后续行动' not in serialized
    result = service.execute(run['id'])
    assert result['status'] == 'completed' and len(repo.calls) == 2
    assert model.requests[0][1]['episodes'] == model.requests[1][1]['episodes']
    assert model.requests[0][1]['self_state'] == model.requests[1][1]['self_state']
    assert repo.related_requests == 1
    assert repo.committed[0]['evidence_quote'] == next(iter(repo.sources.values()))['user_text']
    assert repo.processed == set(repo.pending)
    assert service.execute(run['id']) is None and len(repo.calls) == 2


def test_empty_proposal_finishes_with_one_request_and_no_new_state_or_event():
    repo, model, result = execute(model=ReflectionModel(empty=True))
    assert result['status'] == 'completed' and len(repo.calls) == 1
    assert not repo.committed and len(repo.events) == 1 and not repo.states
    assert repo.processed == set(repo.pending)
    assert not ReflectionService(repo, None, model).preview()['eligible']


def test_any_rejected_update_prevents_all_commits_and_consumption():
    repo, _, result = execute(model=ReflectionModel(verdict=False))
    assert result['error_code'] == 'reflection_review_rejected'
    assert len(repo.calls) == 2 and not repo.committed and not repo.processed


def test_model_failure_is_counted_without_retry_or_partial_commit():
    def failure(phase):
        raise MemoryError('model_timeout', '合成超时', 504)
    repo, model, result = execute(model=ReflectionModel(callback=failure))
    assert result['status'] == 'failed' and result['error_code'] == 'model_timeout'
    assert len(repo.calls) == 1 and len(model.requests) == 1
    assert repo.calls[0]['status'] == 'model_timeout' and not repo.processed and not repo.committed


def test_deleted_or_changed_source_after_proposal_cannot_reach_review_or_commit():
    for change in ('epoch', 'permission', 'quote'):
        repo = ReflectionRepo()
        source = next(iter(repo.sources.values()))
        def invalidate(phase):
            if change == 'epoch':
                repo.current_epoch += 1
            elif change == 'permission':
                source['allow_cloud'] = False
            else:
                source['user_text'] = '被改过的合成原文'
        repo, model, result = execute(repo, ReflectionModel(callback=invalidate))
        assert result['status'] == 'cancelled'
        assert len(repo.calls) == 1 and len(model.requests) == 1
        assert repo.calls[0]['usage']['input_tokens'] == 20
        assert not repo.processed and not repo.committed


def test_background_self_is_in_manifest_and_revision_change_cancels_run():
    state = {'id': str(uuid4()), 'revision': 1, 'domain': 'goal', 'state_key': '学习', 'text': '理解承诺。'}
    repo = ReflectionRepo(states=[state])
    service = ReflectionService(repo, None, ReflectionModel(empty=True))
    preview = service.preview()
    assert any(row['source_type']=='self' and row['id']==state['id'] for row in preview['manifest'])
    run = service.enqueue()
    repo.states[0]['revision'] = 2
    result = service.execute(run['id'])
    assert result['status'] == 'cancelled' and not repo.calls and not repo.processed


def test_pending_and_related_limits_include_old_events_without_age_filter():
    repo = ReflectionRepo(events=[synthetic_event(days=9000) for _ in range(20)],
                          related=[synthetic_event(days=15000) for _ in range(20)])
    preview = ReflectionService(repo, None, ReflectionModel()).preview()
    assert len(preview['pending_episode_ids']) == 12
    assert len(preview['episode_ids']) == 24
    assert len({row['id'] for row in preview['manifest'] if row['source_type']=='episode'}) == 24


def test_oversized_whole_event_is_not_cut_or_marked_processed():
    huge, small = synthetic_event('完整不可切的句子' * 5000), synthetic_event()
    repo = ReflectionRepo(events=[huge, small])
    service = ReflectionService(repo, None, ReflectionModel(empty=True))
    preview = service.preview()
    assert preview['pending_episode_ids'] == [small[0]['id']]
    assert any(row['id']==huge[0]['id'] and row['reason']=='budget' for row in preview['omitted'])
    run = service.enqueue()
    assert service.execute(run['id'])['status'] == 'completed'
    assert repo.processed == {small[0]['id']}
    assert repo.sources[huge[1]['id']]['user_text'] == huge[1]['user_text']


def test_reflection_requires_original_pending_evidence_and_preserves_target_version():
    state_id = str(uuid4())
    state = {'id': state_id, 'revision': 2, 'domain': 'belief', 'state_key': '承诺', 'text': '原观点'}
    repo = ReflectionRepo(states=[state], related=[synthetic_event('旧的合成背景')])
    payload = ReflectionService(repo, None, ReflectionModel()).preview()['payload']
    evidence = next(row['evidence'][0]['id'] for row in payload['episodes'] if row['pending'])
    update = {'id': 'p1', 'domain': 'belief', 'key': '承诺', 'text': '新观点', 'reason': '新原文',
              'confidence': .8, 'evidence_ids': [evidence], 'target_id': state_id, 'expected_revision': 2}
    assert validate_reflection_proposal({'updates': [update]}, payload).updates
    old = next(row['evidence'][0]['id'] for row in payload['episodes'] if not row['pending'])
    for changes in ({'evidence_ids': [old]}, {'evidence_ids': [str(uuid4())]},
                    {'expected_revision': 1}, {'target_id': None, 'expected_revision': 0},
                    {'quote': '模型自行抄写不能成为原文依据'}):
        with pytest.raises(MemoryError):
            validate_reflection_proposal({'updates': [{**update, **changes}]}, payload)


def test_review_cannot_modify_add_omit_or_duplicate_proposals():
    repo = ReflectionRepo()
    payload = ReflectionService(repo, None, ReflectionModel()).preview()['payload']
    proposal = validate_reflection_proposal(ReflectionModel().propose_reflection('', json.dumps(payload))['decision'], payload)
    good = {'proposal_id': 'p1', 'accept': True, 'reason': '依据原文', 'checks': [
        {'unit_id': unit['unit_id'], 'support': 'interpretation',
         'evidence_ids': [str(eid) for eid in proposal.updates[0].evidence_ids], 'note': '合成来源判定'}
        for unit in reflection_review_units(proposal)]}
    assert validate_reflection_review({'reviews': [good]}, proposal)
    for reviews in ([], [good, good], [{**good, 'proposal_id': 'p2'}], [{**good, 'text': '偷偷改写'}],
                    [{**good, 'accept': 'true'}]):
        with pytest.raises(MemoryError):
            validate_reflection_review({'reviews': reviews}, proposal)


def test_emotion_reappraisal_has_new_evidence_and_independent_dynamics():
    repo = ReflectionRepo()
    payload = ReflectionService(repo, None, ReflectionModel()).preview()['payload']
    base = {'id': 'p1', 'domain': 'emotion', 'key': '重新理解承诺', 'text': '更平静地理解承诺。',
            'reason': '新叙述表明后续行动缓解了此前担忧，并非重放旧情绪。', 'confidence': .7,
            'evidence_ids': [payload['episodes'][0]['evidence'][0]['id']], 'intensity': .2, 'half_life_hours': 6, 'intensity_direction': 'new'}
    assert validate_reflection_proposal({'updates': [base]}, payload).updates[0].intensity == .2
    for omitted in ('intensity', 'half_life_hours', 'intensity_direction'):
        with pytest.raises(MemoryError):
            validate_reflection_proposal({'updates': [{k:v for k,v in base.items() if k!=omitted}]}, payload)
    with pytest.raises(MemoryError):
        validate_reflection_proposal({'updates': [{**base, 'domain': 'belief'}]}, payload)


def test_review_uses_actual_provider_budget_and_never_silently_drops_frozen_sources():
    repo, model = ReflectionRepo(), ReflectionModel()
    service = ReflectionService(repo, None, model)
    original = service._cost
    service._cost = lambda payload, phase: service.effective_budget+1 if payload.get('proposals') else original(payload, phase)
    run = service.enqueue()
    result = service.execute(run['id'])
    assert result['error_code'] == 'context_budget_exceeded'
    assert len(repo.calls) == 1 and not repo.committed and not repo.processed


def test_mock_lab_keeps_cloud_audit_empty_but_two_logical_steps():
    repo, model = ReflectionRepo(), ReflectionModel()
    repo.settings.lab_enabled, repo.settings.db_schema, model.is_mock = True, 'lab_reflection_core', True
    _, _, result = execute(repo, model)
    assert result['status'] == 'completed' and result['model_mode'] == 'mock'
    assert repo.calls == [] and repo.logical_steps == ['reflection_proposal', 'reflection_review']
    assert result['audit']['calls'] == 2


def test_reflection_tool_schemas_are_bounded_and_do_not_offer_fact_or_quote_writes():
    proposal = structured_tool('reflection_proposal')['input_schema']
    assert proposal['properties']['updates']['maxItems'] == 8
    fields = proposal['properties']['updates']['items']['properties']
    assert 'evidence_ids' in fields and 'quote' not in fields and 'quote_refs' not in fields
    assert set(proposal['properties']) == {'updates'}
    review = structured_tool('reflection_review')['input_schema']
    assert set(review['properties']['reviews']['items']['properties']) == {'proposal_id', 'accept', 'reason', 'checks'}


def test_processed_old_event_can_be_related_by_local_semantics_without_association():
    old, source = synthetic_event('多年以前，约定以后要用实际行动回应。', days=20000)
    old.update(methods=['semantic'], similarity=.81)
    repo = ReflectionRepo()
    repo.events[old['id']], repo.sources[source['id']] = old, source
    repo.semantic, repo.processed = [old['id']], {old['id']}
    class Embeddings:
        calls = []
        def embed(self, texts, query=False):
            self.calls.append((texts, query))
            return [[.1, .2] for _ in texts]
    embeddings = Embeddings()
    before = deepcopy(repo.events)
    preview = ReflectionService(repo, embeddings, ReflectionModel()).preview()
    background = next(row for row in preview['payload']['episodes'] if row['id']==old['id'])
    assert not background['pending'] and background['evidence'][0]['quote']==source['user_text']
    assert old['id'] not in preview['pending_episode_ids']
    assert any(row['id']==source['id'] and row['source_type']=='experience' for row in preview['manifest'])
    assert embeddings.calls[0][1] is True and repo.candidate_requests[0]['vector']==[.1, .2]
    assert 'after' not in repo.candidate_requests[0] and repo.events == before
    assert preview['retrieval']['association_candidates']==0 and preview['retrieval']['hybrid_candidates']==1


def test_recent_only_candidate_does_not_become_reflection_background():
    old, source = synthetic_event('不相关的合成午饭。')
    old['methods'] = ['recent']
    repo = ReflectionRepo()
    repo.events[old['id']], repo.sources[source['id']], repo.semantic = old, source, [old['id']]
    assert old['id'] not in ReflectionService(repo, None, ReflectionModel()).preview()['episode_ids']


def test_invalidated_target_exposes_only_identity_and_revision_and_can_be_revised():
    state = {'id': str(uuid4()), 'revision': 4, 'domain': 'belief', 'state_key': '承诺与行动',
             'text': '已失效正文不可出现在模型请求。', 'reason': '已失效原因不可出现在模型请求。',
             'invalidated_at': NOW.isoformat()}
    class Reappraisal(ReflectionModel):
        def propose_reflection(self, system, prompt):
            result = super().propose_reflection(system, prompt)
            target = json.loads(prompt)['target_versions'][0]
            result['decision']['updates'][0].update(target_id=target['id'], expected_revision=target['revision'])
            return result
    repo, model = ReflectionRepo(states=[state]), Reappraisal()
    service = ReflectionService(repo, None, model)
    preview = service.preview()
    assert preview['payload']['self_state']==[]
    assert preview['target_versions']==[{'id':state['id'],'domain':'belief','key':'承诺与行动','revision':4}]
    assert not any(row['source_type']=='self' for row in preview['manifest'])
    assert state['text'] not in json.dumps(preview,ensure_ascii=False) and state['reason'] not in json.dumps(preview,ensure_ascii=False)
    run = service.enqueue()
    assert service.execute(run['id'])['status']=='completed'
    assert repo.committed[0]['expected_revision']==4 and repo.committed[0]['target_id']==state['id']
    assert repo.committed[0]['source_experience_ids']==[next(iter(repo.sources))]


def test_deleted_target_metadata_is_not_exposed_and_target_change_cancels_before_cloud():
    state = {'id': str(uuid4()), 'revision': 2, 'domain': 'belief', 'state_key': '承诺与行动',
             'text': '失效旧正文', 'invalidated_at': NOW.isoformat()}
    repo = ReflectionRepo(states=[state])
    service = ReflectionService(repo, None, ReflectionModel())
    run = service.enqueue()
    repo.states[0]['deleted_at'] = NOW.isoformat()
    assert service.preview()['target_versions']==[]
    result = service.execute(run['id'])
    assert result['status']=='cancelled' and not repo.calls and not repo.processed


def test_one_accepted_and_one_rejected_proposal_still_rolls_back_entire_round():
    class MixedReview(ReflectionModel):
        def propose_reflection(self, system, prompt):
            result = super().propose_reflection(system, prompt)
            result['decision']['updates'].append({**result['decision']['updates'][0], 'id':'p2', 'key':'另一个认识'})
            return result
        def review_reflection(self, system, prompt):
            result = super().review_reflection(system, prompt)
            result['decision']['reviews'][1]['accept'] = False
            result['decision']['reviews'][1]['checks'][0]['support'] = 'unsupported'
            return result
    repo, _, result = execute(model=MixedReview())
    assert result['error_code']=='reflection_review_rejected' and result['audit']['proposed']==2
    assert len(repo.calls)==2 and not repo.committed and not repo.processed


def test_overall_deadline_rejects_late_first_response_without_second_request():
    import app.reflection as reflection
    original = reflection.monotonic
    instant = [0]
    reflection.monotonic = lambda: instant[0]
    try:
        def late_response(phase):
            instant[0] = 241
        repo, _, result = execute(model=ReflectionModel(callback=late_response))
        assert result['error_code']=='reflection_deadline'
        assert len(repo.calls)==1 and repo.calls[0]['usage']['input_tokens']==20
        assert not repo.committed and not repo.processed
    finally:
        reflection.monotonic = original


def test_reflection_prompts_distinguish_new_formation_and_exact_tool_protocol():
    from app.reflection import PROPOSAL_SYSTEM, REVIEW_SYSTEM
    assert '工具参数为 {"updates":[]}' in PROPOSAL_SYSTEM
    assert '不得裸输出[]' in PROPOSAL_SYSTEM
    assert 'id 使用独立编号 p1、p2' in PROPOSAL_SYSTEM
    assert 'id 填到 target_id、revision 填到 expected_revision' in PROPOSAL_SYSTEM
    assert '不要输出 revision 字段' in PROPOSAL_SYSTEM
    assert '只在 text/reason 中准确表述' in PROPOSAL_SYSTEM
    assert '不要新增 context_type、source_reliability' in PROPOSAL_SYSTEM
    for prompt in (PROPOSAL_SYSTEM, REVIEW_SYSTEM):
        assert '首次形成认识、修订已有认识、收束已完成或暂缓的目标' in prompt
        assert '未展示旧认识不构成拒绝' in prompt


def test_both_reflection_phases_explicitly_reject_synthetic_subject_and_generalization_errors():
    from app.reflection import PROPOSAL_SYSTEM, REVIEW_SYSTEM
    # These are the two concrete semantic failures seen in synthetic acceptance:
    # user tiredness became the AI's emotion; a missing "simple" premise became
    # an exclusive general rule. This is a prompt contract, not a quality score.
    for prompt in (PROPOSAL_SYSTEM, REVIEW_SYSTEM):
        assert 'role=user' in prompt and '叙述主体' in prompt
        assert '疲惫' in prompt and '想独处' in prompt
        assert '不能' in prompt and '用户未表达的内心' in prompt
        assert '自己的评价' in prompt or '自己的评价和情绪' in prompt
        assert '有限反例' in prompt and '排他条件或普遍事实' in prompt
        assert '简单问题' in prompt and '快速回复' in prompt
    assert '不要因其其他部分正确而接受，也不要改写后接受' in REVIEW_SYSTEM


def _unit_review(proposal):
    units = reflection_review_units(proposal)
    return {'reviews': [{'proposal_id': row.id, 'accept': True, 'reason': '合成逐句判定',
        'checks': [{'unit_id': unit['unit_id'], 'support': 'interpretation',
                    'evidence_ids': [str(eid) for eid in row.evidence_ids], 'note': '对应合成原文'}
                   for unit in units if unit['proposal_id']==row.id]} for row in proposal.updates]}


def _two_proposals():
    repo = ReflectionRepo(events=[synthetic_event('合成甲：只整理路线。'), synthetic_event('合成乙：尚未授权订票。')])
    payload = ReflectionService(repo, None, ReflectionModel()).preview()['payload']
    updates = [{'id': f'p{index+1}', 'domain': 'belief', 'key': f'认识{index}',
                'text': '这是有限认识。保留未知。', 'reason': '根据新原文；不增添背景。',
                'confidence': .7, 'evidence_ids': [event['evidence'][0]['id']]}
               for index, event in enumerate(payload['episodes'])]
    return validate_reflection_proposal({'updates': updates}, payload), payload


def test_review_units_preserve_every_character_and_complete_sentence_ranges():
    proposal, _ = _two_proposals()
    proposal.updates[0].text = '完整句。' * 100 + '\r\n' + '尾句没有标点'
    proposal.updates[0].reason = '甲' * 999
    units = reflection_review_units(proposal)
    assert len({unit['unit_id'] for unit in units}) == len(units)
    for update in proposal.updates:
        for field in ('text', 'reason'):
            selected = [unit for unit in units if unit['proposal_id']==update.id and unit['field']==field]
            original = getattr(update, field)
            assert ''.join(unit['text'] for unit in selected) == original
            assert selected[0]['start']==0 and selected[-1]['end']==len(original)
            assert all(original[unit['start']:unit['end']]==unit['text'] for unit in selected)
            assert all(left['end']==right['start'] for left,right in zip(selected,selected[1:]))
    long_unit = next(unit for unit in units if unit['proposal_id']=='p1' and unit['field']=='reason')
    assert long_unit['text']=='甲'*999
    assert validate_reflection_review(_unit_review(proposal), proposal)


def test_review_requires_all_text_and_reason_units_once_without_cross_proposal_sources():
    proposal, _ = _two_proposals()
    good = _unit_review(proposal)
    assert validate_reflection_review(good, proposal)
    bad = []
    omitted = deepcopy(good)
    omitted['reviews'][0]['checks'].pop()
    bad.append(omitted)
    duplicated = deepcopy(good)
    duplicated['reviews'][0]['checks'].append(deepcopy(duplicated['reviews'][0]['checks'][0]))
    bad.append(duplicated)
    foreign_unit = deepcopy(good)
    foreign_unit['reviews'][0]['checks'][0]['unit_id'] = foreign_unit['reviews'][1]['checks'][0]['unit_id']
    bad.append(foreign_unit)
    foreign_source = deepcopy(good)
    foreign_source['reviews'][0]['checks'][0]['evidence_ids'] = foreign_source['reviews'][1]['checks'][0]['evidence_ids']
    bad.append(foreign_source)
    unknown_source = deepcopy(good)
    unknown_source['reviews'][0]['checks'][0]['evidence_ids'] = [str(uuid4())]
    bad.append(unknown_source)
    for raw in bad:
        with pytest.raises(MemoryError) as error:
            validate_reflection_review(raw, proposal)
        assert error.value.code=='reflection_review_invalid'


def test_review_accept_must_agree_with_every_unit_support_verdict():
    proposal, _ = _two_proposals()
    for support in ('unsupported', 'conflict'):
        raw = _unit_review(proposal)
        raw['reviews'][0]['checks'][0]['support'] = support
        with pytest.raises(MemoryError):
            validate_reflection_review(raw, proposal)
        raw['reviews'][0]['accept'] = False
        assert validate_reflection_review(raw, proposal).reviews[0].accept is False
    all_supported = _unit_review(proposal)
    all_supported['reviews'][0]['accept'] = False
    with pytest.raises(MemoryError):
        validate_reflection_review(all_supported, proposal)


def test_single_unsupported_or_conflicting_unit_prevents_all_writes():
    for support in ('unsupported', 'conflict'):
        class UnitRejection(ReflectionModel):
            def review_reflection(self, system, prompt):
                result = super().review_reflection(system, prompt)
                verdict = result['decision']['reviews'][0]
                verdict['checks'][-1]['support'] = support
                verdict['accept'] = False
                return result
        repo, model, result = execute(model=UnitRejection())
        assert result['error_code']=='reflection_review_rejected'
        assert len(repo.calls)==2 and not repo.committed and not repo.processed
        review_payload = model.requests[1][1]
        assert {unit['field'] for unit in review_payload['review_units']}=={'text','reason'}
        assert all(unit['proposal_id']=='p1' for unit in review_payload['review_units'])


def test_review_units_are_in_actual_wire_budget_and_excess_fails_before_second_call():
    class LongerProposal(ReflectionModel):
        def propose_reflection(self, system, prompt):
            result = super().propose_reflection(system, prompt)
            result['decision']['updates'][0]['text'] = '这是一条有限认识。' * 100
            return result
    repo, model = ReflectionRepo(), LongerProposal()
    service = ReflectionService(repo, None, model)
    run = service.enqueue()
    payload = service._rebuild(run)
    proposal = validate_reflection_proposal(LongerProposal().propose_reflection('', json.dumps(payload))['decision'], payload)
    baseline = {**payload, 'proposals': proposal.model_dump(mode='json')['updates']}
    expanded = {**baseline, 'review_units': reflection_review_units(proposal)}
    small, full = service._cost(baseline, 'reflection_review'), service._cost(expanded, 'reflection_review')
    assert full>small and service._cost(payload, 'reflection_proposal')<small
    service.effective_budget = small
    result = service.execute(run['id'])
    assert result['error_code']=='context_budget_exceeded'
    assert len(repo.calls)==1 and not repo.committed and not repo.processed


def test_emotion_direction_uses_current_decayed_intensity_not_historical_value():
    state = {'id': str(uuid4()), 'revision': 2, 'domain': 'emotion', 'state_key': '担忧',
             'text': '此前的担忧。', 'reason': '此前有误会。', 'intensity': .7, 'half_life_hours': 24,
             'created_at': NOW-timedelta(hours=48)}
    payload = ReflectionService(ReflectionRepo(states=[state]), None, ReflectionModel()).preview()['payload']
    assert payload['self_state'][0]['current_intensity']==.175
    base = {'id':'p1','domain':'emotion','key':'担忧','text':'重新评价担忧。','reason':'依据新增原文评价。',
            'confidence':.7,'evidence_ids':[payload['episodes'][0]['evidence'][0]['id']],
            'target_id':state['id'],'expected_revision':2,'half_life_hours':6}
    for intensity,direction in ((.1,'decrease'),(.175,'maintain'),(.3,'increase')):
        assert validate_reflection_proposal({'updates':[{**base,'intensity':intensity,'intensity_direction':direction}]},payload)
    # .4 is lower than the historical .7 but higher than the current .175.
    for intensity,direction in ((.4,'decrease'),(.175,'increase'),(.1,'maintain'),(.175,'new')):
        with pytest.raises(MemoryError):
            validate_reflection_proposal({'updates':[{**base,'intensity':intensity,'intensity_direction':direction}]},payload)
    payload['self_state'] = []  # Invalidated target metadata is not an emotion baseline.
    with pytest.raises(MemoryError):
        validate_reflection_proposal({'updates':[{**base,'intensity':.1,'intensity_direction':'decrease'}]},payload)


def test_new_emotion_requires_new_direction_and_other_domains_forbid_it():
    proposal, payload = _two_proposals()
    base = proposal.updates[0].model_dump(mode='json')
    base.update(domain='emotion',intensity=.2,half_life_hours=6,intensity_direction='new')
    assert validate_reflection_proposal({'updates':[base]},payload)
    for direction in ('decrease','maintain','increase',None):
        with pytest.raises(MemoryError):
            validate_reflection_proposal({'updates':[{**base,'intensity_direction':direction}]},payload)
    with pytest.raises(MemoryError):
        validate_reflection_proposal({'updates':[{**base,'domain':'belief'}]},payload)


def test_unit_review_prompt_checks_qualifiers_objects_time_conditions_and_emotion_baseline():
    from app.reflection import POLICY_VERSION, PROPOSAL_SYSTEM, REVIEW_SYSTEM
    assert POLICY_VERSION=='reflection-v1.5'
    for marker in ('review_units','每unit_id恰好一次','supported','interpretation','unsupported','conflict',
                   '金额对应消费对象','日期及时间间隔','触发与等待条件','有时','不输出思考过程'):
        assert marker in REVIEW_SYSTEM
    assert '不能把捏造的事实前提藏在interpretation里' in REVIEW_SYSTEM
    for prompt in (PROPOSAL_SYSTEM, REVIEW_SYSTEM):
        assert 'intensity_direction' in prompt and 'current_intensity' in prompt


def test_derived_scoring_and_summary_text_cannot_become_reflection_evidence():
    event, source = synthetic_event('合成小说：戒指花了37元。')
    marker = '派生标签要求将故事作为长期验收目标'
    event.update(title=marker, gist=marker, importance_reason=marker, emotional_reason=marker)
    event['details']=[{**event['evidence'][0], 'kind':'fact', 'meaning':marker,
        'importance_reason':marker, 'emotional_reason':marker}]
    repo, model = ReflectionRepo(events=[(event,source)]), ReflectionModel()
    service=ReflectionService(repo,None,model)
    preview=service.preview()
    assert marker not in json.dumps(preview['payload'],ensure_ascii=False)
    assert preview['payload']['episodes'][0]['evidence'][0]['quote']==source['user_text']
    job=service.enqueue()
    assert service.execute(job['id'])['status']=='completed'
    assert len(model.requests)==2
    assert all(marker not in json.dumps(payload,ensure_ascii=False) for _,payload in model.requests)
    assert repo.events[event['id']]['importance_reason']==marker
    assert any(marker in row['query'] for row in repo.candidate_requests)
