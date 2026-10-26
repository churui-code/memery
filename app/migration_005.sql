-- Additive audit metadata for already initialized v4 experiment databases.
ALTER TABLE model_calls ADD COLUMN estimator_version text;
INSERT INTO schema_migrations(version) VALUES(5);
