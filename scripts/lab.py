#!/usr/bin/env python3
"""Independent synthetic laboratory lifecycle; standard library only."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data/lab'
COMPOSE = ['docker', 'compose', '-f', 'compose.lab.yaml', '-p', 'home-memory-lab']


def run(arguments, **kwargs):
    return subprocess.run(COMPOSE + arguments, cwd=ROOT, check=True, **kwargs)


def configure():
    for directory in ('postgres', 'runtime', 'secrets'):
        (DATA / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ('db_password', 'api_token'):
        path = DATA / 'secrets' / name
        if not path.exists():
            if (DATA / 'postgres/PG_VERSION').exists():
                raise RuntimeError('实验密钥缺失，不能为已有实验数据库静默重建密钥。')
            path.write_text(secrets.token_urlsafe(32) + '\n')
        os.chmod(path, 0o600)
    journal = DATA / 'runtime/service-journal.jsonl'
    if not journal.exists():
        if (DATA / 'postgres/PG_VERSION').exists():
            raise RuntimeError('实验删除日志缺失，请恢复独立实验日志。')
        journal.touch(mode=0o600)
    if not (ROOT / '.secrets/minimax_api_key').is_file():
        raise RuntimeError('项目模型密钥文件尚未配置。请先完成项目配置；实验不会复制生产数据。')


def main():
    parser = argparse.ArgumentParser(description='隔离的合成记忆实验台')
    parser.add_argument('action', choices=['up', 'down', 'status', 'token-path', 'open'])
    args = parser.parse_args()
    if args.action == 'up':
        configure()
        run(['up', '-d', '--wait', 'db'])
        run(['exec', '-T', 'db', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'memory_lab', '-d', 'memory_lab',
             '-c', 'CREATE EXTENSION IF NOT EXISTS vector'])
        run(['build', 'api'])
        run(['up', '-d', '--force-recreate', '--wait', 'api'])
        print('实验台：http://127.0.0.1:8766/lab')
        print('独立令牌文件：' + str(DATA / 'secrets/api_token'))
    elif args.action == 'down':
        run(['down'])
    elif args.action == 'status':
        run(['ps'])
    elif args.action == 'open':
        webbrowser.open('http://127.0.0.1:8766/lab')
    else:
        print(DATA / 'secrets/api_token')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
