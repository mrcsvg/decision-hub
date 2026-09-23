-- Aplica em banco criado antes de 2026-09-23 o que db/schema.sql já traz.
-- Uso: psql -v ON_ERROR_STOP=1 -f db/migrations/2026-09-23-idempotency-key.sql
-- Depois, rode db/grants.sql: a tabela nova só chega ao papel dm_app assim.
BEGIN;

CREATE TABLE IF NOT EXISTS idempotency_key (
    principal_person_id  uuid        NOT NULL REFERENCES person (id),
    key                  text        NOT NULL CHECK (key <> ''),
    object_type          text        NOT NULL CHECK (object_type IN ('decision')),
    object_id            uuid        NOT NULL,  -- sem FK: polimórfico por object_type (hoje só 'decision')
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_person_id, key)
);

COMMIT;
