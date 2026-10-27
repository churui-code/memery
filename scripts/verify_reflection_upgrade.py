#!/usr/bin/env python3
"""Nonempty schema 5 -> 6 encrypted restore drill; standard-library host only.

Uses existing, immutable image IDs, an isolated internal Compose network, fresh
secrets and a separate deletion journal. No API, worker, reflector or cloud model
is started. All data and encrypted snapshots remain under ignored data/qa/.
"""
import argparse
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
PROJECT = 'home-memory-v05-upgrade'
DB_IMAGE = 'pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b'
REPORT = ROOT / 'docs/verification-v0.5-upgrade.json'
HISTORY = ROOT / 'docs/verification-history'

# Executed inside the selected release image, with no source-code bind mount.
# Only IDs, column names, counts and digests leave the synthetic database.
REPOSITORY_SCRIPT = r'''
from hashlib import sha256
import json
from uuid import UUID, uuid4
from psycopg import sql
from app.config import Settings
from app.errors import MemoryError
from app.life import ExperienceDescription, SelfUpdate
from app.repository import Repository

TABLES = ('memories','events','memory_evidence','experiences','messages',
          'episodes','episode_evidence','episode_details','episode_cues',
          'episode_versions','episode_associations','episode_rehearsals',
          'self_observations','context_links')

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)

def snapshot(repo, previous=None):
    result={}
    with repo.pool.connection() as c:
        for table in TABLES:
            columns=previous[table]['columns'] if previous else [r['column_name'] for r in c.execute(
                'SELECT column_name FROM information_schema.columns WHERE table_schema=%s '
                'AND table_name=%s ORDER BY ordinal_position',(repo.settings.db_schema,table)).fetchall()]
            assert columns,table
            query=sql.SQL('SELECT {} FROM {}').format(
                sql.SQL(',').join(sql.Identifier(name) for name in columns),sql.Identifier(table))
            rows=c.execute(query).fetchall()
            encoded=canonical(sorted(canonical(row) for row in rows)).encode()
            result[table]={'columns':columns,'count':len(rows),'sha256':sha256(encoded).hexdigest()}
    return result

def prepare(repo):
    from app.context import content_hash
    events=repo.reflection_candidates()
    assert events,'synthetic pending episodes missing'
    refs=[]
    for event in events:
        refs.append({'source_type':'episode','id':str(event['id']),'revision':event['revision'],
                     'source_hash':event['source_hash'],'hash':content_hash(str(event['id'])),
                     'evidence_ids':[str(row['id']) for row in event['evidence']],'pending':True})
        for eid in event['source_experience_ids']:
            original=repo.experience(eid)
            refs.append({'source_type':'experience','id':str(eid),'revision':original['revision'],
                         'source_hash':original['source_hash'],'hash':original['source_hash'],'spans':[]})
    return {'manifest':refs,'expected_epoch':repo.epoch(),'target_versions':[],
            'input_signature':content_hash([refs,[]]),
            'pending_ids':[str(event['id']) for event in events]}

def quota(repo):
    from app.backup import export_reflection_quota
    return export_reflection_quota(repo)

def no_resurrection(repo, ids):
    for method,key in ((repo.get,'memory_id'),(repo.experience,'experience_id'),(repo.episode,'episode_id')):
        try:
            row=method(UUID(ids[key]))
        except MemoryError as exc:
            assert exc.code=='not_found',(key,exc.code)
        else:
            assert row is None or row.get('deleted_at'),key+' remains visible'
    assert not any(str(row['id'])==ids['self_id'] for row in repo.self_state())
    with repo.pool.connection() as c:
        for table,key,fields in (
            ('memories','memory_id',('text','request_hash')),
            ('experiences','experience_id',('user_text','assistant_text','title','summary','source_hash')),
            ('episodes','episode_id',('title','gist','importance_reason','emotional_reason','source_hash')),
            ('self_observations','self_id',('text','reason','evidence_quote'))):
            row=c.execute(sql.SQL('SELECT * FROM {} WHERE id=%s').format(sql.Identifier(table)),
                          (ids[key],)).fetchone()
            assert row and row['deleted_at'] and all(row[field]=='' for field in fields),table
        assert not c.execute('SELECT 1 FROM messages WHERE experience_id=%s',(ids['experience_id'],)).fetchone()
        assert not c.execute('SELECT 1 FROM episode_evidence WHERE episode_id=%s',(ids['episode_id'],)).fetchone()
        assert not c.execute('SELECT 1 FROM events WHERE memory_id=%s AND content<>%s',
                             (ids['memory_id'],'')).fetchone()
        assert not c.execute('SELECT 1 FROM episode_versions WHERE episode_id=%s AND (title<>%s OR gist<>%s)',
                             (ids['episode_id'],'','')).fetchone()
    return {'source_and_dependencies_invisible':True,'stored_text_scrubbed':True}

repo=Repository(Settings())
try:
    action=INPUT['action']
    if action=='seed_v5':
        repo.migrate()
        repo.replay_deletions(required=True)
        with repo.pool.connection() as c:
            assert c.execute('SELECT max(version) AS v FROM schema_migrations').fetchone()['v']==5
        assert repo.health()['version']=='0.4.0'
        fact,_=repo.remember({'text':'合成升级验收：紫藤故事中的承诺需要后续行动。'})
        text='  合成升级验收：在虚构紫藤故事里，承诺通过行动体现。\n重复词：紫藤、紫藤；Unicode：é / e\u0301 / 🌿。  '
        answer='\n合成回复：这是虚构故事中的解释，保留原话与空白。  '
        result=repo.save_answer(uuid4(),text,answer,[fact['id']],repo.epoch(),
            experience_description=ExperienceDescription(title='合成紫藤故事',summary='合成承诺与行动的讨论。',
                context_type='fiction',participants=['合成甲','合成乙'],significance=.8),
            self_updates=[SelfUpdate(domain='belief',key='升级验收承诺',text='承诺通过后续行动体现。',
                reason='来自虚构故事的合成讨论。',quote='承诺通过行动体现',confidence=.7)])
        assert len(result['episode_ids'])==1 and len(result['self_changes'])==1
        original=repo.experience(result['experience_id'])
        assert original['user_text']==text and original['assistant_text']==answer
        ids={'memory_id':str(fact['id']),'experience_id':str(result['experience_id']),
             'episode_id':str(result['episode_ids'][0]),'self_id':str(result['self_changes'][0]['id'])}
        rows=snapshot(repo)
        assert all(rows[name]['count']>0 for name in
                   ('memories','experiences','episodes','episode_evidence','self_observations','messages','context_links'))
        output={'schema':5,'ids':ids,'snapshot':rows,'model_call_count':0}
        with repo.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM model_calls').fetchone()['n']==0
    elif action=='snapshot':
        with repo.pool.connection() as c:
            version=c.execute('SELECT max(version) AS v FROM schema_migrations').fetchone()['v']
        output={'schema':version,'snapshot':snapshot(repo,INPUT['snapshot'])}
        if version==6:
            state=repo.reflection_settings()
            output.update(enabled=state['enabled'],quota=quota(repo))
            with repo.pool.connection() as c:
                output['conversation_generation_preserved']=not c.execute(
                    "SELECT 1 FROM self_observations WHERE generation_type<>'conversation' "
                    "OR reflection_run_id IS NOT NULL OR evidence_manifest<>'[]'::jsonb").fetchone()
    elif action=='enqueue':
        repo.set_reflection_enabled(True)
        run=repo.enqueue_reflection(**prepare(repo))
        assert run['status']=='pending'
        output={'id':str(run['id']),'quota':quota(repo)}
    elif action=='failed_then_running':
        job=repo.claim_reflection(UUID(INPUT['pending_id']))
        assert job and job['status']=='running'
        cid=repo.start_reflection_call(job,'reflection_proposal',estimated_input_tokens=100,
                                       estimator_version='upgrade-drill-v1')
        repo.finish_call(cid,'synthetic_request_failed')
        failed=repo.fail_reflection(job,'synthetic_request_failed')
        assert failed['status']=='failed' and failed['cloud_calls']==1
        after_failure=quota(repo)
        assert sum(row['rounds'] for row in after_failure['days'])==1
        assert sum(row['calls'] for row in after_failure['days'])==1
        run=repo.enqueue_reflection(**prepare(repo))
        running=repo.claim_reflection(run['id'])
        assert running and running['status']=='running'
        cid=repo.start_reflection_call(running,'reflection_proposal',estimated_input_tokens=100,
                                       estimator_version='upgrade-drill-v1')
        output={'failed_id':str(job['id']),'running_id':str(running['id']),
                'running_token':str(running['lease_token']),'running_call_id':str(cid),
                'after_failure':after_failure,'quota':quota(repo)}
    elif action=='later_failure':
        # A second paid attempt occurs after the encrypted v6 snapshot. This
        # simulated failed request must survive rollback through quota-merge.
        job=repo.reflection_run(UUID(INPUT['running_id']))
        assert str(job['lease_token'])==INPUT['running_token']
        repo.finish_call(UUID(INPUT['running_call_id']),'reflection_proposal_completed',
                         {'input_tokens':11,'output_tokens':7})
        cid=repo.start_reflection_call(job,'reflection_review',estimated_input_tokens=100,
                                       estimator_version='upgrade-drill-v1')
        repo.finish_call(cid,'synthetic_request_failed')
        repo.fail_reflection(job,'synthetic_request_failed')
        output={'quota':quota(repo)}
        assert sum(row['rounds'] for row in output['quota']['days'])==2
        assert sum(row['calls'] for row in output['quota']['days'])==3
    elif action=='assert_reset':
        state=repo.reflection_settings()
        assert not state['enabled'] and state['active_run_id'] is None
        run=repo.reflection_run(UUID(INPUT['run_id']))
        assert run['status']=='cancelled' and run['error_code']=='restored'
        assert run['lease_token'] is None and run['lease_until'] is None
        assert repo.claim_reflection(UUID(INPUT['run_id'])) is None
        with repo.pool.connection() as c:
            assert not c.execute("SELECT 1 FROM reflection_runs WHERE status IN ('pending','running') "
                                 'OR lease_token IS NOT NULL OR lease_until IS NOT NULL').fetchone()
            assert not c.execute("SELECT 1 FROM model_calls WHERE reflection_run_id IS NOT NULL AND status='running'").fetchone()
        if INPUT.get('running_call_id'):
            assert any(str(call['id'])==INPUT['running_call_id'] and call['status']=='interrupted'
                       for call in run['calls'])
            old_job={**run,'lease_token':UUID(INPUT['running_token'])}
            try:
                repo.start_reflection_call(old_job,'reflection_review')
            except MemoryError as exc:
                assert exc.code=='reflection_lease_lost'
            else:
                raise AssertionError('restored lease was reusable')
            failed=repo.reflection_run(UUID(INPUT['failed_id']))
            assert failed['status']=='failed' and failed['calls'][0]['status']=='synthetic_request_failed'
        assert quota(repo)==INPUT['quota'],'restored quota did not retain live high-water marks'
        output={'cancelled':str(run['id']),'enabled':False,'quota':quota(repo),
                'running_calls_interrupted':True,'snapshot':snapshot(repo,INPUT['snapshot'])}
    elif action=='delete':
        repo.forget(UUID(INPUT['ids']['memory_id']))
        output=no_resurrection(repo,INPUT['ids'])
        output['journal_records']=len(repo.journal.records(required=True))
    elif action=='assert_deleted':
        output=no_resurrection(repo,INPUT['ids'])
        assert not repo.reflection_settings()['enabled']
        assert quota(repo)==INPUT['quota']
        output.update(quota=quota(repo),enabled=False)
    else:
        raise ValueError('unknown drill action')
    print(json.dumps(output,ensure_ascii=False,default=str))
finally:
    repo.close()
'''


class Drill:
    def __init__(self, directory, report):
        self.directory = directory
        self.report = report
        self.secrets = {}
        self.owns_project = False
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('COMPOSE_')}
        self.compose = ['docker', 'compose', '--project-directory', str(directory),
                        '--env-file', str(directory / 'empty.env'), '-p', PROJECT,
                        '-f', str(directory / 'compose.json')]

    def redact(self, value):
        for secret in self.secrets.values():
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value

    def command(self, args, *, data=None, stdin=None, stdout=subprocess.PIPE, timeout=180):
        completed = subprocess.run(args, input=data, stdin=stdin, stdout=stdout,
                                   stderr=subprocess.PIPE, cwd=ROOT, env=self.env, timeout=timeout)
        if completed.returncode:
            detail = self.redact(completed.stderr.decode(errors='replace'))[-6000:]
            raise RuntimeError(f'command failed ({completed.returncode}): {args[0]}\n{detail}')
        return completed.stdout

    def run(self, args, **kwargs):
        return self.command(self.compose + args, **kwargs)

    def stage(self, name):
        self.report['stage'] = name
        print(name, flush=True)

    def write_configuration(self, images):
        for name in ('secrets', 'postgres', 'journal', 'backups'):
            (self.directory / name).mkdir(mode=0o700)
        self.secrets = {'db_admin_password': secrets.token_urlsafe(32),
                        'db_app_password': secrets.token_urlsafe(32),
                        'backup_key': secrets.token_hex(32)}
        for name, value in self.secrets.items():
            self.private_write(self.directory / 'secrets' / name, (value + '\n').encode())
        self.private_write(self.directory / 'journal/deletions.jsonl', b'')
        self.private_write(self.directory / 'empty.env', b'')
        environment = {'DB_HOST': 'db', 'DB_NAME': 'memory', 'DB_USER': 'memory_app',
                       'DB_PASSWORD_FILE': '/run/secrets/db_app_password',
                       'BACKUP_KEY_FILE': '/run/secrets/backup_key',
                       'DELETION_JOURNAL': '/data/journal/deletions.jsonl',
                       'MINIMAX_API_KEY': '', 'MINIMAX_BASE_URL': 'http://127.0.0.1:9',
                       'MEMORY_LAB': 'false', 'HF_HUB_OFFLINE': '1',
                       'TRANSFORMERS_OFFLINE': '1', 'TZ': 'Asia/Shanghai'}
        runner = {'restart': 'no', 'pull_policy': 'never', 'init': True,
                  'environment': environment, 'profiles': ['drill'],
                  'volumes': [{'type': 'bind', 'source': str(self.directory / 'journal'),
                               'target': '/data/journal'}],
                  'secrets': ['db_app_password', 'backup_key'], 'command': ['python', '-']}
        config = {'services': {
            'db': {'image': images['database']['id'], 'restart': 'no', 'pull_policy': 'never',
                   'environment': {'POSTGRES_DB': 'memory', 'POSTGRES_USER': 'memory_admin',
                                   'POSTGRES_PASSWORD_FILE': '/run/secrets/db_admin_password',
                                   'TZ': 'Asia/Shanghai'},
                   'secrets': ['db_admin_password'],
                   'volumes': [{'type': 'bind', 'source': str(self.directory / 'postgres'),
                                'target': '/var/lib/postgresql/data'}],
                   'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'memory_admin', '-d', 'memory'],
                                   'interval': '2s', 'timeout': '3s', 'retries': 45}},
            'old': {**runner, 'image': images['old']['id']},
            'new': {**runner, 'image': images['new']['id']}},
            'networks': {'default': {'internal': True}},
            'secrets': {name: {'file': str(self.directory / 'secrets' / name)} for name in self.secrets}}
        self.private_write(self.directory / 'compose.json', json.dumps(config, indent=2).encode())

    @staticmethod
    def private_write(path, body):
        with path.open('xb') as handle:
            os.chmod(path, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    def sql(self, query, database='postgres'):
        return self.run(['exec', '-T', 'db', 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                         '-U', 'memory_admin', '-d', database], data=query.encode())

    def python(self, action, database='memory', image='new', **payload):
        source = 'INPUT=json.loads(' + repr(json.dumps({'action': action, **payload})) + ')\n'
        # json must be imported before decoding the host-provided fixture IDs.
        source = 'import json\n' + source + REPOSITORY_SCRIPT
        output = self.run(['run', '--rm', '-T', '--no-deps', '--pull', 'never',
                           '-e', 'DB_NAME=' + database, image, 'python', '-'], data=source.encode())
        return json.loads(output)

    def backup_action(self, action, database='memory', data=None):
        result = self.run(['run', '--rm', '-T', '--no-deps', '--pull', 'never',
                           '-e', 'DB_NAME=' + database, 'new', 'python', '-m', 'app.backup', action],
                          data=json.dumps(data).encode() if data is not None else None)
        return json.loads(result)

    def encrypted_backup(self, database, name, image='new'):
        path = self.directory / 'backups' / name
        # Synthetic fixture is small; plaintext stays in pipes/host memory and
        # is never persisted. Both subprocess return codes are checked.
        dump = self.run(['exec', '-T', 'db', 'pg_dump', '-U', 'memory_admin', '-d', database,
                         '--format=custom', '--schema=memory', '--no-owner', '--no-privileges'])
        assert dump.startswith(b'PGDMP')
        encrypted = self.run(['run', '--rm', '-T', '--no-deps', '--pull', 'never', image,
                              'python', '-m', 'app.backup', 'encrypt'], data=dump)
        assert encrypted.startswith((b'HOME-MEMORY-1\0', b'HOME-MEMORY-2\0'))
        self.private_write(path, encrypted)
        self.report['backups'].append({'path': str(path.relative_to(ROOT)),
                                       'bytes': len(encrypted), 'sha256': sha256(encrypted).hexdigest(),
                                       'source_database': database, 'encryption_image': image})
        return path

    def restore(self, path, database):
        # Database names are script-owned constants, never user input.
        if not database.startswith('upgrade_') or not database.replace('_', '').isalnum():
            raise ValueError('invalid drill database')
        decrypted = self.run(['run', '--rm', '-T', '--no-deps', '--pull', 'never', 'new',
                              'python', '-m', 'app.backup', 'decrypt'], data=path.read_bytes())
        assert decrypted.startswith(b'PGDMP')
        self.sql(f'CREATE DATABASE {database} OWNER memory_app;')
        self.sql('CREATE EXTENSION vector;', database)
        self.run(['exec', '-T', 'db', 'pg_restore', '-U', 'memory_admin', '-d', database,
                  '--role=memory_app', '--no-owner', '--no-privileges', '--exit-on-error'], data=decrypted)

    def merge_quota(self, database, quota):
        self.backup_action('quota-merge', database, quota)
        # Merging twice must be idempotent, as recovery can be interrupted.
        self.backup_action('quota-merge', database, quota)
        actual = self.backup_action('quota-export', database)
        assert actual == quota, 'restored daily high-water marks changed'


def write_report(report):
    HISTORY.mkdir(exist_ok=True)
    # Preserve a pre-existing report even if it predates this script.
    if REPORT.exists():
        existing = REPORT.read_bytes()
        old = HISTORY / ('verification-v0.5-upgrade-existing-' + sha256(existing).hexdigest()[:16] + '.json')
        if not old.exists():
            old.write_bytes(existing)
    body = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode()
    history = HISTORY / ('verification-v0.5-upgrade-' + report['run_id'] + '.json')
    history.write_bytes(body)
    temporary = REPORT.with_name(REPORT.name + '.' + report['run_id'] + '.tmp')
    temporary.write_bytes(body)
    temporary.replace(REPORT)


def main():
    parser = argparse.ArgumentParser(description='隔离 schema 5→6 加密备份、删除重放和反思配额恢复演练（不调用模型）')
    parser.add_argument('--old-image', default='home-memory:0.4', help='已保留的 v0.4 镜像；不会拉取或构建')
    parser.add_argument('--new-image', default='home-memory:0.5', help='已构建的待验收 v0.5 镜像')
    parser.add_argument('--db-image', default=DB_IMAGE, help='本机已存在的 PostgreSQL 16 + pgvector 镜像')
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8]
    directory = ROOT / 'data/qa' / ('reflection-upgrade-' + run_id)
    directory.mkdir(parents=True, mode=0o700)
    report = {'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
              'project': PROJECT, 'directory': str(directory.relative_to(ROOT)),
              'isolation': 'fresh internal Compose network, database cluster, secrets and independent deletion journal; synthetic records only',
              'cloud_requests': 0, 'api_worker_reflector_started': False,
              'simulated_call_audit_note': 'Repository call reservations only; no model client or network request',
              'checks': {}, 'backups': [], 'passed': False, 'failures': []}
    drill = Drill(directory, report)
    lock = None
    started = time.monotonic()
    try:
        lock = (ROOT / 'data/qa/reflection-upgrade.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        drill.stage('确认独立 Compose 项目空闲并固定现有镜像 ID')
        containers = drill.command(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + PROJECT])
        if containers.strip():
            raise RuntimeError('隔离项目已有容器，请先检查上一次演练；本次不会删除既有容器。')
        images = {}
        for kind, name in (('old', args.old_image), ('new', args.new_image), ('database', args.db_image)):
            inspected = json.loads(drill.command(['docker', 'image', 'inspect', name]))[0]
            images[kind] = {'requested': name, 'id': inspected['Id']}
        if images['old']['id'] == images['new']['id']:
            raise RuntimeError('升级前后镜像不能相同。')
        report['images'] = images
        drill.write_configuration(images)
        drill.owns_project = True
        drill.stage('启动隔离数据库；不启动 API、worker 或 reflector')
        drill.run(['up', '-d', '--wait', '--wait-timeout', '120', '--pull', 'never', 'db'])
        network = json.loads(drill.command(['docker', 'network', 'inspect', PROJECT + '_default']))[0]
        assert network['Internal'] is True
        database_id = drill.run(['ps', '-q', 'db']).decode().strip()
        database_container = json.loads(drill.command(['docker', 'inspect', database_id]))[0]
        assert not database_container['HostConfig'].get('PortBindings')
        assert set(database_container['NetworkSettings']['Networks']) == {PROJECT + '_default'}
        report['checks']['internal_network_no_published_ports'] = True
        password = drill.secrets['db_app_password']
        drill.sql("CREATE ROLE memory_app LOGIN PASSWORD '" + password + "';")
        drill.sql('CREATE EXTENSION vector; GRANT CONNECT,CREATE ON DATABASE memory TO memory_app; '
                  'CREATE SCHEMA memory AUTHORIZATION memory_app; '
                  'ALTER ROLE memory_app IN DATABASE memory SET search_path TO memory,public;', 'memory')

        drill.stage('v0.4 创建非空 schema 5 来源链，并生成升级前加密备份')
        fixture = drill.python('seed_v5', image='old')
        baseline = fixture['snapshot']
        report['before'] = fixture
        backup_v5 = drill.encrypted_backup('memory', 'schema5-before-upgrade.hmem', image='old')
        report['checks']['actual_v04_nonempty_schema5_encrypted_backup'] = True

        drill.stage('v0.5 解密恢复 schema 5，并运行迁移和删除账本重放')
        upgraded = 'upgrade_current'
        drill.restore(backup_v5, upgraded)
        restored_v5 = drill.python('snapshot', upgraded, snapshot=baseline)
        assert restored_v5['schema'] == 5 and restored_v5['snapshot'] == baseline
        initial_quota = drill.backup_action('quota-export', upgraded)
        assert initial_quota == {'version': 1, 'source_schema': 5, 'days': []}
        report['first_replay'] = drill.backup_action('replay', upgraded)
        drill.backup_action('quota-merge', upgraded, initial_quota)
        migrated = drill.python('snapshot', upgraded, snapshot=baseline)
        assert migrated['schema'] == 6 and migrated['snapshot'] == baseline
        assert not migrated['enabled'] and migrated['quota']['days'] == []
        assert migrated['conversation_generation_preserved']
        report['after'] = migrated
        report['checks']['schema5_to6_original_counts_and_digests_unchanged'] = True
        report['checks']['automatic_reflection_off_after_upgrade'] = True

        drill.stage('创建排队和运行中的 v6 加密快照，模拟失败调用消耗配额')
        pending = drill.python('enqueue', upgraded)
        assert pending['quota']['days'] == []
        pending_backup = drill.encrypted_backup(upgraded, 'schema6-pending.hmem')
        running = drill.python('failed_then_running', upgraded, pending_id=pending['id'])
        running_backup = drill.encrypted_backup(upgraded, 'schema6-running.hmem')
        later = drill.python('later_failure', upgraded, **running)
        live_quota = drill.backup_action('quota-export', upgraded)
        assert live_quota == later['quota']
        report['quota_after_first_failed_request'] = running['after_failure']
        report['quota_at_running_snapshot'] = running['quota']
        report['live_quota_after_later_failure'] = live_quota
        report['checks']['failed_call_attempts_spend_real_day_quota'] = True

        for kind, backup, run_id_to_reset in (('pending', pending_backup, pending['id']),
                                             ('running', running_backup, running['running_id'])):
            drill.stage('恢复 v6 ' + kind + ' 快照，清理租约并合并最新真实日配额')
            database = 'upgrade_' + kind + '_restore'
            drill.restore(backup, database)
            drill.backup_action('replay', database)
            drill.merge_quota(database, live_quota)
            extra = running if kind == 'running' else {}
            reset = drill.python('assert_reset', database, run_id=run_id_to_reset,
                                 snapshot=baseline, **{**extra, 'quota': live_quota})
            assert reset['snapshot'] == baseline
            report[kind + '_restored'] = reset
            report['checks'][kind + '_snapshot_cancelled_and_quota_preserved'] = True

        drill.stage('删除根来源，再恢复删除前的 schema 5 加密备份并重放独立账本')
        deleted = drill.python('delete', upgraded, ids=fixture['ids'])
        assert deleted['journal_records'] >= 1
        journal = directory / 'journal/deletions.jsonl'
        journal_before = journal.read_bytes()
        restored = 'upgrade_deleted_restore'
        drill.restore(backup_v5, restored)
        before_replay = drill.python('snapshot', restored, snapshot=baseline)
        assert before_replay['schema'] == 5 and before_replay['snapshot'] == baseline
        replay = drill.backup_action('replay', restored)
        assert replay['replayed'] >= 1
        drill.merge_quota(restored, live_quota)
        report['deletion_restore'] = drill.python('assert_deleted', restored, ids=fixture['ids'], quota=live_quota)
        assert journal.read_bytes() == journal_before, 'restore rewrote the independent deletion journal'
        # A second replay must remain safe without refunding usage or restoring text.
        drill.backup_action('replay', restored)
        drill.python('assert_deleted', restored, ids=fixture['ids'], quota=live_quota)
        report['checks']['old_schema5_backup_cannot_resurrect_deleted_source_chain'] = True
        report['checks']['schema5_restore_preserves_live_schema6_quota'] = True
        report['checks']['independent_journal_unchanged_and_replay_idempotent'] = True
        report['checks']['no_cloud_client_or_background_services'] = True
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['failures'].append({'stage': report.get('stage', 'initialization'),
                                   'type': type(exc).__name__, 'message': drill.redact(str(exc))})
    finally:
        if drill.owns_project:
            try:
                drill.stage('关闭隔离 Compose 项目，保留数据库、删除账本、密钥与加密备份')
                drill.run(['down', '--timeout', '30'])
                remaining = drill.command(['docker', 'ps', '-aq', '--filter',
                                           'label=com.docker.compose.project=' + PROJECT])
                assert not remaining.strip(), 'isolated containers remain'
                report['checks']['isolated_compose_down_data_preserved'] = True
            except (Exception, KeyboardInterrupt) as exc:
                report['passed'] = False
                report['failures'].append({'stage': 'cleanup', 'type': type(exc).__name__,
                                           'message': drill.redact(str(exc))})
        else:
            report['cleanup'] = 'no containers owned by this run'
        if lock is not None:
            lock.close()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['seconds'] = round(time.monotonic() - started, 2)
        write_report(report)
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'],
                      'failures': report['failures'], 'report': str(REPORT),
                      'preserved_data': str(directory)}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
