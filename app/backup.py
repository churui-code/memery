"""Authenticated local backup envelope; pg_dump runs separately in the DB container."""
import io
import json
import os
import sys
import struct
import zipfile
from datetime import datetime,timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from app.config import Settings,secret
from app.journal import DeletionJournal
from app.repository import Repository

MAGIC=b'HOME-MEMORY-1\0'
LIMIT=256*1024*1024
STREAM_MAGIC=b'HOME-MEMORY-2\0'
CHUNK_SIZE=1024*1024
MANIFEST_LIMIT=16*1024*1024


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


def _read_exact(reader,size):
    parts=[]
    remaining=size
    while remaining:
        part=reader.read(remaining)
        if not part:raise ValueError('truncated_backup')
        parts.append(part)
        remaining-=len(part)
    return b''.join(parts)


def _stream_cipher(key,salt):
    derived=HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=STREAM_MAGIC).derive(key)
    return AESGCM(derived)


def _frame(cipher,salt,index,payload):
    counter=index.to_bytes(12,'big')
    encrypted=cipher.encrypt(counter,payload,STREAM_MAGIC+salt+counter)
    return struct.pack('!I',len(encrypted))+encrypted


def encrypt_stream(reader,writer,key,journal_records):
    """Bounded memory, a unique per-backup key, ordered authenticated chunks."""
    prefix=_read_exact(reader,5)
    if prefix!=b'PGDMP':raise ValueError('invalid_dump')
    manifest=json.dumps({'version':2,'created_at':datetime.now(timezone.utc).isoformat(),
        'journal_ids':[r['id'] for r in journal_records]}).encode()
    if len(manifest)>MANIFEST_LIMIT:raise ValueError('oversized_manifest')
    salt=os.urandom(16)
    cipher=_stream_cipher(key,salt)
    writer.write(STREAM_MAGIC+salt+_frame(cipher,salt,0,manifest))
    index=1
    part=prefix+reader.read(CHUNK_SIZE-5)
    while part:
        writer.write(_frame(cipher,salt,index,part))
        index+=1
        part=reader.read(CHUNK_SIZE)
    # An authenticated empty final frame makes truncation detectable.
    writer.write(_frame(cipher,salt,index,b''))


def decrypt_stream(reader,writer,key,journal_records):
    magic=_read_exact(reader,len(MAGIC))
    if magic==MAGIC:
        envelope=magic+reader.read(LIMIT+1024*1024+1)
        writer.write(decrypt(envelope,key,journal_records))
        return
    if magic!=STREAM_MAGIC:raise ValueError('invalid_backup')
    salt=_read_exact(reader,16)
    cipher=_stream_cipher(key,salt)
    def read_frame(index,maximum):
        length=struct.unpack('!I',_read_exact(reader,4))[0]
        if not 16<=length<=maximum+16:raise ValueError('invalid_frame_size')
        counter=index.to_bytes(12,'big')
        return cipher.decrypt(counter,_read_exact(reader,length),STREAM_MAGIC+salt+counter)
    manifest=json.loads(read_frame(0,MANIFEST_LIMIT))
    if manifest['version']!=2 or not set(manifest['journal_ids']).issubset({r['id'] for r in journal_records}):
        raise ValueError('deletion_journal_rolled_back')
    index=1
    while True:
        part=read_frame(index,CHUNK_SIZE)
        if index==1 and not part.startswith(b'PGDMP'):raise ValueError('invalid_dump')
        if not part:
            if reader.read(1):raise ValueError('trailing_backup_data')
            return
        writer.write(part)
        index+=1


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
    if action=='encrypt':encrypt_stream(sys.stdin.buffer,sys.stdout.buffer,key,records)
    elif action=='decrypt':decrypt_stream(sys.stdin.buffer,sys.stdout.buffer,key,records)
    else:raise ValueError('unknown_backup_action')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('backup_failed: '+type(exc).__name__,file=sys.stderr)
        sys.exit(1)
