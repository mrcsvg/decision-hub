-- Testes das invariantes de db/schema.sql.
-- Uso: psql -v ON_ERROR_STOP=1 -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
-- Tudo roda numa transação desfeita ao final; o banco não é alterado.

BEGIN;

-- Dados-base ------------------------------------------------------------------
INSERT INTO person (id, name, email) VALUES
    ('00000000-0000-0000-0000-000000000001', 'Ana', 'ana@example.com');
INSERT INTO job_title (id, name) VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'Coordenadora'),
    ('00000000-0000-0000-0000-0000000000a2', 'Diretora');
INSERT INTO org_area (id, name) VALUES
    ('00000000-0000-0000-0000-0000000000b1', 'Produto');
INSERT INTO assignment VALUES
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a1',
     '00000000-0000-0000-0000-0000000000b1', '2023-01-01', '2025-01-01'),
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a2',
     '00000000-0000-0000-0000-0000000000b1', '2025-01-01', NULL);

-- 1. Snapshot do vínculo na data da decisão ------------------------------------
INSERT INTO decision (id, slug, title, description, decided_on, decider_person_id)
VALUES ('00000000-0000-0000-0000-0000000000d1', 'checkout-sem-confirmacao',
        'Remover etapa de confirmação', 'Remover a tela de confirmação do checkout',
        '2023-06-10', '00000000-0000-0000-0000-000000000001');

DO $$ BEGIN
    ASSERT (SELECT job_title_at_decision FROM decision
             WHERE id = '00000000-0000-0000-0000-0000000000d1')
         = '00000000-0000-0000-0000-0000000000a1',
        'decisão de 2023 deveria registrar o cargo de 2023 (Coordenadora)';
END $$;

-- 2. Vigências sobrepostas são rejeitadas --------------------------------------
DO $$ BEGIN
    INSERT INTO assignment VALUES
        ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a1',
         '00000000-0000-0000-0000-0000000000b1', '2024-06-01', '2024-12-01');
    RAISE EXCEPTION 'FALHOU: vigência sobreposta foi aceita';
EXCEPTION WHEN exclusion_violation THEN NULL;
END $$;

-- 3. Expectativa é append-only --------------------------------------------------
INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric, expected_magnitude, due_on)
VALUES ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-000000000001',
        0.70, 'conversão do checkout', '+2 p.p.', '2023-09-10');

DO $$ BEGIN
    UPDATE expectation SET confidence = 0.95;
    RAISE EXCEPTION 'FALHOU: expectativa foi alterada';
EXCEPTION WHEN integrity_constraint_violation THEN NULL;
END $$;

DO $$ BEGIN
    DELETE FROM expectation;
    RAISE EXCEPTION 'FALHOU: expectativa foi apagada';
EXCEPTION WHEN integrity_constraint_violation THEN NULL;
END $$;

-- 4. Expectativa não entra depois do desfecho -----------------------------------
INSERT INTO review (decision_id, due_on, done_on, verdict, reviewed_by)
VALUES ('00000000-0000-0000-0000-0000000000d1', '2023-09-10', '2023-09-12', 'better',
        '00000000-0000-0000-0000-000000000001');

DO $$ BEGIN
    INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric, expected_magnitude, due_on)
    VALUES ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-000000000001',
            0.90, 'conversão do checkout', '+3 p.p.', '2023-12-01');
    RAISE EXCEPTION 'FALHOU: expectativa aceita depois da revisão';
EXCEPTION WHEN integrity_constraint_violation THEN NULL;
END $$;

-- 5. Evidência idempotente por (source_system, external_id) ---------------------
INSERT INTO evidence (kind, title, source_system, external_id, conformance_level)
VALUES ('experiment', 'Checkout sem confirmação', 'growthbook', 'exp_123', 1);

INSERT INTO evidence (kind, title, source_system, external_id, conformance_level)
VALUES ('experiment', 'Checkout sem confirmação (reimportado)', 'growthbook', 'exp_123', 2)
ON CONFLICT ON CONSTRAINT evidence_source_identity
DO UPDATE SET title = EXCLUDED.title, conformance_level = EXCLUDED.conformance_level;

DO $$ BEGIN
    ASSERT (SELECT count(*) FROM evidence WHERE external_id = 'exp_123') = 1,
        'reimportação deveria atualizar, não duplicar';
END $$;

-- 6. Agente sem modelo ou sem principal é rejeitado -----------------------------
DO $$ BEGIN
    INSERT INTO provenance (object_type, object_id, author_kind)
    VALUES ('decision', '00000000-0000-0000-0000-0000000000d1', 'agent');
    RAISE EXCEPTION 'FALHOU: procedência de agente sem modelo foi aceita';
EXCEPTION WHEN check_violation THEN NULL;
END $$;

-- 7. Busca textual --------------------------------------------------------------
DO $$ BEGIN
    ASSERT EXISTS (SELECT 1 FROM decision WHERE search @@ plainto_tsquery('simple', 'checkout')),
        'busca textual em decision não encontrou o registro';
END $$;

-- 8. Chave de idempotência é única por pessoa ---------------------------------
INSERT INTO idempotency_key (principal_person_id, key, object_type, object_id)
VALUES ('00000000-0000-0000-0000-000000000001', 'k1', 'decision',
        '00000000-0000-0000-0000-0000000000d1');

DO $$ BEGIN
    INSERT INTO idempotency_key (principal_person_id, key, object_type, object_id)
    VALUES ('00000000-0000-0000-0000-000000000001', 'k1', 'decision',
            '00000000-0000-0000-0000-0000000000d1');
    RAISE EXCEPTION 'FALHOU: chave de idempotência repetida foi aceita';
EXCEPTION WHEN unique_violation THEN NULL;
END $$;

-- 9. O papel do servidor MCP só lê e acrescenta -------------------------------
-- Expectativa é da pessoa, na atestação (ADR 0002); o banco garante isso mesmo
-- que o código do servidor erre.
SET ROLE dm_app;

DO $$ BEGIN
    INSERT INTO expectation (decision_id, recorded_by, confidence, expected_metric,
                             expected_magnitude, due_on)
    VALUES ('00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-000000000001',
            0.5, 'conversão', '+1 p.p.', '2030-01-01');
    RAISE EXCEPTION 'FALHOU: dm_app gravou expectativa';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO review (decision_id, due_on)
    VALUES ('00000000-0000-0000-0000-0000000000d1', '2030-01-01');
    RAISE EXCEPTION 'FALHOU: dm_app gravou revisão';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    UPDATE decision SET title = 'x' WHERE id = '00000000-0000-0000-0000-0000000000d1';
    RAISE EXCEPTION 'FALHOU: dm_app alterou decisão';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    DELETE FROM evidence;
    RAISE EXCEPTION 'FALHOU: dm_app apagou evidência';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO person (name, email) VALUES ('Intrusa', 'intrusa@example.com');
    RAISE EXCEPTION 'FALHOU: dm_app cadastrou pessoa';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

-- Nem atesta o que escreve: atestação é humana (ADR 0002). As colunas de
-- atestação, estado e importação ficam fora do INSERT concedido.
DO $$ BEGIN
    INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id,
                            model, attested_by, attested_at)
    VALUES ('decision', '00000000-0000-0000-0000-0000000000d1', 'agent',
            '00000000-0000-0000-0000-000000000001', 'modelo',
            '00000000-0000-0000-0000-000000000001', now());
    RAISE EXCEPTION 'FALHOU: dm_app atestou procedência';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO decision (slug, title, description, decided_on, decider_person_id, state)
    VALUES ('autoatestada', 'Autoatestada', 'x', '2025-06-01',
            '00000000-0000-0000-0000-000000000001', 'attested');
    RAISE EXCEPTION 'FALHOU: dm_app gravou decisão já atestada';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

DO $$ BEGIN
    INSERT INTO learning (summary, state) VALUES ('Autoatestada', 'attested');
    RAISE EXCEPTION 'FALHOU: dm_app gravou lição já atestada';
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;

-- Nenhuma escrita nas tabelas de cadastro e de atestação humana, e nenhum
-- UPDATE, DELETE ou TRUNCATE em tabela alguma. has_any_column_privilege pega
-- também o privilégio concedido por coluna.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['expectation', 'review', 'person', 'assignment', 'project',
                             'job_title', 'org_area', 'indicator', 'measurement'] LOOP
        ASSERT NOT has_any_column_privilege('dm_app', t, 'INSERT, UPDATE')
           AND NOT has_table_privilege('dm_app', t, 'DELETE, TRUNCATE'),
            format('FALHOU: dm_app pode escrever em %s', t);
    END LOOP;
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        ASSERT NOT has_any_column_privilege('dm_app', t, 'UPDATE')
           AND NOT has_table_privilege('dm_app', t, 'DELETE, TRUNCATE'),
            format('FALHOU: dm_app pode alterar ou apagar em %s', t);
    END LOOP;
    ASSERT NOT has_schema_privilege('dm_app', 'public', 'CREATE'),
        'FALHOU: dm_app pode criar objetos no schema public';
END $$;

-- e acrescenta o que as ferramentas precisam, só nas colunas concedidas
DO $$
DECLARE
    v_job_title uuid;
BEGIN
    INSERT INTO decision (slug, title, description, decided_on, decider_person_id)
    VALUES ('proposta-por-agente', 'Proposta por agente', 'x', '2025-06-01',
            '00000000-0000-0000-0000-000000000001')
    RETURNING job_title_at_decision INTO v_job_title;
    ASSERT v_job_title = '00000000-0000-0000-0000-0000000000a2',
        'decisão inserida por dm_app deveria registrar o cargo de 2025 (Diretora)';
END $$;

INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id, model, source_ref)
VALUES ('decision', '00000000-0000-0000-0000-0000000000d1', 'agent',
        '00000000-0000-0000-0000-000000000001', 'modelo', 'mcp');

INSERT INTO tag (name) VALUES ('papel-dm-app') ON CONFLICT (name) DO NOTHING;
INSERT INTO tag (name) VALUES ('papel-dm-app') ON CONFLICT (name) DO NOTHING;

INSERT INTO decision_tag (decision_id, tag_id)
SELECT '00000000-0000-0000-0000-0000000000d1', id FROM tag WHERE name = 'papel-dm-app'
ON CONFLICT DO NOTHING;
INSERT INTO decision_tag (decision_id, tag_id)
SELECT '00000000-0000-0000-0000-0000000000d1', id FROM tag WHERE name = 'papel-dm-app'
ON CONFLICT DO NOTHING;

DO $$ BEGIN
    ASSERT (SELECT count(*) FROM decision_tag dt JOIN tag t ON t.id = dt.tag_id
             WHERE t.name = 'papel-dm-app') = 1,
        'reenvio de tag deveria ser ignorado, não duplicado';
END $$;

RESET ROLE;

\echo 'Todas as invariantes passaram.'

ROLLBACK;
