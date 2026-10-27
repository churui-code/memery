"""Independent reflection executor; automatic eligibility stays entirely local."""
import logging
import signal
from threading import Event
from time import monotonic

from app.config import Settings
from app.embeddings import LocalEmbeddings
from app.errors import MemoryError
from app.model import MiniMax
from app.reflection import ReflectionService
from app.repository import Repository


log = logging.getLogger('reflector')


def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if settings.lab_enabled:
        raise RuntimeError('reflection_scheduler_disabled_for_lab')
    repo = Repository(settings)
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        repo.migrate()
        repo.replay_deletions(required=True)
        service = ReflectionService(repo, LocalEmbeddings(settings), MiniMax(settings))
        automatic_check = float('-inf')
        while not stop.is_set():
            try:
                worked = service.execute()
                current = monotonic()
                if current - automatic_check >= 900:
                    automatic_check = current
                    status = service.settings()
                    if status.get('eligible'):
                        try:
                            service.enqueue(automatic=True)
                        except MemoryError as exc:
                            if exc.code != 'reflection_not_eligible':
                                raise
                if worked is None:
                    stop.wait(1)
            except Exception as exc:
                log.warning('reflection_executor_wait reason=%s', getattr(exc, 'code', type(exc).__name__))
                stop.wait(3)
    finally:
        repo.close()


if __name__ == '__main__':
    main()
