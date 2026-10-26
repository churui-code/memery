"""Authenticated local backup envelope; pg_dump runs separately in the DB container."""
import io
import json
import os
import sys
import zipfile
from datetime import datetime,timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.config import Settings,secret
from app.journal import DeletionJournal
from app.repository import Repository

MAGIC=b'HOME-MEMORY-1\0'
LIMIT=256*1024*1024


def encrypt(dump,key,journal_records):
    if not dump.startswith(b'PGDMP') or len(dump)>LIMIT:
        raise ValueError('invalid_or_oversized_dump')
    manifest={'version':1,'created_at':datetime.now(timezone.utc).isoformat(),
              'journal_ids':[r['id'] for r in journal_records]}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('manifest.json',json.dumps(manifest))
        archive.writestr('memory.dump',dump)
    nonce=os.urandom(12)
    return MAGIC+nonce+AESGCM(key).encrypt(nonce,stream.getvalue(),MAGIC)


def decrypt(envelope,key,journal_records):
    if not envelope.startswith(MAGIC) or len(envelope)>LIMIT+1024*1024:
        raise ValueError('invalid_backup')
    offset=len(MAGIC)
    payload=AESGCM(key).decrypt(envelope[offset:offset+12],envelope[offset+12:],MAGIC)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if set(archive.namelist())!={'manifest.json','memory.dump'} or sum(i.file_size for i in archive.infolist())>LIMIT+1024*1024:
            raise ValueError('invalid_archive')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest['version']!=1 or not set(manifest['journal_ids']).issubset({r['id'] for r in journal_records}):
            raise ValueError('deletion_journal_rolled_back')
        dump=archive.read('memory.dump')
        if not dump.startswith(b'PGDMP'):
            raise ValueError('invalid_dump')
        return dump


def main():
    settings=Settings()
    action=sys.argv[1]
    if action=='replay':
        repo=Repository(settings)
        try:
            repo.migrate()
            count=repo.replay_deletions(required=True)
            repo.expire_history()
            print(json.dumps({'replayed':count,'health':repo.health()},ensure_ascii=False))
        finally:
            repo.close()
        return
    key=bytes.fromhex(secret('BACKUP_KEY'))
    records=DeletionJournal(settings.deletion_journal).records(required=True)
    data=sys.stdin.buffer.read(LIMIT+1024*1024+1)
    result=encrypt(data,key,records) if action=='encrypt' else decrypt(data,key,records)
    sys.stdout.buffer.write(result)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('backup_failed: '+type(exc).__name__,file=sys.stderr)
        sys.exit(1)
