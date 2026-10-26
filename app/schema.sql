CREATE TABLE schema_migrations(version integer PRIMARY KEY, installed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE system_state(id integer PRIMARY KEY CHECK (id=1), epoch bigint NOT NULL DEFAULT 0);
INSERT INTO system_state(id) VALUES (1);
CREATE TABLE memories (
 id uuid PRIMARY KEY, text text NOT NULL, terms tsvector NOT NULL,
 speaker_id text NOT NULL DEFAULT 'user', subject text NOT NULL DEFAULT 'user',
 allow_cloud boolean NOT NULL DEFAULT true, revision integer NOT NULL DEFAULT 1,
 valid_from timestamptz NOT NULL, valid_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 deleted_at timestamptz, idempotency_key text UNIQUE, request_hash text NOT NULL,
 CHECK (valid_until IS NULL OR valid_until>valid_from)
);
CREATE INDEX memories_terms_idx ON memories USING gin(terms);
CREATE INDEX memories_active_idx ON memories(updated_at DESC) WHERE deleted_at IS NULL;
CREATE TABLE events (
 id uuid PRIMARY KEY, memory_id uuid NOT NULL REFERENCES memories(id), revision integer NOT NULL,
 operation text NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(memory_id,revision)
);
CREATE TABLE projections (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, memory_id uuid NOT NULL REFERENCES memories(id),
 revision integer NOT NULL, chunk_index integer NOT NULL, content text NOT NULL,
 embedding vector(512) NOT NULL, model text NOT NULL,
 UNIQUE(memory_id,revision,chunk_index,model)
);
CREATE INDEX projections_memory_idx ON projections(memory_id,revision);
CREATE TABLE outbox (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, memory_id uuid NOT NULL REFERENCES memories(id),
 revision integer NOT NULL, status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','failed','cancelled')),
 attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz, lease_token uuid, last_error text, updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(memory_id,revision)
);
CREATE INDEX outbox_ready_idx ON outbox(available_at) WHERE status IN ('pending','running');
CREATE TABLE deletion_ledger (
 id uuid PRIMARY KEY, target_type text NOT NULL CHECK(target_type IN ('memory','conversation','expiry')),
 target_id uuid, cutoff timestamptz, epoch bigint NOT NULL, deleted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE messages (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, conversation_id uuid NOT NULL,
 role text NOT NULL CHECK(role IN ('user','assistant')), content text NOT NULL,
 source_ids uuid[] NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages(conversation_id,id DESC);
CREATE INDEX messages_sources_idx ON messages USING gin(source_ids);
CREATE TABLE model_calls (
 id uuid PRIMARY KEY, conversation_id uuid NOT NULL, source_ids uuid[] NOT NULL,
 model text NOT NULL, status text NOT NULL, input_tokens integer, output_tokens integer,
 created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version) VALUES(1);
