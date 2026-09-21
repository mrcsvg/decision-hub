-- Testes das invariantes de db/schema.sql.
-- Uso: psql -v ON_ERROR_STOP=1 -f db/schema.sql -f db/test_invariants.sql
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

\echo 'Todas as invariantes passaram.'

ROLLBACK;
