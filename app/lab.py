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
    start_time: AwareDatetime = datetime.fromisoformat(START_TIME)
    synthetic: Literal[True] = True


class CloneExperiment(LabInput):
    name: str = Field(default='实验快照副本', min_length=1, max_length=100)
    model_mode: Literal['mock', 'live'] | None = None
    embedding_mode: Literal['fake', 'local'] | None = None
    policy_profile: Literal['balanced', 'strict'] | None = None


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

    def decide_memories(self, system, prompt):
        return {'decision': {'actions': []}, 'usage': {}}

    def reassess_memories(self, system, prompt):
        return {'decision': {}, 'usage': {}}

    def chat(self, system, messages):
        return {'text': '模拟回答：本轮只验证记忆流程。请在右侧查看实际选中的证据；此输出不用于评价模型回答质量。',
                'usage': {'input_tokens': 0, 'output_tokens': 0}}


class MeteredMiniMax(MiniMax):
    def __init__(self, settings):
        super().__init__(settings)
        self.cloud_calls = 0

    def _request(self, *args, **kwargs):
        if self.cloud_calls >= 3:
            raise MemoryError('lab_call_budget', '本轮已达到三次云调用上限。', 429)
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
                if 'policy_parameters' not in experiment:
                    experiment['policy_parameters'] = asdict(LAB_POLICIES['balanced'])
                    experiment['policy_version'] = LAB_POLICIES['balanced'].version
                for run in experiment.get('runs', {}).values():
                    if run['status'] in ('queued', 'running'):
                        run.update(status='interrupted', error='实验服务重启，运行未完成；结果没有自动重试。')
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
            if not allow_run and any(run['status'] in ('queued', 'running') for run in meta['runs'].values()):
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
                repo.replay_deletions(required=True)
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
    def _public(meta):
        return {key: value for key, value in meta.items() if key not in ('schema', 'journal', 'runs')}

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
                'fixture_events': {}, 'runs': {}}

    def create(self, data):
        if data.model_mode == 'live' and not self.settings.model_key:
            raise MemoryError('model_not_configured', '未配置真实模型；可以使用模拟模式。', 400)
        meta = self._new_meta(data)
        repo = self._repo(meta, replay=False)
        try:
            repo.migrate()
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
                policy_profile=data.policy_profile or source['policy_profile']))
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
            if any(run['status'] in ('queued', 'running') for run in meta['runs'].values()):
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
            steps = len(cases) * len(data.days) * data.repeats
            run = {'id': run_id, 'status': 'queued', 'mode': data.mode, 'days': data.days,
                   'repeats': data.repeats, 'steps': steps, 'completed': 0, 'results': [],
                   'cloud_call_limit': steps*3 if data.mode == 'chat' and meta['model_mode'] == 'live' else 0,
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

    def _execute_run(self, identifier, run_id, cases):
        repo = None
        try:
            # Holding the source experiment prevents edits during snapshot comparisons.
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
                work = meta | {'schema': run['schema'], 'journal': str(journal)}
                repo = self._repo(work, replay=False)
                repo.migrate()
                results, cloud_calls = [], 0
                for repeat in range(run['repeats']):
                    for day in run['days']:
                        for case in cases:
                            if self.stopping.is_set():
                                raise MemoryError('lab_interrupted', '实验服务正在停止。', 503)
                            self._copy_schema(repo, meta['schema'], work['schema'])
                            shutil.copyfile(meta['journal'], journal)
                            repo.clock = MutableClock(datetime.fromisoformat(meta['clock']) + timedelta(days=day))
                            service, model = self._service(repo, work)
                            started = time.monotonic()
                            preview = service.recall_preview(case['query'], recall_mode='natural')
                            experience_id = meta['fixture_events'][case['expected_key']]
                            with repo.pool.connection() as connection:
                                episode_ids = [str(row['episode_id']) for row in connection.execute(
                                    'SELECT DISTINCT episode_id FROM episode_evidence WHERE experience_id=%s',
                                    (experience_id,)).fetchall()]
                            context = preview.get('payload', preview.get('context', {}))
                            preview_checks = self._context_checks(context, episode_ids, case['required'])
                            preview_candidate_hit = any(str(item.get('id')) in episode_ids
                                                        for item in preview.get('candidates', []))
                            row = {'case': case['id'], 'query': case['query'], 'day': day, 'repeat': repeat,
                                   'preview': preview, 'expected_experience_id': experience_id,
                                   'preview_candidate_contains_expected_source': preview_candidate_hit,
                                   **{'preview_' + key: value for key, value in preview_checks.items()},
                                   'candidate_contains_expected_source': preview_candidate_hit,
                                   **preview_checks, 'context_basis': 'preview', 'context_evaluated': True,
                                   'required_quotes': case['required'], 'status': 'completed'}
                            if run['mode'] == 'chat':
                                # A diagnostic preview has a different budget and
                                # cannot prove what a subsequent answer received.
                                row.update(self._context_checks({}, episode_ids, case['required']),
                                    candidate_contains_expected_source=False,
                                    context_basis='answer.actual_context', context_evaluated=False)
                                try:
                                    answer = service.chat(case['query'])
                                    row['answer'] = answer
                                    actual = answer.get('actual_context')
                                    if not isinstance(actual, dict) or not isinstance(actual.get('experience_evidence'), list):
                                        raise MemoryError('lab_actual_context_missing', '实验回答缺少实际上下文，不能评价证据命中。', 502)
                                    row.update(self._context_checks(actual, episode_ids, case['required']),
                                        context_evaluated=True,
                                        candidate_contains_expected_source=any(str(item.get('id')) in episode_ids
                                            for item in answer.get('recall_trace', {}).get('candidates', [])))
                                    row['answer_quote_checks'] = {quote: quote in answer['answer'] for quote in case['required']}
                                    row['answer_quality_evaluated'] = meta['model_mode'] == 'live'
                                except Exception as exc:
                                    row.update(status='failed', error=getattr(exc, 'code', type(exc).__name__))
                            row.update(elapsed_ms=round((time.monotonic()-started)*1000, 2), cloud_calls=model.cloud_calls)
                            cloud_calls += model.cloud_calls
                            results.append(jsonable_encoder(row))
                            self._update_run(identifier, run_id, results=results, completed=len(results), cloud_calls=cloud_calls)
                self._update_run(identifier, run_id, status='completed', results=results, cloud_calls=cloud_calls,
                    completed=len(results), failed_steps=sum(row['status']=='failed' for row in results),
                    metrics={
                        'candidate_hit_count': sum(row['candidate_contains_expected_source'] for row in results),
                        'selected_hit_count': sum(row['selected_contains_expected_source'] for row in results),
                        'expected_quote_count': sum(len(row['context_quote_checks']) for row in results),
                        'context_quote_hit_count': sum(sum(row['context_quote_checks'].values()) for row in results),
                        'elapsed_ms_p50': sorted(row['elapsed_ms'] for row in results)[len(results)//2],
                        'elapsed_ms_p95': sorted(row['elapsed_ms'] for row in results)[min(len(results)-1, math.ceil(len(results)*.95)-1)],
                        'steps': len(results)},
                    note='模拟输出不评价回答质量；chat的最终选择和原话检查使用answer.actual_context，preview字段单列。失败场景不重复执行；生产瞬时重试计入每轮3次及本次运行上限，所有尝试均保留。')
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

    @app.get(prefix+'/experiments/{identifier}/runs', dependencies=dependencies)
    def list_runs(identifier: UUID):
        return manager.runs(str(identifier))

    @app.post(prefix+'/experiments/{identifier}/runs', dependencies=dependencies, status_code=202)
    def start_run(identifier: UUID, data: RunInput):
        return manager.start_run(str(identifier), data)

    @app.get(prefix+'/experiments/{identifier}/runs/{run_id}', dependencies=dependencies)
    def get_run(identifier: UUID, run_id: UUID):
        return manager.runs(str(identifier), str(run_id))
