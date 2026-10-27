#!/usr/bin/env python3
"""Host-side lifecycle; no third-party Python dependencies required."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
DATA=Path(os.getenv('MEMORY_DATA_DIR',str(ROOT/'data'))).resolve()
COMPOSE=['docker','compose']


def run(args,**kwargs):
    return subprocess.run(COMPOSE+args,cwd=ROOT,check=True,**kwargs)


def sql_command(query,database='memory',capture=False):
    return run(['exec','-T','db','psql','-X','-v','ON_ERROR_STOP=1','-U','memory_admin','-d',database],
               input=query.encode(),stdout=subprocess.PIPE if capture else None)


def configure():
    directory=ROOT/'.secrets'
    directory.mkdir(exist_ok=True,mode=0o700)
    os.chmod(directory,0o700)
    defaults={'db_admin_password':secrets.token_urlsafe(32),'db_app_password':secrets.token_urlsafe(32),
              'api_token':secrets.token_urlsafe(32),'backup_key':secrets.token_hex(32)}
    for name,value in defaults.items():
        path=directory/name
        if not path.exists():
            path.write_text(value+'\n')
        os.chmod(path,0o600)
    source=Path('/Users/churui/Project/whiteboard-x6-demo/.env')
    values={}
    if source.exists():
        for line in source.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1)
                values[k.strip()]=v.strip().strip('"').strip("'")
    keypath=directory/'minimax_api_key'
    if not keypath.exists():
        keypath.write_text(values.get('MINIMAX_API_KEY','')+'\n')
        os.chmod(keypath,0o600)
    env=ROOT/'.env'
    if not env.exists():
        env.write_text('MINIMAX_BASE_URL='+values.get('MINIMAX_BASE_URL','https://api.minimaxi.com/anthropic')+'\nMINIMAX_THINKING=disabled\nMEMORY_PORT=8765\n')
        os.chmod(env,0o600)
    for name in ('postgres','models','journal','backups'):
        (DATA/name).mkdir(parents=True,exist_ok=True)
    journal=DATA/'journal/deletions.jsonl'
    if not journal.exists():
        # A lost journal on an existing installation must not be silently replaced.
        if (DATA/'postgres/PG_VERSION').exists():
            raise RuntimeError('删除账本缺失，不能自动重新创建；请恢复独立账本。')
        with journal.open('xb') as handle:
            handle.flush()
            os.fsync(handle.fileno())
    print('本地配置已准备；密钥未输出。',flush=True)


def bootstrap_database():
    password=(ROOT/'.secrets/db_app_password').read_text().strip().replace("'","''")
    sql_command("""DO $body$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='memory_app')
      THEN CREATE ROLE memory_app LOGIN; END IF; END $body$;
      ALTER ROLE memory_app PASSWORD '"""+password+"""';
      CREATE EXTENSION IF NOT EXISTS vector;
      GRANT CONNECT,CREATE ON DATABASE memory TO memory_app;
      CREATE SCHEMA IF NOT EXISTS memory AUTHORIZATION memory_app;
      ALTER ROLE memory_app IN DATABASE memory SET search_path TO memory,public;
    """)


def wait_api():
    port=os.getenv('MEMORY_PORT','8765')
    if (ROOT/'.env').exists():
        for line in (ROOT/'.env').read_text().splitlines():
            if line.startswith('MEMORY_PORT=') and not os.getenv('MEMORY_PORT'):
                port=line.split('=',1)[1]
    for _ in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:'+port+'/health',timeout=2) as response:
                return json.load(response)
        except Exception:
            time.sleep(1)
    raise RuntimeError('API 健康检查未通过，请查看 docker compose logs api。')


def up():
    configure()
    run(['up','-d','--wait','db'])
    bootstrap_database()
    run(['build','api'])
    run(['run','--rm','--no-deps','api','python','-m','app.embeddings','--download'])
    run(['up','-d','--force-recreate','--wait','api','worker','reflector'])
    print(json.dumps(wait_api(),ensure_ascii=False))


def backup():
    dest=DATA/'backups'/('memory-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6]+'.hmem')
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_suffix('.partial')
    dump=subprocess.Popen(COMPOSE+['exec','-T','db','pg_dump','-U','memory_admin','-d','memory','--format=custom','--schema=memory','--no-owner','--no-privileges'],cwd=ROOT,stdout=subprocess.PIPE)
    try:
        with temp.open('wb') as handle:
            os.chmod(temp,0o600)
            run(['run','--rm','-T','--no-deps','api','python','-m','app.backup','encrypt'],stdin=dump.stdout,stdout=handle)
            handle.flush()
            os.fsync(handle.fileno())
        if dump.wait()!=0:
            raise RuntimeError('pg_dump failed')
        temp.replace(dest)
    except Exception:
        dump.kill()
        dump.wait()
        temp.unlink(missing_ok=True)
        raise
    print(str(dest))
    return dest


def restore(filename):
    filename=Path(filename).resolve()
    staging='memory_restore_'+uuid4().hex[:12]
    previous='memory_previous_'+uuid4().hex[:12]
    # Authenticate/decrypt before disturbing the live service; the dump only
    # passes through process pipes into a new, isolated database.
    sql_command(f'CREATE DATABASE {staging} OWNER memory_app;',database='postgres')
    promoted=False
    stopped=False
    try:
        sql_command('CREATE EXTENSION vector;',database=staging)
        with filename.open('rb') as handle:
            decrypt=subprocess.Popen(COMPOSE+['run','--rm','-T','--no-deps','api','python','-m','app.backup','decrypt'],cwd=ROOT,stdin=handle,stdout=subprocess.PIPE)
            try:
                run(['exec','-T','db','pg_restore','-U','memory_admin','-d',staging,'--role=memory_app','--no-owner','--no-privileges','--exit-on-error'],stdin=decrypt.stdout)
                if decrypt.wait()!=0:
                    raise RuntimeError('backup authentication/decryption failed')
            finally:
                if decrypt.poll() is None:
                    decrypt.kill()
                    decrypt.wait()
        run(['run','--rm','--no-deps','-e','DB_NAME='+staging,'api','python','-m','app.backup','replay'])
        run(['stop','api','worker','reflector'])
        stopped=True
        backup()  # Quiesced, encrypted rollback copy before replacing the database.
        run(['run','--rm','--no-deps','-e','DB_NAME='+staging,'api','python','-m','app.backup','replay'])
        # The encrypted snapshot may predate paid requests, including failures.
        # Export only after writers stop, then merge before the atomic switch.
        # An unavailable live ledger or failed merge aborts restoration.
        quota=run(['run','--rm','-T','--no-deps','api','python','-m','app.backup','quota-export'],
                  stdout=subprocess.PIPE).stdout
        run(['run','--rm','-T','--no-deps','-e','DB_NAME='+staging,'api','python','-m','app.backup','quota-merge'],
            input=quota)
        sql_command("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='memory' AND pid<>pg_backend_pid();",database='postgres')
        sql_command(f'BEGIN; ALTER DATABASE memory RENAME TO {previous}; ALTER DATABASE {staging} RENAME TO memory; COMMIT;',database='postgres')
        promoted=True
        run(['up','-d','--wait','api','worker','reflector'])
        wait_api()
        sql_command(f'DROP DATABASE {previous} WITH (FORCE);',database='postgres')
        print('恢复完成，已重放独立删除账本。')
    except Exception:
        if promoted:
            run(['stop','api','worker','reflector'])
            sql_command("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='memory' AND pid<>pg_backend_pid();",database='postgres')
            sql_command(f'BEGIN; ALTER DATABASE memory RENAME TO {staging}; ALTER DATABASE {previous} RENAME TO memory; COMMIT;',database='postgres')
        if stopped:
            run(['up','-d','api','worker','reflector'])
        raise
    finally:
        # staging no longer exists after a successful promotion.
        sql_command(f'DROP DATABASE IF EXISTS {staging} WITH (FORCE);',database='postgres')


def main():
    parser=argparse.ArgumentParser(description='家庭 AI 记忆服务管理')
    parser.add_argument('action',choices=['configure','up','down','status','test','backup','restore'])
    parser.add_argument('file',nargs='?')
    args=parser.parse_args()
    if args.action=='configure': configure()
    elif args.action=='up': up()
    elif args.action=='down': run(['down'])
    elif args.action=='status': run(['ps'])
    elif args.action=='test': run(['--profile','test','run','--rm','tests'])
    elif args.action=='backup': backup()
    elif args.action=='restore':
        if not args.file: parser.error('restore 需要备份路径')
        restore(args.file)


if __name__=='__main__':
    try: main()
    except (Exception,KeyboardInterrupt) as exc:
        print('操作失败：'+type(exc).__name__+('：'+str(exc) if isinstance(exc,RuntimeError) else ''),file=sys.stderr)
        sys.exit(1)
