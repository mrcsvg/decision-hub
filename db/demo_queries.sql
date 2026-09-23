-- Consultas para a demonstração. Somente leitura.
--   psql -f db/demo_queries.sql

\echo '1. Quantas linhas em cada tabela'
SELECT relname AS tabela, n_live_tup AS linhas
  FROM pg_stat_user_tables ORDER BY relname;

\echo '2. Decisões com o cargo do decisor NA DATA da decisão (vínculo histórico)'
SELECT d.decided_on, p.name AS decisor, j.name AS cargo_na_data, a.name AS area,
       d.state, d.title
  FROM decision d
  JOIN person p       ON p.id = d.decider_person_id
  LEFT JOIN job_title j ON j.id = d.job_title_at_decision
  LEFT JOIN org_area a  ON a.id = d.org_area_at_decision
 ORDER BY d.decided_on;

\echo '3. Calibração: confiança declarada antes x veredito da revisão'
SELECT x.confidence AS confianca, r.verdict AS veredito, x.expected_metric AS metrica,
       x.expected_magnitude AS esperado, d.title
  FROM review r
  JOIN decision d    ON d.id = r.decision_id
  JOIN expectation x ON x.decision_id = d.id
 WHERE r.done_on IS NOT NULL
 ORDER BY x.confidence DESC;

\echo '4. Revisões vencidas, que ninguém fez'
SELECT r.due_on AS vencida_em, current_date - r.due_on AS dias, p.name AS dono, d.title
  FROM review r
  JOIN decision d ON d.id = r.decision_id
  JOIN person p   ON p.id = d.decider_person_id
 WHERE r.done_on IS NULL AND r.due_on < current_date
 ORDER BY r.due_on;

\echo '5. Evidência contrária registrada (o que distingue memória de justificativa)'
SELECT d.title AS decisao, e.title AS evidencia, e.kind, de.weight AS peso, de.note
  FROM decision_evidence de
  JOIN decision d ON d.id = de.decision_id
  JOIN evidence e ON e.id = de.evidence_id
 WHERE de.role = 'contradicts'
 ORDER BY d.title;

\echo '6. O que o agente escreveu e ainda espera atestação de uma pessoa'
SELECT pv.object_type AS tipo, pv.created_at::date AS em, pp.name AS em_nome_de,
       coalesce(d.title, e.title, l.summary) AS registro
  FROM provenance pv
  LEFT JOIN person pp   ON pp.id = pv.principal_person_id
  LEFT JOIN decision d  ON pv.object_type = 'decision' AND d.id = pv.object_id
  LEFT JOIN evidence e  ON pv.object_type = 'evidence' AND e.id = pv.object_id
  LEFT JOIN learning l  ON pv.object_type = 'learning' AND l.id = pv.object_id
 WHERE pv.author_kind = 'agent' AND pv.attested_at IS NULL
 ORDER BY pv.created_at;

\echo '7. Lições e de onde vieram'
SELECT l.state, l.summary,
       (SELECT string_agg(d.title, '; ') FROM decision_learning dl
          JOIN decision d ON d.id = dl.decision_id WHERE dl.learning_id = l.id) AS decisoes,
       (SELECT count(*) FROM evidence_learning el WHERE el.learning_id = l.id) AS evidencias
  FROM learning l ORDER BY l.recorded_on;

\echo '8. Indicadores por projeto, mês a mês'
SELECT pr.name AS projeto, i.name AS indicador, i.direction,
       string_agg(to_char(m.period, 'Mon') || ' ' || m.value, '  ' ORDER BY m.period) AS serie
  FROM indicator i
  JOIN project pr   ON pr.id = i.project_id
  JOIN measurement m ON m.indicator_id = i.id
 GROUP BY pr.name, i.name, i.direction
 ORDER BY pr.name, i.name;

\echo '9. Evidência por origem (sem comparar efeitos entre origens: ADR 0003)'
SELECT coalesce(source_system, '(sem origem)') AS origem, kind, count(*) AS evidencias
  FROM evidence GROUP BY 1, 2 ORDER BY 1, 2;
