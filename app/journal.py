"""Fsynced deletion intents on independent storage; identifiers, never text."""
import fcntl
import json
import os
from pathlib import Path
from datetime import datetime
from app.errors import MemoryError


class DeletionJournal:
    def __init__(self, filename):
        self.path = Path(filename)

    def append(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open('ab') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write((json.dumps(record, default=str, sort_keys=True)+'\n').encode())
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise MemoryError('journal_unavailable', '删除账本写入失败，操作未确认完成。', 503) from exc

    def records(self, required=False):
        if not self.path.exists():
            if required:
                raise MemoryError('journal_missing', '缺少独立删除账本，拒绝开放恢复后的服务。', 503)
            return []
        try:
            with self.path.open('rb') as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                raw = handle.read()
            if raw and not raw.endswith(b'\n'):
                raise ValueError('incomplete_record')
            records = [json.loads(line) for line in raw.splitlines() if line]
            for r in records:
                if r['target_type'] not in {'memory','conversation','expiry','experience','self'}:
                    raise ValueError('invalid_target')
                datetime.fromisoformat(r['deleted_at'])
            return records
        except (ValueError, KeyError, OSError) as exc:
            raise MemoryError('journal_invalid', '删除账本不完整或无法读取，需修复后启动。', 503) from exc
