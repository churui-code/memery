"""Downgrade only the additive v6 structures in isolated migration fixtures."""
def drop_reflection_schema(c):
    c.execute('DROP TABLE episode_corrections')
    c.execute('ALTER TABLE experiences DROP COLUMN correction_history_blocked')
    c.execute('ALTER TABLE self_observations DROP COLUMN generation_type,DROP COLUMN reflection_run_id,DROP COLUMN evidence_manifest')
    c.execute('ALTER TABLE model_calls DROP COLUMN reflection_run_id')
    c.execute('ALTER TABLE model_calls ALTER COLUMN conversation_id SET NOT NULL')
    c.execute('DROP TABLE reflection_processed_episodes')
    c.execute('DROP TABLE reflection_runs')
    c.execute('DROP TABLE reflection_daily_usage')
    c.execute('DROP TABLE reflection_settings')
    c.execute('ALTER TABLE episodes DROP COLUMN reflection_correction')
    c.execute('DELETE FROM schema_migrations WHERE version>=6')
