"""Synthetic lab reflection controls, paired snapshots and request accounting."""
from dataclasses import replace
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.errors import MemoryError
from app.lab import (AdvanceTime, CloneExperiment, CreateExperiment, LabCallBudget,
                     LabManager, LabModel, MeteredMiniMax, ReflectionInput, RunInput)
from app.main import create_app
from conftest import FakeEmbeddings


@pytest.fixture
def reflection_lab(repo, tmp_path):
    manager = LabManager(replace(repo.settings, lab_enabled=True, lab_data_dir=str(tmp_path/'reflection-lab')))
    yield manager
    manager.close()
    for item in manager.listing()['items']:
        manager.delete(item['id'])


def wait_for(lab, identifier, run_id, reflection=False):
    read = lab.reflection_runs if reflection else lab.runs
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = read(identifier, run_id)
        if result['status'] not in ('queued', 'running'):
            return result
        time.sleep(.03)
    raise AssertionError('synthetic lab operation did not complete')


def test_mock_review_covers_each_proposal_unit_with_its_own_sources():
    first, second = str(uuid4()), str(uuid4())
    proposals = [{'id': 'first', 'evidence_ids': [first]}, {'id': 'second', 'evidence_ids': [second]}]
    units = [
        {'unit_id': 'first:text:u0001', 'proposal_id': 'first', 'field': 'text', 'start': 0, 'end': 3, 'text': '合成。'},
        {'unit_id': 'second:reason:u0001', 'proposal_id': 'second', 'field': 'reason', 'start': 0, 'end': 3, 'text': '依据。'},
        {'unit_id': 'first:text:u0002', 'proposal_id': 'first', 'field': 'text', 'start': 3, 'end': 6, 'text': '说明。'},
        {'unit_id': 'first:reason:u0001', 'proposal_id': 'first', 'field': 'reason', 'start': 0, 'end': 3, 'text': '依据。'},
        {'unit_id': 'second:text:u0001', 'proposal_id': 'second', 'field': 'text', 'start': 0, 'end': 3, 'text': '合成。'},
    ]
    model = LabModel()
    result = model.review_reflection('', json.dumps({'proposals': proposals, 'review_units': units}))
    reviews = result['decision']['reviews']
    assert [row['proposal_id'] for row in reviews] == ['first', 'second']
    for proposal, review in zip(proposals, reviews):
        expected = [unit['unit_id'] for unit in units if unit['proposal_id'] == proposal['id']]
        assert [check['unit_id'] for check in review['checks']] == expected
        assert review['accept'] is True
        assert all(check['support'] == 'interpretation' and check['evidence_ids'] == proposal['evidence_ids']
                   and '模拟' in check['note'] and '不证明' in check['note'] for check in review['checks'])
    assert model.cloud_calls == 0 and result['usage'] == {}


def test_reflection_profiles_clone_and_legacy_registry_default_off(reflection_lab):
    lab = reflection_lab
    source = lab.create(CreateExperiment())
    assert source['reflection_profile'] == 'off'
    assert lab.detail(source['id'])['reflection_settings']['enabled'] is False
    target = lab.clone(source['id'], CloneExperiment(reflection_profile='scheduled'))
    assert target['reflection_profile'] == 'scheduled'
    assert lab.detail(target['id'])['reflection_settings']['enabled'] is True
    assert lab.detail(source['id'])['reflection_settings']['enabled'] is False
    assert lab.detail(source['id'])['originals'] == lab.detail(target['id'])['originals']
    lab.registry['experiments'][source['id']].pop('reflection_profile')
    lab.registry['experiments'][source['id']].pop('reflection_runs')
    lab._save()
    reopened = LabManager(lab.settings)
    try:
        assert reopened._meta(source['id'])['reflection_profile'] == 'off'
        assert reopened.reflection_runs(source['id']) == {'items': []}
        lab.registry = reopened.registry
    finally:
        reopened.close()


def test_time_and_repeated_reflection_preview_are_read_only(reflection_lab, monkeypatch):
    from app.lab import LabModel
    lab = reflection_lab
    source = lab.create(CreateExperiment(reflection_profile='scheduled'))
    before = lab.detail(source['id'])
    repo = lab._repo(lab._meta(source['id']))
    try:
        with repo.pool.connection() as connection:
            stored_before = connection.execute('SELECT * FROM self_observations ORDER BY id').fetchall()
    finally:
        repo.close()
    monkeypatch.setattr(LabModel, 'propose_reflection', lambda *args: pytest.fail('preview called model'))
    lab.advance(source['id'], AdvanceTime(days=365))
    baseline = lab.detail(source['id'])
    earlier_emotions = {row['id']: row for row in before['self_history'] if row['domain'] == 'emotion'}
    later_emotions = [row for row in baseline['self_history'] if row['domain'] == 'emotion']
    assert later_emotions
    assert any(row['current_intensity'] < earlier_emotions[row['id']]['current_intensity']
               for row in later_emotions)
    assert all(row['intensity'] == earlier_emotions[row['id']]['intensity'] for row in later_emotions)
    for _ in range(100):
        preview = lab.reflection_preview(source['id'], automatic=True)
        assert preview['read_only'] and preview['cloud_calls'] == 0
    after = lab.detail(source['id'])
    assert before['originals'] == after['originals']
    assert before['counts'] == after['counts']
    assert baseline['clock'] == after['clock']
    assert baseline['self_history'] == after['self_history']
    assert baseline['self_state'] == after['self_state']
    assert lab.reflection_runs(source['id']) == {'items': []}
    repo = lab._repo(lab._meta(source['id']))
    try:
        with repo.pool.connection() as connection:
            assert connection.execute('SELECT * FROM self_observations ORDER BY id').fetchall() == stored_before
            assert connection.execute('SELECT count(*) AS n FROM reflection_runs').fetchone()['n'] == 0
    finally:
        repo.close()


def test_mock_reflection_does_not_create_fake_dialogue_or_cloud_calls(reflection_lab):
    lab = reflection_lab
    source = lab.create(CreateExperiment())
    before = lab.detail(source['id'])
    queued = lab.start_reflection(source['id'], ReflectionInput(automatic=False))
    result = wait_for(lab, source['id'], queued['id'], reflection=True)
    assert result['status'] == 'completed', result
    assert result['model_mode'] == 'mock'
    assert result['cloud_calls'] == result['cloud_call_limit'] == 0
    after = lab.detail(source['id'])
    assert before['originals'] == after['originals']
    assert before['counts']['experiences'] == after['counts']['experiences']
    repo = lab._repo(lab._meta(source['id']))
    try:
        with repo.pool.connection() as connection:
            assert connection.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n'] == 0
    finally:
        repo.close()


def test_shared_call_budget_counts_failure_and_survives_new_models(reflection_lab, monkeypatch):
    from app.model import MiniMax
    calls = []

    def failure(*args, **kwargs):
        calls.append(1)
        raise MemoryError('synthetic_transport_failure', '合成传输失败', 502)

    monkeypatch.setattr(MiniMax, '_request', failure)
    ledger = LabCallBudget(4)
    for _ in range(2):
        model = MeteredMiniMax(reflection_lab.settings, call_limit=2, budget=ledger)
        for _ in range(2):
            with pytest.raises(MemoryError):
                model._request('synthetic', [])
    next_time_model = MeteredMiniMax(reflection_lab.settings, budget=ledger)
    with pytest.raises(MemoryError) as error:
        next_time_model._request('synthetic', [])
    assert error.value.code == 'lab_run_call_budget'
    assert ledger.calls == len(calls) == 4
    assert next_time_model.cloud_calls == 0


def test_paired_runs_use_same_snapshot_and_shared_limit_across_time(reflection_lab, monkeypatch):
    import app.lab as module
    from app.model import MiniMax
    lab = reflection_lab
    source = lab.create(CreateExperiment())
    before = lab.detail(source['id'])
    # A synthetic provider stub exercises both metering layers without network.
    lab.settings = replace(lab.settings, model_key='synthetic-lab-test')
    lab.registry['experiments'][source['id']]['model_mode'] = 'live'
    lab._save()
    monkeypatch.setattr(module, 'PROBES', [{'id': 'ring', 'query': '紫藤戒指', 'expected_key': 'promise', 'required': ['37元']}])
    requests, snapshots, ledgers = [], [], []
    monkeypatch.setattr(MiniMax, '_request', lambda *args, **kwargs: requests.append(1) or {})
    original_service = lab._service

    class StubReflection:
        enabled = False

        def __init__(self, repo, model):
            self.repo, self.model = repo, model

        def set_enabled(self, enabled):
            self.enabled = enabled

        def settings(self):
            return {'enabled': self.enabled}

        def preview(self, automatic=False):
            return {'eligible': self.enabled, 'reasons': [] if self.enabled else ['disabled'], 'manifest': []}

        def enqueue(self, automatic=False):
            return {'id': str(uuid4()), 'status': 'pending'}

        def execute(self, run_id=None):
            self.model._request('synthetic proposal', [])
            self.model._request('synthetic review', [])
            return {'id': run_id, 'status': 'completed', 'calls': [{'phase': 'proposal'}, {'phase': 'review'}]}

    def reflection(repo, meta, budget=None):
        model = MeteredMiniMax(lab.settings, call_limit=2, budget=budget)
        return StubReflection(repo, model), model

    def service(repo, meta):
        instance, model = original_service(repo, meta)
        with repo.pool.connection() as connection:
            ids = connection.execute('SELECT id FROM experiences ORDER BY id').fetchall()
        snapshots.append((repo.settings.db_schema, ids, repo.clock.now()))

        def chat(query):
            ledgers.append(model.budget)
            for _ in range(3):
                model._request('synthetic chat', [])
            preview = instance.recall_preview(query)
            return {'answer': '合成回答37元', 'actual_context': preview['payload'], 'recall_trace': preview}

        instance.chat = chat
        return instance, model

    monkeypatch.setattr(lab, '_reflection_service', reflection)
    monkeypatch.setattr(lab, '_service', service)
    queued = lab.start_run(source['id'], RunInput(mode='chat', days=[0, 365], reflection_mode='paired'))
    result = wait_for(lab, source['id'], queued['id'])
    assert result['status'] == 'completed' and result['failed_steps'] == 0, result
    assert result['steps'] == 4
    assert result['per_case_cloud_call_limit'] == 8
    assert result['cloud_calls'] == result['cloud_call_limit'] == len(requests) == 16
    assert len({id(ledger) for ledger in ledgers}) == 1
    assert ledgers[0].calls == 16
    assert len({schema for schema, _, _ in snapshots}) == 4
    assert all(snapshot[1] == snapshots[0][1] for snapshot in snapshots)
    assert snapshots[0][2] == snapshots[1][2] and snapshots[2][2] == snapshots[3][2]
    assert [row['arm'] for row in result['results']] == ['off', 'on', 'off', 'on']
    assert [row['cloud_calls'] for row in result['results']] == [3, 5, 3, 5]
    assert all(row['actual_next_context'] == row['answer']['actual_context'] for row in result['results'])
    assert lab.detail(source['id'])['originals'] == before['originals']


def test_reflection_failure_is_retained_and_delete_is_blocked_while_queued(reflection_lab, monkeypatch):
    lab = reflection_lab
    source = lab.create(CreateExperiment())
    captured = []
    monkeypatch.setattr(lab.executor, 'submit', lambda *args: captured.append(args))
    queued = lab.start_reflection(source['id'], ReflectionInput())
    with pytest.raises(MemoryError) as error:
        lab.delete(source['id'])
    assert error.value.code == 'lab_busy'
    original = lab._reflection_service

    def factory(repo, meta, budget=None):
        service, model = original(repo, meta, budget)

        def fail(*args, **kwargs):
            raise MemoryError('synthetic_reflection_failure', '合成反思失败', 502)

        service.preview = lambda **kwargs: {'eligible': True, 'reasons': []}
        service.enqueue = fail
        return service, model

    monkeypatch.setattr(lab, '_reflection_service', factory)
    function, *args = captured[0]
    function(*args)
    report = lab.reflection_runs(source['id'], queued['id'])
    assert report['status'] == 'failed' and report['error'] == 'synthetic_reflection_failure'
    assert 'before_state' in report and 'preview' in report
    assert report['cloud_calls'] == 0


def test_reflection_routes_auth_profile_validation_and_preview(repo, model, tmp_path):
    settings = replace(repo.settings, lab_enabled=True, lab_data_dir=str(tmp_path/'reflection-routes'))
    app = create_app(settings, repo=repo, embeddings=FakeEmbeddings(), model=model)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer test-token'}
        assert client.post('/v1/lab/experiments', headers=headers,
                           json={'reflection_profile': 'arbitrary-code'}).status_code == 422
        created = client.post('/v1/lab/experiments', headers=headers, json={'scenario_id': 'empty'}).json()
        base = '/v1/lab/experiments/'+created['id']
        assert client.get(base+'/reflection-preview').status_code == 401
        response = client.get(base+'/reflection-preview?automatic=true', headers=headers)
        assert response.status_code == 200 and response.json()['read_only']
        assert client.get(base+'/reflection-runs', headers=headers).json() == {'items': []}
        assert client.post(base+'/reflection-runs', headers=headers, json={'schema': 'memory'}).status_code == 422
        assert client.delete(base, headers=headers).status_code == 200


def test_empty_reflection_is_reported_as_skip_without_enqueue(reflection_lab, monkeypatch):
    lab = reflection_lab
    source = lab.create(CreateExperiment(scenario_id='empty'))
    original = lab._reflection_service

    def factory(repo, meta, budget=None):
        service, model = original(repo, meta, budget)
        service.enqueue = lambda **kwargs: pytest.fail('ineligible reflection was enqueued')
        return service, model

    monkeypatch.setattr(lab, '_reflection_service', factory)
    queued = lab.start_reflection(source['id'], ReflectionInput())
    result = wait_for(lab, source['id'], queued['id'], reflection=True)
    assert result['status'] == 'completed'
    assert result['result']['status'] == 'skipped' and result['result']['reasons']
    assert result['cloud_calls'] == 0


def test_restart_cancels_orphan_reflection_lease_and_preserves_profile(reflection_lab):
    lab = reflection_lab
    source = lab.create(CreateExperiment(reflection_profile='scheduled'))
    repo = lab._repo(lab._meta(source['id']))
    try:
        reflection, _ = lab._reflection_service(repo, lab._meta(source['id']))
        queued_core = reflection.enqueue(automatic=False)
        with repo.pool.connection() as connection:
            quota_before = connection.execute('SELECT * FROM reflection_daily_usage ORDER BY day').fetchall()
    finally:
        repo.close()
    operation_id = str(uuid4())
    lab.registry['experiments'][source['id']]['reflection_runs'][operation_id] = {
        'id': operation_id, 'status': 'running'}
    lab._save()
    reopened = LabManager(lab.settings)
    try:
        assert reopened.reflection_runs(source['id'], operation_id)['status'] == 'interrupted'
        assert reopened.detail(source['id'])['reflection_settings']['enabled'] is True
        repo = reopened._repo(reopened._meta(source['id']))
        try:
            with repo.pool.connection() as connection:
                run = connection.execute('SELECT status,lease_token,lease_until FROM reflection_runs WHERE id=%s',
                                         (queued_core['id'],)).fetchone()
                assert run == {'status': 'cancelled', 'lease_token': None, 'lease_until': None}
                assert connection.execute('SELECT * FROM reflection_daily_usage ORDER BY day').fetchall() == quota_before
        finally:
            repo.close()
        lab.registry = reopened.registry
    finally:
        reopened.close()


def test_many_years_advance_allows_one_run_without_catch_up_storm(reflection_lab):
    lab = reflection_lab
    source = lab.create(CreateExperiment(reflection_profile='scheduled'))
    before = lab.detail(source['id'])
    lab.advance(source['id'], AdvanceTime(days=3650))
    assert lab.reflection_runs(source['id'])['items'] == []
    assert lab.reflection_preview(source['id'], automatic=True)['preview']['eligible']
    queued = lab.start_reflection(source['id'], ReflectionInput(automatic=True))
    report = wait_for(lab, source['id'], queued['id'], reflection=True)
    assert report['status'] == 'completed', report
    assert report['result']['status'] == 'completed', report
    assert len(lab.reflection_runs(source['id'])['items']) == 1
    lab.advance(source['id'], AdvanceTime(days=3650))
    assert not lab.reflection_preview(source['id'], automatic=True)['preview']['eligible']
    assert len(lab.reflection_runs(source['id'])['items']) == 1
    after = lab.detail(source['id'])
    assert before['originals'] == after['originals']
    assert before['counts']['episode_rehearsals'] == after['counts']['episode_rehearsals']


def test_logical_advance_does_not_reset_real_day_reflection_quota(reflection_lab):
    lab = reflection_lab
    source = lab.create(CreateExperiment(reflection_profile='scheduled'))
    # Inspect the real-provider eligibility without sending any request.
    lab.registry['experiments'][source['id']]['model_mode'] = 'live'
    lab._save()
    quota = lab.detail(source['id'])['reflection_settings']['quota']
    repo = lab._repo(lab._meta(source['id']))
    try:
        with repo.pool.connection() as connection:
            connection.execute('''INSERT INTO reflection_daily_usage(day,rounds,calls) VALUES(%s,2,4)
                ON CONFLICT(day) DO UPDATE SET rounds=2,calls=4''', (quota['day'],))
    finally:
        repo.close()
    lab.advance(source['id'], AdvanceTime(days=3650))
    settings = lab.detail(source['id'])['reflection_settings']
    assert settings['quota']['day'] == quota['day']
    assert settings['quota']['rounds_used'] == 2 and settings['quota']['calls_used'] == 4
    preview = lab.reflection_preview(source['id'], automatic=False)['preview']
    assert not preview['eligible']
    assert 'daily_round_limit' in preview['reasons'] and 'daily_call_limit' in preview['reasons']
