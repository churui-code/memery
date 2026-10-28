-- Evidence tombstones contain IDs/offsets only, never a second copy of private text.
ALTER TABLE experiences ADD COLUMN correction_history_blocked boolean NOT NULL DEFAULT false;
CREATE TABLE episode_corrections (
 id uuid PRIMARY KEY,
 episode_id uuid NOT NULL REFERENCES episodes(id),
 episode_revision integer NOT NULL,
 experience_id uuid NOT NULL REFERENCES experiences(id),
 start_offset integer NOT NULL CHECK(start_offset>=0),
 end_offset integer NOT NULL CHECK(end_offset>start_offset),
 replacement_experience_id uuid NOT NULL REFERENCES experiences(id),
 created_at timestamptz NOT NULL,
 UNIQUE(episode_id,experience_id,start_offset,end_offset)
);
CREATE INDEX episode_corrections_source_idx ON episode_corrections(experience_id);
INSERT INTO schema_migrations(version) VALUES(7);
