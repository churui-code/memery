from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import sql

from app.worker import process_one, process_experience, process_episode
from conftest import FakeEmbeddings


@pytest.fixture(params=['memory', 'experience', 'episode'])
def index_job(repo, request):
    if request.param == 'memory':
        repo.remember({'text': '合成索引测试：卧室空调26度。'})
        return SimpleNamespace(table='outbox', chunks='projections', claim=repo.claim_job,
            finish=repo.finish_index, fail=repo.fail_job, process=process_one)
    repo.save_answer(uuid4(), '合成索引测试：卧室空调26度。', '合成回复', [], repo.epoch())
    if request.param == 'experience':
        return SimpleNamespace(table='experience_jobs', chunks='experience_chunks',
            claim=repo.claim_experience_job, finish=repo.finish_experience_index,
            fail=repo.fail_experience_job, process=process_experience)
    return SimpleNamespace(table='episode_jobs', chunks='episode_chunks',
        claim=repo.claim_episode_job, finish=repo.finish_episode_index,
        fail=repo.fail_episode_job, process=process_episode)


def job_state(repo, index_job):
    with repo.pool.connection() as c:
        rows=c.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(index_job.table))).fetchall()
        assert len(rows)==1
        return rows[0]


def stored_chunks(repo, index_job):
    with repo.pool.connection() as c:
        return c.execute(sql.SQL('SELECT chunk_index,content,embedding::text,model FROM {} ORDER BY chunk_index')
            .format(sql.Identifier(index_job.chunks))).fetchall()


def test_reclaimed_index_job_rejects_late_success_and_failure(repo, index_job):
    old=index_job.claim()
    assert old is not None and index_job.claim() is None
    with repo.pool.connection() as c:
        c.execute(sql.SQL("UPDATE {} SET lease_until=now()-interval '1 second'")
            .format(sql.Identifier(index_job.table)))
    current=index_job.claim()
    assert current['attempts']==2
    assert current['lease_token']!=old['lease_token']
    claimed=job_state(repo,index_job)
    vectors=FakeEmbeddings().embed(['合成旧结果'])

    index_job.finish(old,['合成旧结果'],vectors)
    index_job.fail(old)
    assert stored_chunks(repo,index_job)==[]
    assert job_state(repo,index_job)==claimed

    index_job.finish(current,['合成新结果'],vectors)
    completed=job_state(repo,index_job)
    assert completed['status']=='done' and completed['lease_token'] is None
    index_job.finish(old,['合成旧结果'],vectors)
    index_job.fail(old)
    index_job.fail(current)
    assert job_state(repo,index_job)==completed
    assert [row['content'] for row in stored_chunks(repo,index_job)]==['合成新结果']


@pytest.mark.parametrize('invalid', ['incomplete', 'dimension', 'nan', 'infinity'])
def test_invalid_index_result_preserves_previous_chunks_and_lease(repo, index_job, invalid):
    embeddings=FakeEmbeddings()
    index_job.finish(index_job.claim(),['合成原有索引'],embeddings.embed(['合成原有索引']))
    previous=stored_chunks(repo,index_job)
    assert len(previous)==1
    with repo.pool.connection() as c:
        c.execute(sql.SQL("UPDATE {} SET status='pending',available_at=now()")
            .format(sql.Identifier(index_job.table)))
    job=index_job.claim()
    claimed=job_state(repo,index_job)
    texts=['合成新分块一','合成新分块二']
    vectors=embeddings.embed(texts)
    if invalid=='incomplete':
        vectors.pop()
    elif invalid=='dimension':
        vectors[1].pop()
    else:
        vectors[1][0]=float('nan' if invalid=='nan' else 'inf')

    # The second vector fails after deleting the old index and inserting one chunk.
    with pytest.raises(ValueError,match='incomplete_embeddings' if invalid=='incomplete' else 'invalid_embedding'):
        index_job.finish(job,texts,vectors)
    assert stored_chunks(repo,index_job)==previous
    assert job_state(repo,index_job)==claimed

    index_job.finish(job,texts,embeddings.embed(texts))
    assert [row['content'] for row in stored_chunks(repo,index_job)]==texts
    assert job_state(repo,index_job)['status']=='done'


def test_index_worker_delays_retries_and_stops_after_three_failures(repo, index_job):
    class BrokenEmbeddings:
        calls=0

        def embed(self, texts):
            self.calls+=1
            raise RuntimeError('synthetic embedding failure')

    embeddings=BrokenEmbeddings()
    for attempt in range(1,4):
        assert index_job.process(repo,embeddings)
        state=job_state(repo,index_job)
        assert state['attempts']==attempt
        assert state['status']==('failed' if attempt==3 else 'pending')
        assert state['lease_token'] is None
        assert state['last_error']=='embedding_unavailable'
        assert index_job.claim() is None
        assert stored_chunks(repo,index_job)==[]
        with repo.pool.connection() as c:
            c.execute(sql.SQL("UPDATE {} SET available_at=now()-interval '1 second'")
                .format(sql.Identifier(index_job.table)))
    assert index_job.claim() is None
    assert embeddings.calls==3


def test_manual_reindex_revokes_running_lease_and_recovers_failed_job(repo):
    saved=repo.remember({'text':'合成索引测试：卧室空调26度。'})[0]
    old=repo.claim_job()
    repo.reindex(saved['id'])
    repo.fail_job(old)
    assert not repo.finish_index(old,['合成旧结果'],FakeEmbeddings().embed(['合成旧结果']))
    current=repo.claim_job()
    assert current['attempts']==1 and current['lease_token']!=old['lease_token']
    with repo.pool.connection() as c:
        c.execute('UPDATE outbox SET attempts=3 WHERE id=%s',(current['id'],))
    repo.fail_job(current)
    assert repo.get(saved['id'])['index_status']=='failed'

    repo.reindex(saved['id'])
    assert process_one(repo,FakeEmbeddings())
    result=repo.get(saved['id'])
    assert result['index_status']=='done' and result['index_error'] is None
    with repo.pool.connection() as c:
        assert c.execute('SELECT attempts FROM outbox WHERE id=%s',(current['id'],)).fetchone()['attempts']==1
