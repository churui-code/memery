"""Commit rechecks each frozen original before keeping inserted self rows."""
import pytest
from psycopg import sql

from app.errors import MemoryError
from test_reflection_storage import calls, count, save, source, start, update


@pytest.mark.parametrize('field,value', [
    ('quote', '只有全部资料齐备才能继续。'),
    ('role', 'assistant'),
    ('start_offset', 1),
    ('end_offset', 2),
])
def test_changed_evidence_without_epoch_change_rolls_back_submission(repo, field, value):
    event = source(repo, '合成对话：等我下次带来信息再讨论。')
    job = start(repo)
    calls(repo, job)
    proposal = update(event, text='下次收到信息后再讨论。', reason='保留用户要求的讨论节奏。')
    before_epoch, before_links = repo.epoch(), count(repo, 'context_links')
    with repo.pool.connection() as conn:
        conn.execute(sql.SQL('UPDATE episode_evidence SET {}=%s WHERE id=%s').format(
            sql.Identifier(field)), (value, proposal['evidence_ids'][0]))
    with pytest.raises(MemoryError, match='reflection_source_changed'):
        save(repo, job, [proposal])
    assert repo.epoch() == before_epoch
    assert count(repo, 'context_links') == before_links
    assert count(repo, 'self_observations') == count(repo, 'reflection_processed_episodes') == 0
    assert repo.reflection_run(job['id'])['status'] == 'running'


def test_corrupt_related_uncited_quote_also_prevents_commit(repo):
    current = source(repo, '合成对话：下次带来信息再讨论。')
    related = source(repo, '合成对话：此前先整理过记录。')
    job = start(repo, events=[current, related], pending_ids=[str(current['id'])])
    calls(repo, job)
    proposal = update(current, text='等待用户下次带来信息再讨论。', reason='按本次原文保留讨论节奏。')
    with repo.pool.connection() as conn:
        conn.execute('UPDATE episode_evidence SET quote=%s WHERE id=%s',
            ('只有全部资料齐备才能继续。', related['evidence'][0]['id']))
    with pytest.raises(MemoryError, match='reflection_source_changed'):
        save(repo, job, [proposal])
    assert count(repo, 'self_observations') == count(repo, 'reflection_processed_episodes') == 0
