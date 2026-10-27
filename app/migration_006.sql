-- Reflection never creates a new autobiographical source or changes old text.
CREATE TABLE reflection_settings (
 id integer PRIMARY KEY CHECK(id=1), enabled boolean NOT NULL DEFAULT false,
 last_completed_at timestamptz, last_started_at timestamptz, last_chat_activity_at timestamptz,
 last_daily_date date, last_chat_business_at timestamptz, last_started_business_at timestamptz,
 last_daily_business_date date, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO reflection_settings(id) VALUES(1);
CREATE TABLE reflection_daily_usage (
 day date PRIMARY KEY, rounds integer NOT NULL DEFAULT 0 CHECK(rounds>=0),
 calls integer NOT NULL DEFAULT 0 CHECK(calls>=0)
);
CREATE TABLE reflection_runs (
 id uuid PRIMARY KEY, status text NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
 automatic boolean NOT NULL DEFAULT false, input_signature text NOT NULL,
 policy_version text NOT NULL, expected_epoch bigint NOT NULL,
 source_manifest jsonb NOT NULL DEFAULT '[]', target_versions jsonb NOT NULL DEFAULT '[]',
 pending_ids jsonb NOT NULL DEFAULT '[]', result_ids jsonb NOT NULL DEFAULT '[]',
 model_mode text NOT NULL DEFAULT 'real' CHECK(model_mode IN ('real','mock')),
 completed_steps jsonb NOT NULL DEFAULT '[]', audit jsonb NOT NULL DEFAULT '{}',
 trigger_reasons jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), business_at timestamptz NOT NULL,
 started_at timestamptz, finished_at timestamptz, lease_until timestamptz, lease_token uuid,
 quota_day date, error_code text, invalidated_at timestamptz
);
CREATE UNIQUE INDEX reflection_success_signature_idx ON reflection_runs(input_signature,policy_version) WHERE status='completed';
CREATE UNIQUE INDEX reflection_one_active_idx ON reflection_runs((1)) WHERE status IN ('pending','running');
CREATE INDEX reflection_runs_created_idx ON reflection_runs(created_at DESC,id);
CREATE TABLE reflection_processed_episodes (
 episode_id uuid PRIMARY KEY REFERENCES episodes(id), revision integer NOT NULL,
 source_hash text NOT NULL, run_id uuid NOT NULL REFERENCES reflection_runs(id),
 processed_at timestamptz NOT NULL
);
ALTER TABLE episodes ADD COLUMN reflection_correction boolean NOT NULL DEFAULT false;
ALTER TABLE self_observations ADD COLUMN generation_type text NOT NULL DEFAULT 'conversation'
 CHECK(generation_type IN ('conversation','reflection'));
ALTER TABLE self_observations ADD COLUMN reflection_run_id uuid REFERENCES reflection_runs(id);
ALTER TABLE self_observations ADD COLUMN evidence_manifest jsonb NOT NULL DEFAULT '[]';
ALTER TABLE model_calls ALTER COLUMN conversation_id DROP NOT NULL;
ALTER TABLE model_calls ADD COLUMN reflection_run_id uuid REFERENCES reflection_runs(id);
CREATE INDEX model_calls_reflection_idx ON model_calls(reflection_run_id) WHERE reflection_run_id IS NOT NULL;
INSERT INTO schema_migrations(version) VALUES(6);
