ALTER TABLE memories ADD COLUMN origin text NOT NULL DEFAULT 'manual' CHECK(origin IN ('manual','automatic'));
ALTER TABLE memories ADD COLUMN kind text NOT NULL DEFAULT 'fact' CHECK(kind IN ('fact','preference','instruction','temporary'));
CREATE TABLE memory_evidence (
 memory_id uuid NOT NULL REFERENCES memories(id), revision integer NOT NULL,
 conversation_id uuid NOT NULL, quote text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(memory_id,revision)
);
INSERT INTO schema_migrations(version) VALUES(2);
