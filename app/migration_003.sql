-- The experience archive is independent of the disposable conversation window.
CREATE TABLE experiences (
 id uuid PRIMARY KEY, conversation_id uuid NOT NULL,
 user_text text NOT NULL, assistant_text text NOT NULL,
 title text NOT NULL DEFAULT '', summary text NOT NULL DEFAULT '',
 context_type text NOT NULL DEFAULT 'conversation',
 participants jsonb NOT NULL DEFAULT '[]', anchors jsonb NOT NULL DEFAULT '[]',
 significance double precision NOT NULL DEFAULT 0.2 CHECK(significance BETWEEN 0 AND 1),
 terms tsvector NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
 occurred_at timestamptz, time_quote text,
 allow_cloud boolean NOT NULL DEFAULT true, context_stale boolean NOT NULL DEFAULT false,
 deleted_at timestamptz, legacy_message_id bigint UNIQUE
);
CREATE INDEX experiences_terms_idx ON experiences USING gin(terms);
CREATE INDEX experiences_time_idx ON experiences(recorded_at DESC) WHERE deleted_at IS NULL;
CREATE TABLE experience_chunks (
 experience_id uuid NOT NULL REFERENCES experiences(id), chunk_index integer NOT NULL,
 content text NOT NULL, embedding vector(512) NOT NULL, model text NOT NULL,
 PRIMARY KEY(experience_id,chunk_index,model)
);
CREATE TABLE experience_jobs (
 experience_id uuid PRIMARY KEY REFERENCES experiences(id),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed','cancelled')),
 attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, lease_token uuid, last_error text
);
CREATE INDEX experience_jobs_ready_idx ON experience_jobs(available_at) WHERE status IN ('pending','running');
CREATE TABLE self_observations (
 id uuid PRIMARY KEY, domain text NOT NULL CHECK(domain IN ('emotion','relationship','identity','belief','goal')),
 state_key text NOT NULL, revision integer NOT NULL,
 text text NOT NULL, reason text NOT NULL, evidence_quote text NOT NULL,
 confidence double precision NOT NULL CHECK(confidence BETWEEN 0 AND 1),
 intensity double precision CHECK(intensity BETWEEN 0 AND 1),
 half_life_hours double precision CHECK(half_life_hours BETWEEN 0.25 AND 720),
 created_at timestamptz NOT NULL DEFAULT now(), invalidated_at timestamptz, deleted_at timestamptz,
 UNIQUE(domain,state_key,revision)
);
CREATE INDEX self_observations_current_idx ON self_observations(domain,state_key,revision DESC);
-- Edges retain only IDs. Recursive traversal revokes derived memories on deletion.
CREATE TABLE context_links (
 source_type text NOT NULL CHECK(source_type IN ('memory','experience','self')),
 source_id uuid NOT NULL,
 target_type text NOT NULL CHECK(target_type IN ('experience','self')),
 target_id uuid NOT NULL,
 PRIMARY KEY(source_type,source_id,target_type,target_id)
);
CREATE INDEX context_links_target_idx ON context_links(target_type,target_id);
ALTER TABLE messages ADD COLUMN experience_id uuid REFERENCES experiences(id);
ALTER TABLE deletion_ledger DROP CONSTRAINT deletion_ledger_target_type_check;
ALTER TABLE deletion_ledger ADD CHECK(target_type IN ('memory','conversation','expiry','experience','self'));

-- Preserve every surviving old message, including assistant speech. Never invent
-- missing messages or claim that migrated assistant speech is an external fact.
INSERT INTO experiences(id,conversation_id,user_text,assistant_text,title,terms,recorded_at,legacy_message_id)
 SELECT md5('experience-' || id::text)::uuid,conversation_id,
 CASE WHEN role='user' THEN content ELSE '' END,
 CASE WHEN role='assistant' THEN content ELSE '' END,
 '历史对话（迁移）',to_tsvector('simple',content),created_at,id FROM messages;
UPDATE messages m SET experience_id=e.id FROM experiences e WHERE e.legacy_message_id=m.id;
INSERT INTO experience_jobs(experience_id) SELECT id FROM experiences;
INSERT INTO context_links(source_type,source_id,target_type,target_id)
 SELECT 'memory',s.id,'experience',e.id FROM messages m
 JOIN experiences e ON e.legacy_message_id=m.id CROSS JOIN LATERAL unnest(m.source_ids) s(id);
INSERT INTO schema_migrations(version) VALUES(3);
