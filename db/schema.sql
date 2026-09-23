-- =============================================================================
-- decision-memory · modelo lógico consolidado v0
-- PostgreSQL 14+
--
-- Identificadores em inglês para facilitar contribuição externa. Mapeamento
-- para o modelo lógico de docs/concepcao.md:
--
--   PESSOA            -> person              CARGO             -> job_title
--   AREA              -> org_area            LOTACAO           -> assignment
--   PROJETO           -> project             INDICADOR         -> indicator
--   MEDICAO           -> measurement         DECISAO           -> decision
--   ALTERNATIVA       -> alternative         EXPECTATIVA       -> expectation
--   REVISAO           -> review              EVIDENCIA         -> evidence
--   DECISAO_EVIDENCIA -> decision_evidence   LICAO             -> learning
--   DECISAO_LICAO     -> decision_learning   EVIDENCIA_LICAO   -> evidence_learning
--   TAG (+ *_TAG)     -> tag (+ *_tag)       PROCEDENCIA       -> provenance
--
-- Invariantes garantidas pelo banco (não apenas pela aplicação):
--   1. expectation é append-only e não aceita inserção depois de uma revisão
--      realizada: a expectativa é congelada antes do desfecho.
--   2. decision guarda cargo e área do decisor NA DATA da decisão.
--   3. assignment não admite vigências sobrepostas para a mesma pessoa.
--   4. evidence é idempotente por (source_system, external_id).
--   5. escrita por agente é idempotente por (pessoa, chave): idempotency_key.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- -----------------------------------------------------------------------------
-- Tipos
-- -----------------------------------------------------------------------------

CREATE TYPE door_type          AS ENUM ('one_way', 'two_way');
CREATE TYPE metric_direction   AS ENUM ('increase', 'decrease');
CREATE TYPE evidence_kind      AS ENUM ('experiment', 'study', 'analysis', 'document', 'external');
CREATE TYPE evidence_strength  AS ENUM ('causal', 'correlational', 'anecdotal');
CREATE TYPE evidence_role      AS ENUM ('supports', 'contradicts', 'discarded');
CREATE TYPE review_verdict     AS ENUM ('as_expected', 'better', 'worse', 'inconclusive');
CREATE TYPE record_state       AS ENUM ('proposed', 'attested', 'discarded');
CREATE TYPE author_kind        AS ENUM ('human', 'agent', 'import');

-- -----------------------------------------------------------------------------
-- Pessoas e vínculo organizacional
-- -----------------------------------------------------------------------------

CREATE TABLE person (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text        NOT NULL,
    email       text        UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE job_title (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name  text NOT NULL UNIQUE
);

CREATE TABLE org_area (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL UNIQUE,
    manager_person_id  uuid REFERENCES person (id)
);

-- Vínculo histórico. Uma pessoa pode mudar de cargo e área; decisões antigas
-- continuam apontando para o vínculo da época (ver decision_snapshot_assignment).
CREATE TABLE assignment (
    person_id     uuid NOT NULL REFERENCES person (id),
    job_title_id  uuid NOT NULL REFERENCES job_title (id),
    org_area_id   uuid NOT NULL REFERENCES org_area (id),
    valid_from    date NOT NULL,
    valid_to      date,
    PRIMARY KEY (person_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (
        person_id WITH =,
        daterange(valid_from, valid_to, '[)') WITH &&
    )
);

-- -----------------------------------------------------------------------------
-- Contexto organizacional
-- -----------------------------------------------------------------------------

CREATE TABLE project (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    starts_on  date,
    ends_on    date,
    status     text NOT NULL DEFAULT 'active'
               CHECK (status IN ('planned', 'active', 'closed', 'cancelled')),
    CHECK (ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on)
);

CREATE TABLE indicator (
    id          uuid             PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid             NOT NULL REFERENCES project (id),
    name        text             NOT NULL,
    unit        text,
    direction   metric_direction NOT NULL,
    UNIQUE (project_id, name)
);

-- Separada de indicator: a chave real de uma medição é (indicador, período).
CREATE TABLE measurement (
    indicator_id  uuid    NOT NULL REFERENCES indicator (id),
    period        date    NOT NULL,
    value         numeric NOT NULL,
    PRIMARY KEY (indicator_id, period)
);

-- -----------------------------------------------------------------------------
-- Decisão
-- -----------------------------------------------------------------------------

CREATE TABLE decision (
    id                      uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                    text         NOT NULL UNIQUE,
    title                   text         NOT NULL,
    context                 text,
    description             text         NOT NULL,
    door                    door_type    NOT NULL DEFAULT 'two_way',
    decided_on              date         NOT NULL,
    decider_person_id       uuid         NOT NULL REFERENCES person (id),
    project_id              uuid         REFERENCES project (id),  -- opcional: nem toda decisão de produto pertence a um projeto
    job_title_at_decision   uuid         REFERENCES job_title (id),
    org_area_at_decision    uuid         REFERENCES org_area (id),
    state                   record_state NOT NULL DEFAULT 'proposed',
    created_at              timestamptz  NOT NULL DEFAULT now(),
    search                  tsvector GENERATED ALWAYS AS (
        to_tsvector('simple',
            coalesce(title, '') || ' ' || coalesce(context, '') || ' ' || coalesce(description, ''))
    ) STORED
);

-- Preenche cargo e área na data da decisão a partir de assignment,
-- sem sobrescrever o que já tiver sido informado.
CREATE FUNCTION decision_snapshot_assignment() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_job_title uuid;
    v_area      uuid;
BEGIN
    SELECT a.job_title_id, a.org_area_id
      INTO v_job_title, v_area
      FROM assignment a
     WHERE a.person_id = NEW.decider_person_id
       AND daterange(a.valid_from, a.valid_to, '[)') @> NEW.decided_on;

    NEW.job_title_at_decision := coalesce(NEW.job_title_at_decision, v_job_title);
    NEW.org_area_at_decision  := coalesce(NEW.org_area_at_decision,  v_area);
    RETURN NEW;
END;
$$;

CREATE TRIGGER decision_snapshot_assignment
    BEFORE INSERT ON decision
    FOR EACH ROW EXECUTE FUNCTION decision_snapshot_assignment();

CREATE TABLE alternative (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id       uuid NOT NULL REFERENCES decision (id) ON DELETE CASCADE,
    description       text NOT NULL,
    rejection_reason  text NOT NULL
);

-- Expectativa: registrada por uma pessoa, antes do desfecho, nunca alterada.
-- Uma nova estimativa é uma nova linha. Não é exposta para escrita via MCP
-- (ver docs/adr/0002-mcp-first.md).
CREATE TABLE expectation (
    decision_id         uuid          NOT NULL REFERENCES decision (id),
    recorded_at         timestamptz   NOT NULL DEFAULT now(),
    recorded_by         uuid          NOT NULL REFERENCES person (id),
    confidence          numeric(3, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    expected_metric     text          NOT NULL,
    expected_magnitude  text          NOT NULL,
    due_on              date          NOT NULL,
    PRIMARY KEY (decision_id, recorded_at)
);

CREATE TABLE review (
    id           uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id  uuid           NOT NULL REFERENCES decision (id),
    due_on       date           NOT NULL,
    done_on      date,
    verdict      review_verdict,
    notes        text,
    reviewed_by  uuid           REFERENCES person (id),
    CHECK ((done_on IS NULL) = (verdict IS NULL)),
    CHECK (done_on IS NULL OR reviewed_by IS NOT NULL)
);

CREATE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% é append-only: registre uma nova linha em vez de alterar', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

CREATE TRIGGER expectation_append_only
    BEFORE UPDATE OR DELETE ON expectation
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION expectation_before_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM review r
         WHERE r.decision_id = NEW.decision_id
           AND r.done_on IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'decisão % já foi revisada: expectativa só pode ser registrada antes do desfecho', NEW.decision_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER expectation_before_outcome
    BEFORE INSERT ON expectation
    FOR EACH ROW EXECUTE FUNCTION expectation_before_outcome();

-- -----------------------------------------------------------------------------
-- Evidência
-- -----------------------------------------------------------------------------

-- Polimórfica. Para experimentos, `normalized` guarda o registro validado
-- contra spec/experiment-record-v0.schema.json e `raw_payload` a resposta
-- original da plataforma de origem.
CREATE TABLE evidence (
    id                 uuid              PRIMARY KEY DEFAULT gen_random_uuid(),
    kind               evidence_kind     NOT NULL,
    title              text              NOT NULL,
    summary            text,
    url                text,
    source_system      text,
    external_id        text,
    strength           evidence_strength,
    conformance_level  smallint          CHECK (conformance_level BETWEEN 0 AND 2),
    normalized         jsonb,
    raw_payload        jsonb,
    imported_at        timestamptz,
    created_at         timestamptz       NOT NULL DEFAULT now(),
    search             tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))
    ) STORED,
    CONSTRAINT evidence_source_identity UNIQUE (source_system, external_id),
    CHECK ((source_system IS NULL) = (external_id IS NULL)),
    CHECK (kind <> 'experiment' OR conformance_level IS NOT NULL OR source_system IS NULL)
);

-- O papel da evidência é propriedade da relação, não da evidência.
CREATE TABLE decision_evidence (
    decision_id  uuid          NOT NULL REFERENCES decision (id) ON DELETE CASCADE,
    evidence_id  uuid          NOT NULL REFERENCES evidence (id),
    role         evidence_role NOT NULL,
    weight       numeric(3, 2) CHECK (weight BETWEEN 0 AND 1),
    note         text,
    PRIMARY KEY (decision_id, evidence_id)
);

-- -----------------------------------------------------------------------------
-- Aprendizado
-- -----------------------------------------------------------------------------

CREATE TABLE learning (
    id           uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    summary      text         NOT NULL,
    recorded_on  date         NOT NULL DEFAULT current_date,
    state        record_state NOT NULL DEFAULT 'proposed',
    search       tsvector GENERATED ALWAYS AS (to_tsvector('simple', summary)) STORED
);

-- Uma lição nasce de uma decisão, de uma evidência, ou de ambas; e pode ser
-- associada a várias decisões em projetos diferentes.
CREATE TABLE decision_learning (
    decision_id  uuid NOT NULL REFERENCES decision (id) ON DELETE CASCADE,
    learning_id  uuid NOT NULL REFERENCES learning (id) ON DELETE CASCADE,
    PRIMARY KEY (decision_id, learning_id)
);

CREATE TABLE evidence_learning (
    evidence_id  uuid NOT NULL REFERENCES evidence (id) ON DELETE CASCADE,
    learning_id  uuid NOT NULL REFERENCES learning (id) ON DELETE CASCADE,
    PRIMARY KEY (evidence_id, learning_id)
);

-- -----------------------------------------------------------------------------
-- Taxonomia compartilhada
-- -----------------------------------------------------------------------------

CREATE TABLE tag (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name  text NOT NULL UNIQUE CHECK (name = lower(name) AND name <> '')
);

CREATE TABLE decision_tag (
    decision_id  uuid NOT NULL REFERENCES decision (id) ON DELETE CASCADE,
    tag_id       uuid NOT NULL REFERENCES tag (id),
    PRIMARY KEY (decision_id, tag_id)
);

CREATE TABLE evidence_tag (
    evidence_id  uuid NOT NULL REFERENCES evidence (id) ON DELETE CASCADE,
    tag_id       uuid NOT NULL REFERENCES tag (id),
    PRIMARY KEY (evidence_id, tag_id)
);

CREATE TABLE learning_tag (
    learning_id  uuid NOT NULL REFERENCES learning (id) ON DELETE CASCADE,
    tag_id       uuid NOT NULL REFERENCES tag (id),
    PRIMARY KEY (learning_id, tag_id)
);

-- -----------------------------------------------------------------------------
-- Governança do registro
-- -----------------------------------------------------------------------------

-- Quem escreveu cada registro (pessoa, agente ou importação) e quem o atestou.
CREATE TABLE provenance (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    object_type          text        NOT NULL
                         CHECK (object_type IN ('decision', 'evidence', 'learning', 'review', 'alternative')),
    object_id            uuid        NOT NULL,
    author_kind          author_kind NOT NULL,
    principal_person_id  uuid        REFERENCES person (id),  -- em nome de quem o agente agiu
    model                text,
    source_ref           text,                                -- conversa, job de importação, PR
    created_at           timestamptz NOT NULL DEFAULT now(),
    attested_by          uuid        REFERENCES person (id),
    attested_at          timestamptz,
    CHECK ((attested_by IS NULL) = (attested_at IS NULL)),
    CHECK (author_kind <> 'agent' OR (model IS NOT NULL AND principal_person_id IS NOT NULL))
);

-- Idempotência de escrita por agente (propose_decision). Reenvio com a mesma
-- chave, pela mesma pessoa, devolve o registro já criado em vez de duplicá-lo.
CREATE TABLE idempotency_key (
    principal_person_id  uuid        NOT NULL REFERENCES person (id),
    key                  text        NOT NULL CHECK (key <> ''),
    object_type          text        NOT NULL CHECK (object_type IN ('decision')),
    object_id            uuid        NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_person_id, key)
);

-- -----------------------------------------------------------------------------
-- Índices
-- -----------------------------------------------------------------------------

CREATE INDEX decision_search_idx        ON decision   USING gin (search);
CREATE INDEX evidence_search_idx        ON evidence   USING gin (search);
CREATE INDEX learning_search_idx        ON learning   USING gin (search);

CREATE INDEX decision_project_idx       ON decision (project_id);
CREATE INDEX decision_decider_idx       ON decision (decider_person_id);
CREATE INDEX decision_state_idx         ON decision (state) WHERE state = 'proposed';
CREATE INDEX learning_state_idx         ON learning (state) WHERE state = 'proposed';
CREATE INDEX review_pending_idx         ON review (due_on) WHERE done_on IS NULL;
CREATE INDEX review_decision_idx        ON review (decision_id);
CREATE INDEX decision_evidence_ev_idx   ON decision_evidence (evidence_id);
CREATE INDEX evidence_learning_lr_idx   ON evidence_learning (learning_id);
CREATE INDEX decision_learning_lr_idx   ON decision_learning (learning_id);
CREATE INDEX provenance_object_idx      ON provenance (object_type, object_id);
CREATE INDEX provenance_unattested_idx  ON provenance (created_at) WHERE attested_at IS NULL;

COMMIT;
