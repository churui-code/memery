-- Raw source material remains independent of every rebuildable memory projection.
ALTER TABLE experiences ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE experiences ADD COLUMN source_hash text NOT NULL DEFAULT '';
UPDATE experiences SET source_hash=encode(sha256(convert_to(user_text || chr(31) || assistant_text,'UTF8')),'hex')
 WHERE deleted_at IS NULL;

CREATE TABLE episodes (
 id uuid PRIMARY KEY, title text NOT NULL, gist text NOT NULL,
 context_type text NOT NULL CHECK(context_type IN ('conversation','reported','hypothetical','fiction')),
 importance double precision NOT NULL DEFAULT 0.2 CHECK(importance BETWEEN 0 AND 1),
 importance_reason text NOT NULL DEFAULT '',
 emotional_salience double precision NOT NULL DEFAULT 0 CHECK(emotional_salience BETWEEN 0 AND 1),
 emotional_reason text NOT NULL DEFAULT '', participants jsonb NOT NULL DEFAULT '[]',
 terms tsvector NOT NULL, occurred_at timestamptz, occurred_until timestamptz, time_quote text,
 time_precision text NOT NULL DEFAULT 'unknown' CHECK(time_precision IN ('unknown','day','range')),
 source_reliability jsonb NOT NULL DEFAULT '{}', encoding_version text NOT NULL DEFAULT 'episode-encoding-v1',
 recorded_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
 last_rehearsed_at timestamptz, revision integer NOT NULL DEFAULT 1,
 source_hash text NOT NULL, local_fallback boolean NOT NULL DEFAULT false,
 allow_cloud boolean NOT NULL DEFAULT true, context_stale boolean NOT NULL DEFAULT false,
 deleted_at timestamptz
);
CREATE INDEX episodes_terms_idx ON episodes USING gin(terms);
CREATE INDEX episodes_recent_idx ON episodes(recorded_at DESC,id) WHERE deleted_at IS NULL;
CREATE INDEX episodes_occurred_idx ON episodes(occurred_at,id) WHERE deleted_at IS NULL;
CREATE TABLE episode_evidence (
 id uuid PRIMARY KEY, episode_id uuid NOT NULL REFERENCES episodes(id),
 experience_id uuid NOT NULL REFERENCES experiences(id), role text NOT NULL CHECK(role IN ('user','assistant')),
 quote text NOT NULL, start_offset integer NOT NULL CHECK(start_offset>=0),
 end_offset integer NOT NULL CHECK(end_offset>=start_offset),
 source_revision integer NOT NULL, source_hash text NOT NULL,
 UNIQUE(episode_id,experience_id,role,start_offset,end_offset)
);
CREATE INDEX episode_evidence_source_idx ON episode_evidence(experience_id,episode_id);
CREATE TABLE episode_details (
 id uuid PRIMARY KEY, episode_id uuid NOT NULL REFERENCES episodes(id),
 evidence_id uuid NOT NULL REFERENCES episode_evidence(id), kind text NOT NULL,
 meaning text NOT NULL DEFAULT '', importance double precision NOT NULL CHECK(importance BETWEEN 0 AND 1),
 emotional_salience double precision NOT NULL CHECK(emotional_salience BETWEEN 0 AND 1),
 importance_reason text NOT NULL DEFAULT '', emotional_reason text NOT NULL DEFAULT '',
 source_reliability jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX episode_details_episode_idx ON episode_details(episode_id);
CREATE TABLE episode_cues (
 id uuid PRIMARY KEY, episode_id uuid NOT NULL REFERENCES episodes(id),
 evidence_id uuid NOT NULL REFERENCES episode_evidence(id), kind text NOT NULL, text text NOT NULL
);
CREATE INDEX episode_cues_episode_idx ON episode_cues(episode_id);
CREATE INDEX episode_cues_text_idx ON episode_cues(text);
CREATE TABLE episode_versions (
 episode_id uuid NOT NULL REFERENCES episodes(id), revision integer NOT NULL,
 title text NOT NULL, gist text NOT NULL, importance double precision NOT NULL,
 importance_reason text NOT NULL, emotional_salience double precision NOT NULL,
 emotional_reason text NOT NULL, source_hash text NOT NULL, created_at timestamptz NOT NULL,
 source_reliability jsonb NOT NULL DEFAULT '{}',encoding_version text NOT NULL DEFAULT 'episode-encoding-v1',
 time_precision text NOT NULL DEFAULT 'unknown',occurred_at timestamptz,occurred_until timestamptz,time_quote text,
 PRIMARY KEY(episode_id,revision)
);
-- Association edges aid recall. They never carry deletion or permission taint.
CREATE TABLE episode_associations (
 source_id uuid NOT NULL REFERENCES episodes(id), target_id uuid NOT NULL REFERENCES episodes(id),
 kind text NOT NULL, PRIMARY KEY(source_id,target_id,kind), CHECK(source_id<>target_id)
);
CREATE INDEX episode_associations_target_idx ON episode_associations(target_id);
CREATE TABLE episode_rehearsals (
 episode_id uuid NOT NULL REFERENCES episodes(id), experience_id uuid NOT NULL REFERENCES experiences(id),
 kind text NOT NULL, recalled_at timestamptz NOT NULL,
 PRIMARY KEY(episode_id,experience_id)
);
CREATE TABLE episode_chunks (
 episode_id uuid NOT NULL REFERENCES episodes(id), revision integer NOT NULL, source_hash text NOT NULL,
 chunk_index integer NOT NULL, content text NOT NULL, embedding vector(512) NOT NULL, model text NOT NULL,
 PRIMARY KEY(episode_id,revision,chunk_index,model)
);
CREATE INDEX episode_chunks_hnsw_idx ON episode_chunks USING hnsw(embedding vector_cosine_ops);
CREATE TABLE episode_jobs (
 episode_id uuid PRIMARY KEY REFERENCES episodes(id), revision integer NOT NULL, source_hash text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed','cancelled')),
 attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, lease_token uuid, last_error text
);
CREATE INDEX episode_jobs_ready_idx ON episode_jobs(available_at) WHERE status IN ('pending','running');
ALTER TABLE context_links DROP CONSTRAINT context_links_source_type_check;
ALTER TABLE context_links DROP CONSTRAINT context_links_target_type_check;
ALTER TABLE context_links ADD CHECK(source_type IN ('memory','experience','self','episode'));
ALTER TABLE context_links ADD CHECK(target_type IN ('experience','self','episode'));
ALTER TABLE model_calls ADD COLUMN phase text NOT NULL DEFAULT 'answer';
ALTER TABLE model_calls ADD COLUMN source_refs jsonb NOT NULL DEFAULT '[]';
ALTER TABLE model_calls ADD COLUMN estimated_input_tokens integer;
ALTER TABLE model_calls ADD COLUMN policy_version text;
INSERT INTO schema_migrations(version) VALUES(4);
