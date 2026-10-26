"""Synthetic laboratory boundaries, replay, snapshots and API contracts."""
from dataclasses import replace
from datetime import datetime
import time

from fastapi.testclient import TestClient
import pytest

from app.errors import MemoryError
from app.lab import (AddEvent, AdvanceTime, ChatInput, CloneExperiment, CreateExperiment, LabManager,
                     MeteredMiniMax, ProbeInput, RunInput)
from app.main import create_app
from conftest import FakeEmbeddings


@pytest.fixture
def lab(repo, tmp_path):
    settings = replace(repo.settings, lab_enabled=True, lab_data_dir=str(tmp_path/'lab'))
    manager = LabManager(settings)
    yield manager
    manager.close()
    for item in manager.listing()['items']:
        manager.delete(item['id'])


def wait_run(lab, experiment_id, run_id):
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        result = lab.runs(experiment_id, run_id)
        if result['status'] not in ('queued', 'running'):
            return result
        time.sleep(.05)
    raise AssertionError('lab run did not complete')


def test_lab_synthetic_data_isolated_and_probe_never_rehearses(lab, repo):
    first = lab.create(CreateExperiment(scenario_id='park-ring'))
    second = lab.create(CreateExperiment(scenario_id='empty'))
    before = lab.detail(first['id'])
    assert before['events'] and before['originals']
    assert lab.detail(second['id'])['events'] == []
    assert repo.search_experiences('紫藤戒指') == []
    for _ in range(100):
        result = lab.probe(first['id'], ProbeInput(query='紫藤戒指'))
        assert result['cloud_calls'] == 0 and result['read_only']
        assert result['preview']['selected']
    after = lab.detail(first['id'])
    assert after['counts'] == before['counts']
    assert after['events'] == before['events']
    assert after['self_history'] == before['self_history']


def test_lab_logical_clock_preserves_history_and_changes_emotion_view(lab):
    item = lab.create(CreateExperiment())
    before = lab.detail(item['id'])
    mood = next(state for state in before['self_state'] if state['domain']=='emotion')
    lab.advance(item['id'], AdvanceTime(days=2))
    after = lab.detail(item['id'])
    later = next(state for state in after['self_state'] if state['domain']=='emotion')
    assert later['current_intensity'] == pytest.approx(mood['current_intensity']/4)
    assert later['intensity'] == mood['intensity']
    assert before['originals'] == after['originals']
    assert before['counts'] == after['counts']


def test_lab_snapshot_preserves_provenance_and_independent_writes(lab):
    original = lab.create(CreateExperiment())
    copied = lab.clone(original['id'], CloneExperiment(name='对照副本'))
    first, second = lab.detail(original['id']), lab.detail(copied['id'])
    assert first['events'] == second['events']
    assert first['originals'] == second['originals']
    assert first['self_history'] == second['self_history']
    lab.event(copied['id'], AddEvent(title='独立事件', text='合成故事：小夏在雪松书店买了蓝色笔记本。', synthetic=True))
    lab.advance(copied['id'], AdvanceTime(days=30))
    assert lab.detail(original['id'])['counts'] == first['counts']
    assert lab.detail(copied['id'])['counts']['experiences'] == first['counts']['experiences']+1
    assert lab.detail(original['id'])['clock'] == first['clock']


def test_lab_policy_comparison_keeps_snapshot_and_production_defaults(lab, repo, model):
    from app.recall import RecallPolicy
    from app.service import MemoryService
    original = lab.create(CreateExperiment())
    strict = lab.clone(original['id'], CloneExperiment(policy_profile='strict'))
    assert original['policy_parameters']['natural_threshold'] == RecallPolicy().natural_threshold
    assert strict['policy_parameters']['natural_threshold'] > original['policy_parameters']['natural_threshold']
    assert lab.detail(original['id'])['originals'] == lab.detail(strict['id'])['originals']
    assert lab.detail(original['id'])['clock'] == lab.detail(strict['id'])['clock']
    preview = lab.probe(strict['id'], ProbeInput(query='紫藤戒指'))['preview']
    assert preview['policy_version'] == strict['policy_version']
    assert MemoryService(repo, FakeEmbeddings(), model).recall.policy == RecallPolicy()
    with pytest.raises(ValueError):
        CreateExperiment(policy_profile='custom-python')


def test_lab_report_uses_snapshots_without_polluting_source(lab):
    experiment = lab.create(CreateExperiment())
    before = lab.detail(experiment['id'])
    queued = lab.start_run(experiment['id'], RunInput(days=[0], mode='probe'))
    result = wait_run(lab, experiment['id'], queued['id'])
    assert result['status'] == 'completed', result
    assert result['cloud_calls'] == result['cloud_call_limit'] == 0
    assert result['completed'] == result['steps'] == len(result['results'])
    assert all('context_quote_checks' in row and 'selected_contains_expected_source' in row for row in result['results'])
    assert lab.detail(experiment['id'])['counts'] == before['counts']
    assert lab.detail(experiment['id'])['events'] == before['events']
    assert 'schema' not in result


def test_lab_chat_returns_actual_answer_context(lab):
    experiment = lab.create(CreateExperiment())
    question = '紫藤戒指那次的原话是什么？'
    result = lab.chat(experiment['id'], ChatInput(text=question, synthetic=True))
    assert result['actual_context']['current_question'] == question
    assert isinstance(result['actual_context']['experience_evidence'], list)


@pytest.mark.parametrize('actual_kind', ['target', 'empty', 'wrong_source', 'missing'])
def test_chat_report_evaluates_actual_context_instead_of_preview(lab, monkeypatch, actual_kind):
    from uuid import uuid4
    import app.lab as module

    experiment = lab.create(CreateExperiment())
    before = lab.detail(experiment['id'])
    event = next(row for row in before['events'] if row['title'] == '星河公园的紫藤戒指')
    target_id = str(event['id'])
    evidence = {'id': target_id, 'episode_id': target_id, 'details': [{'quote': '37元'}]}
    case = {'id': 'actual-context', 'query': '紫藤戒指的金额？', 'expected_key': 'promise', 'required': ['37元']}
    monkeypatch.setattr(module, 'PROBES', [case])
    original = lab._service

    def service(repo, meta):
        result, model = original(repo, meta)
        preview_evidence = [] if actual_kind == 'target' else [evidence]
        result.recall_preview = lambda *args, **kwargs: {
            'candidates': preview_evidence, 'selected': preview_evidence,
            'payload': {'experience_evidence': preview_evidence}}

        def chat(*args, **kwargs):
            # Even a selected trace and an answer containing 37元 do not prove
            # the correct source reached the actual answer context.
            response = {'answer': '37元', 'recall_trace': {'candidates': [evidence], 'selected': [evidence]}}
            if actual_kind != 'missing':
                actual_evidence = [evidence] if actual_kind == 'target' else []
                if actual_kind == 'wrong_source':
                    other_id = str(uuid4())
                    actual_evidence = [{'id': other_id, 'episode_id': other_id, 'details': [{'quote': '37元'}]}]
                response['actual_context'] = {'current_question': case['query'], 'experience_evidence': actual_evidence}
            return response

        result.chat = chat
        return result, model

    monkeypatch.setattr(lab, '_service', service)
    queued = lab.start_run(experiment['id'], RunInput(days=[0], mode='chat'))
    report = wait_run(lab, experiment['id'], queued['id'])
    assert report['status'] == 'completed', report
    row = report['results'][0]
    expected = actual_kind == 'target'
    assert row['preview_selected_contains_expected_source'] is not expected
    assert row['preview_context_quote_checks'] == {'37元': not expected}
    assert row['selected_contains_expected_source'] is expected
    assert row['context_quote_checks'] == {'37元': expected}
    assert row['context_basis'] == 'answer.actual_context'
    assert report['metrics']['selected_hit_count'] == int(expected)
    assert report['metrics']['context_quote_hit_count'] == int(expected)
    if actual_kind == 'missing':
        assert row['status'] == 'failed' and row['error'] == 'lab_actual_context_missing'
        assert row['context_evaluated'] is False and report['failed_steps'] == 1
    else:
        assert row['context_evaluated'] is True and row['answer_quote_checks'] == {'37元': True}
    assert lab.detail(experiment['id'])['counts'] == before['counts']


def test_lab_failed_chat_steps_are_preserved_without_retry(lab, monkeypatch):
    experiment = lab.create(CreateExperiment())
    original = lab._service
    calls = []

    def service(repo, meta):
        result, model = original(repo, meta)

        def failing(*args, **kwargs):
            calls.append(1)
            raise MemoryError('synthetic_failure', '合成失败', 502)

        result.chat = failing
        return result, model

    monkeypatch.setattr(lab, '_service', service)
    queued = lab.start_run(experiment['id'], RunInput(days=[0], mode='chat'))
    result = wait_run(lab, experiment['id'], queued['id'])
    assert result['status'] == 'completed'
    assert result['failed_steps'] == result['steps'] == len(calls)
    assert all(row['error']=='synthetic_failure' for row in result['results'])


def test_lab_recovery_marks_unfinished_runs_interrupted(lab):
    experiment = lab.create(CreateExperiment())
    lab.registry['experiments'][experiment['id']]['runs']['synthetic-run'] = {'id':'synthetic-run','status':'running'}
    lab._save()
    reopened = LabManager(lab.settings)
    try:
        assert reopened.runs(experiment['id'], 'synthetic-run')['status'] == 'interrupted'
        # Keep this fixture's final cleanup consistent with the recovered registry.
        lab.registry = reopened.registry
    finally:
        reopened.close()


def test_lab_refuses_delete_during_operation(lab):
    experiment = lab.create(CreateExperiment())
    with lab._locked(experiment['id']):
        with pytest.raises(MemoryError) as caught:
            lab.delete(experiment['id'])
    assert caught.value.status == 409
    assert lab.detail(experiment['id'])['events']


def test_lab_cloud_limiter_counts_failed_requests(lab, monkeypatch):
    from app.model import MiniMax
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise MemoryError('synthetic_provider_failure', '合成失败', 502)

    monkeypatch.setattr(MiniMax, '_request', fail)
    model = MeteredMiniMax(lab.settings)
    for _ in range(3):
        with pytest.raises(MemoryError):
            model._request('system', [])
    with pytest.raises(MemoryError) as caught:
        model._request('system', [])
    assert caught.value.code == 'lab_call_budget'
    assert model.cloud_calls == len(calls) == 3


def test_lab_routes_require_auth_and_reject_schema_and_real_data_flags(repo, model, tmp_path):
    settings = replace(repo.settings, lab_enabled=True, lab_data_dir=str(tmp_path/'routes'))
    app = create_app(settings, repo=repo, embeddings=FakeEmbeddings(), model=model)
    with TestClient(app) as client:
        assert client.get('/lab').status_code == 200
        assert client.get('/v1/lab/scenarios').status_code == 401
        headers = {'Authorization':'Bearer test-token'}
        assert client.get('/v1/lab/scenarios', headers=headers).status_code == 200
        assert client.post('/v1/lab/experiments', headers=headers, json={'schema':'memory'}).status_code == 422
        assert client.post('/v1/lab/experiments', headers=headers, json={'synthetic':False}).status_code == 422
        created = client.post('/v1/lab/experiments', headers=headers, json={'scenario_id':'empty'})
        assert created.status_code == 201
        item = created.json()
        assert 'schema' not in item and 'journal' not in item
        assert client.post('/v1/lab/experiments/'+item['id']+'/events', headers=headers,
            json={'title':'合成','text':'事件','synthetic':False}).status_code == 422
        assert client.delete('/v1/lab/experiments/'+item['id'], headers=headers).status_code == 200
        assert client.get('/v1/lab/experiments/'+item['id'], headers=headers).status_code == 404


def test_production_does_not_register_laboratory(repo, model):
    settings = replace(repo.settings, lab_enabled=False)
    app = create_app(settings, repo=repo, embeddings=FakeEmbeddings(), model=model)
    with TestClient(app) as client:
        assert client.get('/lab').status_code == 404
        assert client.get('/v1/lab/scenarios').status_code == 404
