"""Isolated synthetic experiments and their local inspection UI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
from threading import BoundedSemaphore, Event, Lock, RLock
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from psycopg import sql
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.clock import MutableClock
from app.embeddings import LocalEmbeddings
from app.errors import MemoryError
from app.lab_scenarios import PROBES, SCENARIO_VERSION, START_TIME, encoded_event, scenario, scenarios
from app.model import MiniMax
from app.repository import Repository
from app.recall import RecallEngine, RecallPolicy
from app.service import MemoryService


LAB_POLICIES = {
    'balanced': RecallPolicy(),
    'strict': replace(RecallPolicy(), version='episodic-activation-strict-v1', natural_threshold=.50, deep_threshold=.24),
}


class LabInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    @field_validator('name', 'title', 'text', 'query', check_fields=False)
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError('内容不能为空白')
        return value


class CreateExperiment(LabInput):
    name: str = Field(default='合成记忆实验', min_length=1, max_length=100)
    scenario_id: Literal['park-ring', 'similar-events', 'empty'] = 'park-ring'
    model_mode: Literal['mock', 'live'] = 'mock'
    embedding_mode: Literal['fake', 'local'] = 'fake'
    policy_profile: Literal['balanced', 'strict'] = 'balanced'
    reflection_profile: Literal['off', 'scheduled'] = 'off'
    start_time: AwareDatetime = datetime.fromisoformat(START_TIME)
    synthetic: Literal[True] = True


class CloneExperiment(LabInput):
    name: str = Field(default='实验快照副本', min_length=1, max_length=100)
    model_mode: Literal['mock', 'live'] | None = None
    embedding_mode: Literal['fake', 'local'] | None = None
    policy_profile: Literal['balanced', 'strict'] | None = None
    reflection_profile: Literal['off', 'scheduled'] | None = None


class AddEvent(LabInput):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    importance: float = Field(default=.2, ge=0, le=1)
    emotional_salience: float = Field(default=0, ge=0, le=1)
    synthetic: Literal[True]
    encoding: Literal['fixture', 'model'] = 'fixture'


class AdvanceTime(LabInput):
    days: float = Field(default=0, ge=0, le=36500)
    hours: float = Field(default=0, ge=0, le=876000)


class ProbeInput(LabInput):
    query: str = Field(min_length=1, max_length=8000)
    recall_mode: Literal['natural', 'deep'] = 'natural'
    budget: int | None = Field(default=None, ge=1000, le=100000)
    conversation_id: UUID | None = None


class ChatInput(LabInput):
    text: str = Field(min_length=1, max_length=8000)
    conversation_id: UUID | None = None
    recall_mode: Literal['natural', 'deep'] = 'natural'
    synthetic: Literal[True]


class RunInput(LabInput):
    mode: Literal['probe', 'chat'] = 'probe'
    days: list[int] = Field(default_factory=lambda: [0, 30, 365, 1825], min_length=1, max_length=6)
    repeats: int = Field(default=1, ge=1, le=3)
    reflection_mode: Literal['off', 'on', 'paired'] = 'off'


class ReflectionInput(LabInput):
    automatic: bool = False


class LabEmbeddings:
    """Stable lexical vectors; this deliberately is not a semantic benchmark."""
    dimensions = 512

    def embed(self, texts, query=False):
        result = []
        for text in texts:
            vector = [0.0] * self.dimensions
            units = [text[i:i+2] for i in range(max(0, len(text)-1))] or [text]
            for unit in units:
                digest = sha256(unit.encode()).digest()
                vector[int.from_bytes(digest[:2], 'big') % self.dimensions] += 1
            length = math.sqrt(sum(value * value for value in vector)) or 1
            result.append([value / length for value in vector])
        return result


class LabModel:
    cloud_calls = 0
    is_mock = True

    def decide_memories(self, system, prompt):
        return {'decision': {'actions': []}, 'usage': {}}

    def reassess_memories(self, system, prompt):
        return {'decision': {}, 'usage': {}}

    def propose_reflection(self, system, prompt):
        payload = json.loads(prompt)
        # A visibly synthetic interpretation exercises evidence/version writes;
        # it is never presented as a semantic or emotional quality result.
        key = 'lab_mock_interpretation'
        if any(row.get('state_key', row.get('key')) == key for row in payload.get('self_state', [])):
            return {'decision': {'updates': []}, 'usage': {}}
        event = next((row for row in payload.get('episodes', []) if row.get('pending') and row.get('evidence')), None)
        updates = [] if event is None else [{'id': 'mock_interpretation', 'domain': 'belief', 'key': key,
            'text': '模拟认识：这段合成经历需要结合具体情境和原话理解。',
            'reason': '仅用于验证反思原文引用、复核与状态版本的合成夹具，不用于评价认识质量。',
            'confidence': .5, 'evidence_ids': [event['evidence'][0]['id']],
            'target_id': None, 'expected_revision': 0}]
        return {'decision': {'updates': updates}, 'usage': {}}

    def review_reflection(self, system, prompt):
        payload = json.loads(prompt)
        return {'decision': {'reviews': [{'proposal_id': row['id'], 'accept': True,
            'reason': '合成流程夹具：接受固定模拟提案，仅验证事务与来源。',
            'checks': [{'unit_id': unit['unit_id'], 'support': 'interpretation',
                'evidence_ids': list(row['evidence_ids']),
                'note': '模拟逐句复核，仅验证单元覆盖与来源绑定，不证明解释质量。'}
                for unit in payload['review_units'] if unit['proposal_id'] == row['id']]}
            for row in payload.get('proposals', [])]}, 'usage': {}}

    def chat(self, system, messages):
        return {'text': '模拟回答：本轮只验证记忆流程。请在右侧查看实际选中的证据；此输出不用于评价模型回答质量。',
                'usage': {'input_tokens': 0, 'output_tokens': 0}}


class LabCallBudget:
    """One ledger per run, shared across every snapshot, time point and arm."""
    def __init__(self, limit):
        self.limit = limit
        self.calls = 0
        self.lock = Lock()

    def consume(self):
        with self.lock:
            if self.calls >= self.limit:
                raise MemoryError('lab_run_call_budget', '本次实验已达到云调用总上限。', 429)
            self.calls += 1


class MeteredMiniMax(MiniMax):
    def __init__(self, settings, call_limit=3, budget=None):
        super().__init__(settings)
        self.cloud_calls = 0
        self.call_limit = call_limit
        self.budget = budget

    def _request(self, *args, **kwargs):
        if self.cloud_calls >= self.call_limit:
            raise MemoryError('lab_call_budget', '当前阶段已达到云调用上限。', 429)
        if self.budget is not None:
            self.budget.consume()
        self.cloud_calls += 1
        return super()._request(*args, **kwargs)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            os.chmod(temporary, 0o600)
            json.dump(jsonable_encoder(value), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class LabManager:
    def __init__(self, settings):
        self.settings = settings
        self.directory = Path(getattr(settings, 'lab_data_dir', '/data/lab'))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry_path = self.directory / 'registry.json'
        self.guard = RLock()
        self.locks = {}
        self.stopping = Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='memory-lab')
        self.run_slots = BoundedSemaphore(4)
        self.local_embeddings = LocalEmbeddings(settings)
        self.registry = {'version': 1, 'experiments': {}}
        if self.registry_path.exists():
            self.registry = json.loads(self.registry_path.read_text())
            if self.registry.get('version') != 1:
                raise RuntimeError('unsupported_lab_registry')
            for experiment in self.registry['experiments'].values():
                experiment.setdefault('policy_profile', 'balanced')
                experiment.setdefault('reflection_profile', 'off')
                experiment.setdefault('reflection_runs', {})
                if 'policy_parameters' not in experiment:
                    experiment['policy_parameters'] = asdict(LAB_POLICIES['balanced'])
                    experiment['policy_version'] = LAB_POLICIES['balanced'].version
                for run in [*experiment.get('runs', {}).values(), *experiment['reflection_runs'].values()]:
                    if run['status'] in ('queued', 'running'):
                        run.update(status='interrupted', error='实验服务重启，运行未完成；结果没有自动重试。')
                        if run['id'] in experiment['reflection_runs']:
                            experiment['reflection_recovery_required'] = True
            self._save()

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=True)

    def _save(self):
        with self.guard:
            _atomic_json(self.registry_path, self.registry)

    def _meta(self, experiment_id):
        key = str(UUID(str(experiment_id)))
        with self.guard:
            item = self.registry['experiments'].get(key)
            if not item:
                raise MemoryError('lab_not_found', '实验不存在。', 404)
            return deepcopy(item)

    @contextmanager
    def _locked(self, experiment_id, wait=False, allow_run=False):
        self._meta(experiment_id)
        with self.guard:
            lock = self.locks.setdefault(str(experiment_id), Lock())
        if not lock.acquire(blocking=wait):
            raise MemoryError('lab_busy', '实验正在执行，请完成后再修改或删除。', 409)
        try:
            meta = self._meta(experiment_id)
            if not allow_run and self._active_runs(meta):
                raise MemoryError('lab_busy', '实验报告已排队或正在执行，请结束后再操作。', 409)
            yield meta
        finally:
            lock.release()

    def _settings(self, meta):
        return replace(self.settings, db_schema=meta['schema'], deletion_journal=meta['journal'], lab_enabled=True)

    def _repo(self, meta, at=None, replay=True):
        repo = Repository(self._settings(meta), clock=MutableClock(datetime.fromisoformat(at or meta['clock'])))
        try:
            if replay:
                # Older laboratory schemas are independent of the main API's
                # migration and must be upgraded before accessing new tables.
                repo.migrate()
                repo.replay_deletions(required=True)
                if meta.get('reflection_recovery_required'):
                    repo.reset_reflections_after_restore(reason='lab_restarted')
                    self._reflection_service(repo, meta)[0].set_enabled(meta.get('reflection_profile') == 'scheduled')
                    with self.guard:
                        self.registry['experiments'][meta['id']].pop('reflection_recovery_required', None)
                        self._save()
            return repo
        except Exception:
            repo.close()
            raise

    def _service(self, repo, meta):
        embeddings = self.local_embeddings if meta['embedding_mode'] == 'local' else LabEmbeddings()
        model = MeteredMiniMax(self.settings) if meta['model_mode'] == 'live' else LabModel()
        service = MemoryService(repo, embeddings, model)
        service.recall = RecallEngine(repo, embeddings, policy=RecallPolicy(**meta['policy_parameters']))
        return service, model

    @staticmethod
    def _active_runs(meta):
        return any(run['status'] in ('queued', 'running') for run in
                   [*meta.get('runs', {}).values(), *meta.get('reflection_runs', {}).values()])

    def _reflection_service(self, repo, meta, budget=None):
        from app.reflection import ReflectionService
        embeddings = self.local_embeddings if meta['embedding_mode'] == 'local' else LabEmbeddings()
        model = MeteredMiniMax(self.settings, call_limit=2, budget=budget) if meta['model_mode'] == 'live' else LabModel()
        return ReflectionService(repo, embeddings, model), model

    @staticmethod
    def _public(meta):
        return {key: value for key, value in meta.items() if key not in
                ('schema', 'journal', 'runs', 'reflection_runs', 'reflection_recovery_required')}

    def listing(self):
        with self.guard:
            return {'items': [self._public(item) for item in self.registry['experiments'].values()]}

    def _new_meta(self, data):
        identifier = str(uuid4())
        directory = self.directory / identifier
        directory.mkdir(mode=0o700)
        journal = directory / 'deletions.jsonl'
        journal.touch(mode=0o600)
        return {'id': identifier, 'name': data.name, 'schema': 'lab_' + UUID(identifier).hex,
                'journal': str(journal), 'clock': data.start_time.isoformat(), 'scenario_id': data.scenario_id,
                'scenario_version': SCENARIO_VERSION, 'model_mode': data.model_mode,
                'embedding_mode': data.embedding_mode, 'synthetic': True, 'seed': 0,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'policy_profile': data.policy_profile, 'policy_version': LAB_POLICIES[data.policy_profile].version,
                'policy_parameters': asdict(LAB_POLICIES[data.policy_profile]),
                'reflection_profile': data.reflection_profile,
                'fixture_events': {}, 'runs': {}, 'reflection_runs': {}}

    def create(self, data):
        if data.model_mode == 'live' and not self.settings.model_key:
            raise MemoryError('model_not_configured', '未配置真实模型；可以使用模拟模式。', 400)
        meta = self._new_meta(data)
        repo = self._repo(meta, replay=False)
        try:
            repo.migrate()
            self._reflection_service(repo, meta)[0].set_enabled(data.reflection_profile == 'scheduled')
            for event in scenario(data.scenario_id)['events']:
                result = self._inject(repo, event)
                meta['fixture_events'][event['key']] = str(result['experience_id'])
            self._index(repo, meta)
            with self.guard:
                self.registry['experiments'][meta['id']] = meta
                self._save()
            return self._public(meta)
        except Exception:
            self._drop(repo, meta['schema'])
            shutil.rmtree(Path(meta['journal']).parent, ignore_errors=True)
            raise
        finally:
            repo.close()

    @staticmethod
    def _inject(repo, event):
        description, episodes, states = encoded_event(event)
        return repo.save_answer(uuid4(), event['text'], '合成夹具已编码；未调用生成模型。', [], repo.epoch(),
            experience_description=description, episode_proposals=episodes, self_updates=states)

    def _index(self, repo, meta):
        from app.worker import process_one
        embeddings = self.local_embeddings if meta['embedding_mode'] == 'local' else LabEmbeddings()
        for _ in range(20000):
            if not process_one(repo, embeddings):
                return
        raise MemoryError('lab_index_limit', '实验索引任务超过安全数量，请检查队列。', 503)

    @staticmethod
    def _drop(repo, schema):
        with repo.pool.connection() as connection:
            connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    @staticmethod
    def _copy_schema(repo, source, target):
        """Snapshot all versioned tables, preserving identities and source IDs."""
        with repo.pool.connection() as connection:
            tables = [row['table_name'] for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'",
                (source,)).fetchall() if row['table_name'] != 'schema_migrations']
            edges = connection.execute('''SELECT child.relname AS child,parent.relname AS parent
                FROM pg_constraint fk JOIN pg_class child ON child.oid=fk.conrelid
                JOIN pg_namespace ns ON ns.oid=child.relnamespace JOIN pg_class parent ON parent.oid=fk.confrelid
                WHERE fk.contype='f' AND ns.nspname=%s''', (source,)).fetchall()
            ordered, pending = [], set(tables)
            while pending:
                ready = sorted(name for name in pending if not any(edge['child'] == name and edge['parent'] in pending and edge['parent'] != name for edge in edges))
                if not ready:
                    raise MemoryError('lab_snapshot_schema', '实验表存在无法复制的循环依赖。', 500)
                ordered.extend(ready)
                pending.difference_update(ready)
            if tables:
                connection.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
                    sql.SQL(',').join(sql.Identifier(target, table) for table in tables)))
            for table in ordered:
                connection.execute(sql.SQL('INSERT INTO {} OVERRIDING SYSTEM VALUE SELECT * FROM {}').format(
                    sql.Identifier(target, table), sql.Identifier(source, table)))
            columns = connection.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=%s AND is_identity='YES'", (target,)).fetchall()
            for row in columns:
                qualified = f'"{target}"."{row["table_name"]}"'
                sequence = connection.execute('SELECT pg_get_serial_sequence(%s,%s) AS name', (qualified, row['column_name'])).fetchone()['name']
                maximum = connection.execute(sql.SQL('SELECT max({}) AS n FROM {}').format(
                    sql.Identifier(row['column_name']), sql.Identifier(target, row['table_name']))).fetchone()['n']
                connection.execute('SELECT setval(%s::regclass,%s,%s)', (sequence, maximum or 1, maximum is not None))

    def clone(self, identifier, data):
        with self._locked(identifier) as source:
            source_repo = self._repo(source)
            source_repo.close()
            target = self._new_meta(CreateExperiment(name=data.name, scenario_id='empty',
                start_time=datetime.fromisoformat(source['clock']), model_mode=data.model_mode or source['model_mode'],
                embedding_mode=data.embedding_mode or source['embedding_mode'],
                policy_profile=data.policy_profile or source['policy_profile'],
                reflection_profile=data.reflection_profile or source.get('reflection_profile', 'off')))
            if data.policy_profile is None:
                target.update(policy_parameters=source['policy_parameters'], policy_version=source['policy_version'])
            if target['model_mode'] == 'live' and not self.settings.model_key:
                shutil.rmtree(Path(target['journal']).parent)
                raise MemoryError('model_not_configured', '未配置真实模型。', 400)
            target.update(scenario_id=source['scenario_id'], fixture_events=source['fixture_events'], copied_from=identifier)
            repo = self._repo(target, replay=False)
            try:
                repo.migrate()
                self._copy_schema(repo, source['schema'], target['schema'])
                shutil.copyfile(source['journal'], target['journal'])
                repo.reset_reflections_after_restore(reason='cloned')
                self._reflection_service(repo, target)[0].set_enabled(target['reflection_profile'] == 'scheduled')
                # Vectors built by a different embedding implementation must not mix.
                if target['embedding_mode'] != source['embedding_mode']:
                    with repo.pool.connection() as connection:
                        for table in ('projections', 'experience_chunks', 'episode_chunks'):
                            connection.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(table)))
                        for table in ('outbox', 'experience_jobs', 'episode_jobs'):
                            connection.execute(sql.SQL("UPDATE {} SET status='pending',attempts=0,available_at=now(),lease_until=NULL,lease_token=NULL,last_error=NULL WHERE status<>'cancelled'").format(sql.Identifier(table)))
                    self._index(repo, target)
                with self.guard:
                    self.registry['experiments'][target['id']] = target
                    self._save()
                return self._public(target)
            except Exception:
                self._drop(repo, target['schema'])
                shutil.rmtree(Path(target['journal']).parent, ignore_errors=True)
                raise
            finally:
                repo.close()

    def detail(self, identifier):
        meta = self._meta(identifier)
        repo = self._repo(meta)
        try:
            with repo.pool.connection() as connection:
                events = connection.execute('''SELECT id,title,gist,importance,importance_reason,emotional_salience,
                    emotional_reason,recorded_at,last_rehearsed_at,revision,context_type,source_reliability,
                    encoding_version,time_precision,occurred_at,occurred_until,time_quote,
                    ARRAY(SELECT DISTINCT ev.experience_id FROM episode_evidence ev WHERE ev.episode_id=episodes.id
                          ORDER BY ev.experience_id) AS source_experience_ids FROM episodes
                    WHERE deleted_at IS NULL ORDER BY recorded_at,id LIMIT 1000''').fetchall()
                originals = connection.execute('''SELECT id,title,user_text,assistant_text,recorded_at,context_type
                    FROM experiences WHERE deleted_at IS NULL ORDER BY recorded_at,id LIMIT 1000''').fetchall()
                counts = {table: connection.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n']
                          for table in ('episodes', 'episode_rehearsals', 'experiences', 'self_observations')}
            return self._public(meta) | {'events': events, 'originals': originals, 'self_state': repo.self_state(),
                'self_history': repo.self_history(limit=100), 'counts': counts, 'health': repo.health(),
                'reflection_settings': self._reflection_service(repo, meta)[0].settings(),
                'view_limit': 1000}
        finally:
            repo.close()

    def advance(self, identifier, data):
        with self._locked(identifier) as meta:
            clock = MutableClock(datetime.fromisoformat(meta['clock']))
            clock.advance(days=data.days, hours=data.hours)
            with self.guard:
                self.registry['experiments'][str(identifier)]['clock'] = clock.now().isoformat()
                self._save()
            return {'clock': clock.now(), 'effects': '仅推进记忆逻辑时间；不会执行历史删除或云反思。'}

    def event(self, identifier, data):
        with self._locked(identifier) as meta:
            repo = self._repo(meta)
            try:
                if data.encoding == 'model':
                    service, model = self._service(repo, meta)
                    result = service.chat(data.text)
                    result['cloud_calls'] = model.cloud_calls
                else:
                    result = self._inject(repo, data.model_dump())
                    result['cloud_calls'] = 0
                self._index(repo, meta)
                return result | {'encoding': data.encoding, 'model_mode': meta['model_mode']}
            finally:
                repo.close()

    def probe(self, identifier, data):
        with self._locked(identifier) as meta:
            repo = self._repo(meta)
            try:
                service, _ = self._service(repo, meta)
                start = time.monotonic()
                result = service.recall_preview(data.query, conversation_id=data.conversation_id,
                    recall_mode=data.recall_mode, budget=data.budget)
                return {'preview': result, 'elapsed_ms': round((time.monotonic()-start)*1000, 2),
                        'clock': meta['clock'], 'read_only': True, 'cloud_calls': 0,
                        'model_mode': meta['model_mode'], 'embedding_mode': meta['embedding_mode']}
            finally:
                repo.close()

    def chat(self, identifier, data):
        with self._locked(identifier) as meta:
            repo = self._repo(meta)
            try:
                service, model = self._service(repo, meta)
                start = time.monotonic()
                result = service.chat(data.text, conversation_id=data.conversation_id, recall_mode=data.recall_mode)
                self._index(repo, meta)
                return result | {'cloud_calls': model.cloud_calls, 'model_mode': meta['model_mode'],
                                 'elapsed_ms': round((time.monotonic()-start)*1000, 2)}
            finally:
                repo.close()

    def delete(self, identifier):
        with self._locked(identifier) as meta:
            if self._active_runs(meta):
                raise MemoryError('lab_busy', '实验报告仍在执行，请结束后删除。', 409)
            repo = self._repo(meta)
            try:
                for run in meta['runs'].values():
                    if run.get('schema'):
                        self._drop(repo, run['schema'])
                self._drop(repo, meta['schema'])
            finally:
                repo.close()
            with self.guard:
                del self.registry['experiments'][str(identifier)]
                self._save()
            shutil.rmtree(Path(meta['journal']).parent)
            return {'deleted': True, 'id': identifier}

    def reflection_preview(self, identifier, automatic=False):
        with self._locked(identifier) as meta:
            repo = self._repo(meta)
            try:
                service, _ = self._reflection_service(repo, meta)
                return {'preview': service.preview(automatic=automatic), 'read_only': True,
                        'clock': meta['clock'], 'cloud_calls': 0, 'model_mode': meta['model_mode'],
                        'reflection_profile': meta.get('reflection_profile', 'off'),
                        'note': '模拟模式只验证调度与来源流程，不评价观点或情绪质量。'}
            finally:
                repo.close()

    def reflection_runs(self, identifier, run_id=None):
        runs = self._meta(identifier).get('reflection_runs', {})
        if run_id is None:
            return {'items': list(runs.values())}
        if str(run_id) not in runs:
            raise MemoryError('lab_reflection_not_found', '反思实验报告不存在。', 404)
        return runs[str(run_id)]

    def _update_reflection_run(self, identifier, run_id, **updates):
        with self.guard:
            self.registry['experiments'][identifier]['reflection_runs'][run_id].update(updates)
            self._save()

    def start_reflection(self, identifier, data):
        if self.stopping.is_set() or not self.run_slots.acquire(blocking=False):
            raise MemoryError('lab_queue_full', '实验运行队列已满或正在停止，请稍后再试。', 429)
        try:
            with self._locked(identifier) as meta:
                run = {'id': str(uuid4()), 'status': 'queued', 'automatic': data.automatic,
                       'clock': meta['clock'], 'model_mode': meta['model_mode'],
                       'reflection_profile': meta.get('reflection_profile', 'off'),
                       'cloud_calls': 0, 'cloud_call_limit': 2 if meta['model_mode'] == 'live' else 0,
                       'started_at': datetime.now(timezone.utc).isoformat(),
                       'note': '模拟模式只验证反思流程；真实反思最多两次请求，不自动重试。'}
                with self.guard:
                    self.registry['experiments'][str(identifier)]['reflection_runs'][run['id']] = run
                    self._save()
            self.executor.submit(self._execute_reflection, str(identifier), run['id'])
            return deepcopy(run)
        except Exception:
            self.run_slots.release()
            if 'run' in locals():
                self._update_reflection_run(str(identifier), run['id'], status='interrupted', error='lab_interrupted')
            raise

    def _execute_reflection(self, identifier, run_id):
        repo, model = None, None
        started = time.monotonic()
        try:
            with self._locked(identifier, wait=True, allow_run=True) as meta:
                if self.stopping.is_set():
                    raise MemoryError('lab_interrupted', '实验服务正在停止。', 503)
                run = meta['reflection_runs'][run_id]
                self._update_reflection_run(identifier, run_id, status='running')
                repo = self._repo(meta)
                service, model = self._reflection_service(repo, meta, LabCallBudget(run['cloud_call_limit']))
                before = repo.self_state()
                preview = service.preview(automatic=run['automatic'])
                self._update_reflection_run(identifier, run_id, before_state=before, preview=preview)
                if preview['eligible']:
                    queued = service.enqueue(automatic=run['automatic'])
                    result = service.execute(queued['id'])
                    if result is None:
                        result = repo.reflection_run(queued['id'])
                else:
                    result = {'status': 'skipped', 'reasons': preview.get('reasons', [])}
                self._update_reflection_run(identifier, run_id, result=result,
                    status='completed' if result and result.get('status') in ('completed', 'skipped') else 'failed',
                    actual_source_manifest=result.get('source_manifest', []) if result else [],
                    after_state=repo.self_state(), cloud_calls=model.cloud_calls,
                    elapsed_ms=round((time.monotonic()-started)*1000, 2))
        except Exception as exc:
            self._update_reflection_run(identifier, run_id,
                status='interrupted' if self.stopping.is_set() else 'failed',
                error=getattr(exc, 'code', type(exc).__name__), cloud_calls=getattr(model, 'cloud_calls', 0),
                elapsed_ms=round((time.monotonic()-started)*1000, 2))
        finally:
            if repo:
                repo.close()
            self.run_slots.release()

    def start_run(self, identifier, data):
        if any(day < 0 or day > 36500 for day in data.days):
            raise MemoryError('lab_time_range', '测试天数必须为0到36500。', 422)
        if self.stopping.is_set() or not self.run_slots.acquire(blocking=False):
            raise MemoryError('lab_queue_full', '实验运行队列已满或正在停止，请稍后再试。', 429)
        try:
            result = self._queue_run(identifier, data)
        except Exception:
            self.run_slots.release()
            raise
        try:
            self.executor.submit(self._execute_run, str(identifier), result['id'], result.pop('_cases'))
        except RuntimeError:
            self.run_slots.release()
            self._update_run(str(identifier), result['id'], status='interrupted', error='实验服务正在停止。')
            raise MemoryError('lab_interrupted', '实验服务正在停止。', 503)
        return result

    def _queue_run(self, identifier, data):
        with self._locked(identifier) as meta:
            if any(run['status'] in ('queued', 'running') for run in meta['runs'].values()):
                raise MemoryError('lab_busy', '同一实验已有运行中的报告。', 409)
            run_id = str(uuid4())
            cases = [probe for probe in PROBES if probe['expected_key'] in meta['fixture_events']]
            if not cases:
                raise MemoryError('lab_no_cases', '此实验没有内置场景，请手动查询或使用内置场景建立报告。', 400)
            case_count = len(cases) * len(data.days) * data.repeats
            arms = ['off', 'on'] if data.reflection_mode == 'paired' else [data.reflection_mode]
            per_case_limit = (3 * len(arms) if data.mode == 'chat' else 0) + (2 if 'on' in arms else 0)
            steps = case_count * len(arms)
            run = {'id': run_id, 'status': 'queued', 'mode': data.mode, 'days': data.days,
                   'repeats': data.repeats, 'steps': steps, 'completed': 0, 'results': [],
                   'reflection_mode': data.reflection_mode, 'arms': arms, 'cases': case_count,
                   'per_case_cloud_call_limit': per_case_limit if meta['model_mode'] == 'live' else 0,
                   'cloud_call_limit': case_count*per_case_limit if meta['model_mode'] == 'live' else 0,
                   'cloud_calls': 0, 'schema': 'lab_run_' + UUID(run_id).hex,
                   'scenario_version': SCENARIO_VERSION, 'policy_version': meta['policy_version'],
                   'policy_profile': meta['policy_profile'], 'policy_parameters': meta['policy_parameters'],
                   'model': self.settings.model_name if meta['model_mode'] == 'live' else 'fixture-mock',
                   'embedding': self.settings.embedding_model if meta['embedding_mode'] == 'local' else 'lexical-fake',
                   'started_at': datetime.now(timezone.utc).isoformat(), 'snapshot_clock': meta['clock']}
            with self.guard:
                self.registry['experiments'][str(identifier)]['runs'][run_id] = run
                self._save()
            return self._run_view(run) | {'_cases': cases}

    @staticmethod
    def _run_view(run):
        return {key: value for key, value in run.items() if key != 'schema'}

    def runs(self, identifier, run_id=None):
        runs = self._meta(identifier)['runs']
        if run_id is None:
            return {'items': [self._run_view(run) for run in runs.values()]}
        if str(run_id) not in runs:
            raise MemoryError('lab_run_not_found', '实验报告不存在。', 404)
        return self._run_view(runs[str(run_id)])

    def _update_run(self, identifier, run_id, **updates):
        with self.guard:
            self.registry['experiments'][identifier]['runs'][run_id].update(updates)
            self._save()

    @staticmethod
    def _context_checks(context, episode_ids, required):
        evidence = context.get('experience_evidence', [])
        selected = {str(item.get('episode_id', item.get('id'))) for item in evidence}
        targets = set(episode_ids)
        quotes = [detail.get('quote', '') for item in evidence
                  if str(item.get('episode_id', item.get('id'))) in targets
                  for detail in item.get('details', [])]
        return {'selected_episode_ids': sorted(selected),
                'selected_contains_expected_source': bool(targets & selected),
                'context_quote_checks': {quote: any(quote in actual for actual in quotes) for quote in required}}

    def _run_case(self, repo, meta, run, case, day, repeat, arm, budget):
        started = time.monotonic()
        row = {'case': case['id'], 'query': case['query'], 'day': day, 'repeat': repeat,
               'arm': arm, 'pair_id': f"{repeat}:{day}:{case['id']}",
               'required_quotes': case['required'], 'status': 'completed', 'cloud_calls': 0,
               'candidate_contains_expected_source': False,
               'selected_contains_expected_source': False, 'selected_episode_ids': [],
               'context_quote_checks': {quote: False for quote in case['required']},
               'context_basis': 'answer.actual_context' if run['mode'] == 'chat' else 'preview',
               'context_evaluated': False, 'model_mode': meta['model_mode']}
        model, reflection_model = None, None
        try:
            reflection, reflection_model = self._reflection_service(repo, meta, budget)
            reflection.set_enabled(arm == 'on')
            before = repo.self_state()
            reflection_preview = reflection.preview(automatic=True)
            row['reflection'] = {'enabled': arm == 'on', 'preview': reflection_preview,
                                 'before_state': before, 'model_mode': meta['model_mode']}
            if arm == 'on':
                if reflection_preview['eligible']:
                    queued = reflection.enqueue(automatic=True)
                    result = reflection.execute(queued['id'])
                    if result is None:
                        result = repo.reflection_run(queued['id'])
                else:
                    result = {'status': 'skipped', 'reasons': reflection_preview.get('reasons', [])}
                row['reflection']['result'] = result
                row['reflection']['actual_source_manifest'] = result.get('source_manifest', []) if result else []
                if result is None or result.get('status') not in ('completed', 'skipped'):
                    row.update(status='failed', error='lab_reflection_failed')
            row['reflection']['after_state'] = repo.self_state()
            row['reflection']['cloud_calls'] = reflection_model.cloud_calls
            service, model = self._service(repo, meta)
            if isinstance(model, MeteredMiniMax):
                model.budget = budget
            preview = service.recall_preview(case['query'], recall_mode='natural')
            experience_id = meta['fixture_events'][case['expected_key']]
            with repo.pool.connection() as connection:
                episode_ids = [str(item['episode_id']) for item in connection.execute(
                    'SELECT DISTINCT episode_id FROM episode_evidence WHERE experience_id=%s',
                    (experience_id,)).fetchall()]
            context = preview.get('payload', preview.get('context', {}))
            preview_checks = self._context_checks(context, episode_ids, case['required'])
            preview_hit = any(str(item.get('id')) in episode_ids for item in preview.get('candidates', []))
            row.update(preview=preview, expected_experience_id=experience_id,
                       preview_candidate_contains_expected_source=preview_hit,
                       **{'preview_' + key: value for key, value in preview_checks.items()})
            if run['mode'] == 'chat':
                answer = service.chat(case['query'])
                row['answer'] = answer
                actual = answer.get('actual_context')
                if not isinstance(actual, dict) or not isinstance(actual.get('experience_evidence'), list):
                    raise MemoryError('lab_actual_context_missing', '实验回答缺少实际上下文，不能评价证据命中。', 502)
                row.update(self._context_checks(actual, episode_ids, case['required']),
                    context_evaluated=True, actual_next_context=actual,
                    candidate_contains_expected_source=any(str(item.get('id')) in episode_ids
                        for item in answer.get('recall_trace', {}).get('candidates', [])))
                row['answer_quote_checks'] = {quote: quote in answer['answer'] for quote in case['required']}
                row['answer_quality_evaluated'] = meta['model_mode'] == 'live'
            else:
                row.update(preview_checks, candidate_contains_expected_source=preview_hit, context_evaluated=True)
        except Exception as exc:
            row.update(status='failed', error=getattr(exc, 'code', type(exc).__name__))
        finally:
            row['cloud_calls'] = getattr(model, 'cloud_calls', 0) + getattr(reflection_model, 'cloud_calls', 0)
            if 'reflection' in row:
                row['reflection']['cloud_calls'] = getattr(reflection_model, 'cloud_calls', 0)
            row['elapsed_ms'] = round((time.monotonic()-started)*1000, 2)
        return jsonable_encoder(row)

    def _execute_run(self, identifier, run_id, cases):
        repo = None
        try:
            if self.stopping.is_set():
                raise MemoryError('lab_interrupted', '实验服务正在停止。', 503)
            with self._locked(identifier, wait=True, allow_run=True) as meta:
                run = meta['runs'][run_id]
                self._update_run(identifier, run_id, status='running')
                source_repo = self._repo(meta)
                source_repo.close()
                run_directory = Path(meta['journal']).parent / run_id
                run_directory.mkdir(mode=0o700)
                journal = run_directory / 'deletions.jsonl'
                journal.touch(mode=0o600)
                budget = LabCallBudget(run['cloud_call_limit'])
                results, cloud_calls = [], 0
                for repeat in range(run['repeats']):
                    for day in run['days']:
                        for case in cases:
                            for arm in run.get('arms', ['off']):
                                if self.stopping.is_set():
                                    raise MemoryError('lab_interrupted', '实验服务正在停止。', 503)
                                # A separate random schema per arm keeps real-day quotas
                                # isolated. The run-wide ledger is deliberately outside it.
                                work = meta | {'schema': 'lab_run_' + uuid4().hex, 'journal': str(journal)}
                                self._update_run(identifier, run_id, schema=work['schema'])
                                repo = self._repo(work, replay=False)
                                repo.migrate()
                                self._copy_schema(repo, meta['schema'], work['schema'])
                                shutil.copyfile(meta['journal'], journal)
                                repo.reset_reflections_after_restore(reason='cloned')
                                repo.clock = MutableClock(datetime.fromisoformat(meta['clock']) + timedelta(days=day))
                                row = self._run_case(repo, work, run, case, day, repeat, arm, budget)
                                cloud_calls += row['cloud_calls']
                                results.append(row)
                                self._update_run(identifier, run_id, results=results, completed=len(results), cloud_calls=cloud_calls)
                                self._drop(repo, work['schema'])
                                repo.close()
                                repo = None
                arm_metrics = {}
                for arm in run.get('arms', ['off']):
                    items = [row for row in results if row['arm'] == arm]
                    arm_metrics[arm] = {
                        'steps': len(items), 'failed_steps': sum(row['status'] == 'failed' for row in items),
                        'selected_hit_count': sum(row['selected_contains_expected_source'] for row in items),
                        'context_quote_hit_count': sum(sum(row['context_quote_checks'].values()) for row in items),
                        'cloud_calls': sum(row['cloud_calls'] for row in items)}
                self._update_run(identifier, run_id, status='completed', results=results, cloud_calls=cloud_calls,
                    completed=len(results), failed_steps=sum(row['status']=='failed' for row in results),
                    metrics={
                        'candidate_hit_count': sum(row['candidate_contains_expected_source'] for row in results),
                        'selected_hit_count': sum(row['selected_contains_expected_source'] for row in results),
                        'expected_quote_count': sum(len(row['context_quote_checks']) for row in results),
                        'context_quote_hit_count': sum(sum(row['context_quote_checks'].values()) for row in results),
                        'elapsed_ms_p50': sorted(row['elapsed_ms'] for row in results)[len(results)//2],
                        'elapsed_ms_p95': sorted(row['elapsed_ms'] for row in results)[min(len(results)-1, math.ceil(len(results)*.95)-1)],
                        'steps': len(results), 'arms': arm_metrics},
                    note='模拟输出不评价模型质量。每个时间点的 off/on 从同一快照独立运行；反思最多2次、对话最多3次、成对完整对话最多8次。所有时间点共享总调用上限，失败请求与有界对话重试均计入；失败场景不重复执行。chat证据检查仅使用answer.actual_context。')
        except Exception as exc:
            self._update_run(identifier, run_id, status='interrupted' if self.stopping.is_set() else 'failed',
                             error=getattr(exc, 'code', type(exc).__name__))
        finally:
            if repo:
                try:
                    self._drop(repo, repo.settings.db_schema)
                finally:
                    repo.close()
            self.run_slots.release()


def register_lab(app, settings, authenticated):
    if not getattr(settings, 'lab_enabled', False):
        return
    manager = LabManager(settings)
    app.state.lab = manager
    dependencies = [Depends(authenticated)]
    prefix = '/v1/lab'

    @app.get('/lab', include_in_schema=False)
    def lab_page():
        return FileResponse(Path(__file__).with_name('static') / 'lab.html')

    @app.get('/lab-assets/{filename}', include_in_schema=False)
    def lab_asset(filename: Literal['lab.js', 'lab.css']):
        return FileResponse(Path(__file__).with_name('static') / filename)

    @app.get(prefix+'/scenarios', dependencies=dependencies)
    def get_scenarios():
        return {'items': scenarios(), 'synthetic_only': True,
                'policies': [{'id': key, 'parameters': asdict(policy)} for key, policy in LAB_POLICIES.items()]}

    @app.get(prefix+'/experiments', dependencies=dependencies)
    def list_experiments():
        return manager.listing()

    @app.post(prefix+'/experiments', dependencies=dependencies, status_code=201)
    def create_experiment(data: CreateExperiment):
        return manager.create(data)

    @app.get(prefix+'/experiments/{identifier}', dependencies=dependencies)
    def experiment_detail(identifier: UUID):
        return manager.detail(str(identifier))

    @app.post(prefix+'/experiments/{identifier}/clone', dependencies=dependencies, status_code=201)
    def clone_experiment(identifier: UUID, data: CloneExperiment):
        return manager.clone(str(identifier), data)

    @app.delete(prefix+'/experiments/{identifier}', dependencies=dependencies)
    def delete_experiment(identifier: UUID):
        return manager.delete(str(identifier))

    @app.post(prefix+'/experiments/{identifier}/events', dependencies=dependencies, status_code=201)
    def add_event(identifier: UUID, data: AddEvent):
        return manager.event(str(identifier), data)

    @app.post(prefix+'/experiments/{identifier}/advance', dependencies=dependencies)
    def advance_time(identifier: UUID, data: AdvanceTime):
        return manager.advance(str(identifier), data)

    @app.post(prefix+'/experiments/{identifier}/probe', dependencies=dependencies)
    def probe(identifier: UUID, data: ProbeInput):
        return manager.probe(str(identifier), data)

    @app.post(prefix+'/experiments/{identifier}/chat', dependencies=dependencies)
    def chat(identifier: UUID, data: ChatInput):
        return manager.chat(str(identifier), data)

    @app.get(prefix+'/experiments/{identifier}/reflection-preview', dependencies=dependencies)
    def reflection_preview(identifier: UUID, automatic: bool = False):
        return manager.reflection_preview(str(identifier), automatic=automatic)

    @app.get(prefix+'/experiments/{identifier}/reflection-runs', dependencies=dependencies)
    def list_reflection_runs(identifier: UUID):
        return manager.reflection_runs(str(identifier))

    @app.post(prefix+'/experiments/{identifier}/reflection-runs', dependencies=dependencies, status_code=202)
    def start_reflection(identifier: UUID, data: ReflectionInput):
        return manager.start_reflection(str(identifier), data)

    @app.get(prefix+'/experiments/{identifier}/reflection-runs/{run_id}', dependencies=dependencies)
    def get_reflection_run(identifier: UUID, run_id: UUID):
        return manager.reflection_runs(str(identifier), str(run_id))

    @app.get(prefix+'/experiments/{identifier}/runs', dependencies=dependencies)
    def list_runs(identifier: UUID):
        return manager.runs(str(identifier))

    @app.post(prefix+'/experiments/{identifier}/runs', dependencies=dependencies, status_code=202)
    def start_run(identifier: UUID, data: RunInput):
        return manager.start_run(str(identifier), data)

    @app.get(prefix+'/experiments/{identifier}/runs/{run_id}', dependencies=dependencies)
    def get_run(identifier: UUID, run_id: UUID):
        return manager.runs(str(identifier), str(run_id))
