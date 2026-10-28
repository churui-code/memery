"""Read-only full-table evidence for the current synthetic acceptance experiment."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from psycopg import sql
from app.config import Settings
from app.repository import Repository
from scripts.verify_chat_repairs import atomic_json


def audit(batch, label):
    directory = Path('/qa')
    state = json.loads((directory / f'batch{batch}.json').read_text())
    registry = json.loads((directory / 'runtime/registry.json').read_text())
    schema = registry['experiments'][state['experiment']['body']['id']]['schema']
    repo = Repository(replace(Settings(), db_schema=schema, deletion_journal='/qa/audit-unused.jsonl'))
    result = {'schema': schema, 'label': label, 'tables': {}}
    try:
        with repo.pool.connection() as conn, conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            tables = conn.execute("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
            for table in tables:
                name = table['table_name']
                rows = conn.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(name))).fetchall()
                serialized = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
                result['tables'][name] = {'count': len(rows), 'sha256': hashlib.sha256(
                    json.dumps(serialized).encode()).hexdigest(), 'rows': rows}
    finally:
        repo.close()
    target = directory / f'batch{batch}-db' / f'{label}.json'
    target.parent.mkdir(exist_ok=True)
    atomic_json(target, result)
    print(json.dumps({'label': label, 'tables': len(result['tables'])}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch', type=int, required=True)
    parser.add_argument('--label', required=True)
    args = parser.parse_args()
    audit(args.batch, args.label)
