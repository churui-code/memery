import logging
import signal
import time
from threading import Event
from app.config import Settings
from app.embeddings import LocalEmbeddings
from app.repository import Repository
from app.text import chunks

log=logging.getLogger('worker')


def process_one(repo,embeddings):
    job=repo.claim_job()
    if not job:
        return False
    source=repo.job_source(job)
    if not source:
        repo.finish_index(job,[],[])
        return True
    try:
        parts=chunks(source['text'])
        vectors=embeddings.embed(parts)
        repo.finish_index(job,parts,vectors)
    except Exception as exc:
        log.warning('index_failed job=%s reason=%s',job['id'],type(exc).__name__)
        repo.fail_job(job)
    return True


def main():
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    settings=Settings()
    repo=Repository(settings)
    stop=Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stop.set())
    try:
        repo.migrate()
        repo.replay_deletions(required=True)
        embeddings=LocalEmbeddings(settings)
        maintenance=0
        while not stop.is_set():
            try:
                worked=process_one(repo,embeddings)
                if time.monotonic()-maintenance>60:
                    repo.expire_history()
                    maintenance=time.monotonic()
                if not worked:
                    stop.wait(0.5)
            except Exception as exc:
                log.warning('worker_retry reason=%s',type(exc).__name__)
                stop.wait(3)
    finally:
        repo.close()


if __name__=='__main__':
    main()
