-- Papel do servidor MCP (server/decision_memory). Aplicar depois de schema.sql;
-- rodar de novo é seguro, e necessário depois de criar tabela nova: os GRANT
-- com ON ALL TABLES só alcançam as tabelas que já existem. A senha não fica aqui:
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

-- REVOKE em tabela também revoga os privilégios concedidos por coluna: rodar
-- de novo não deixa sobra.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dm_app;

-- Sem efeito a partir do PostgreSQL 15; no 14 (mínimo do README) fecha o
-- CREATE que PUBLIC tem no schema public por padrão.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO dm_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dm_app;

GRANT INSERT ON
    alternative, decision_evidence, decision_learning, evidence_learning,
    tag, decision_tag, evidence_tag, learning_tag, idempotency_key
TO dm_app;

-- Nestas quatro, INSERT só nas colunas que o agente preenche. Atestação é
-- humana (ADR 0002): attested_by/attested_at, state (que fica no padrão
-- 'proposed') e o vínculo na data da decisão (que o gatilho preenche) ficam de
-- fora, assim como as colunas de importação da evidência. O banco não impede
-- que dm_app grave author_kind = 'human' ou 'import' em provenance: isso
-- continua responsabilidade do servidor.
GRANT INSERT (object_type, object_id, author_kind, principal_person_id, model, source_ref)
    ON provenance TO dm_app;
GRANT INSERT (slug, title, context, description, door, decided_on, decider_person_id,
              project_id)
    ON decision TO dm_app;
GRANT INSERT (summary, recorded_on)
    ON learning TO dm_app;
GRANT INSERT (kind, title, summary, url, source_system, external_id, strength)
    ON evidence TO dm_app;

COMMIT;
