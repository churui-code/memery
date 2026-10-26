"""Business time is injectable; workers keep real-time database leases."""
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


def aware_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('clock_requires_timezone')
    return value.astimezone(timezone.utc)


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)


class MutableClock:
    def __init__(self, initial):
        self._value = aware_utc(initial)
        self._lock = RLock()

    def now(self):
        with self._lock:
            return self._value

    def set(self, value):
        value = aware_utc(value)
        with self._lock:
            if value < self._value:
                raise ValueError('clock_cannot_go_backwards')
            self._value = value
            return self._value

    def advance(self, *, days=0, hours=0, seconds=0):
        with self._lock:
            return self.set(self._value + timedelta(days=days, hours=hours, seconds=seconds))


FrozenClock = MutableClock
