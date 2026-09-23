-- Papel do servidor MCP (server/decision_memory). Aplicar depois de schema.sql;
-- rodar de novo é seguro. A senha não fica aqui:
--   ALTER ROLE dm_app PASSWORD '...';
-- e vai para o Secret Manager (ver server/decision_memory/README.md).
--
-- O servidor só lê e acrescenta. Nenhum UPDATE, nenhum DELETE, e nenhuma
-- escrita em expectation, review, person ou project: expectativa e revisão são
-- da pessoa (ADR 0002), e cadastro não é trabalho de agente.

BEGIN;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dm_app') THEN
        CREATE ROLE dm_app LOGIN;
    END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dm_app;
GRANT USAGE ON SCHEMA public TO dm_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dm_app;
GRANT INSERT ON
    decision, alternative, evidence, decision_evidence,
    learning, decision_learning, evidence_learning,
    tag, decision_tag, evidence_tag, learning_tag,
    provenance, idempotency_key
TO dm_app;

COMMIT;
