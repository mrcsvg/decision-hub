-- Dados de demonstração para as tabelas que o corpus de fixtures não cobre:
-- job_title, org_area, assignment, indicator e measurement. Fictícios, como as
-- fixtures, e ligados a elas por e-mail de pessoa e nome de projeto.
--
-- Rodar depois da carga das fixtures (python -m decision_memory.seed), como
-- dm_admin. Rodar de novo não duplica nada.
--
--   psql -v ON_ERROR_STOP=1 -f db/demo.sql
--
-- Ana muda de cargo em abril de 2026: as duas decisões dela (março e maio)
-- mostram cargos diferentes, que é a invariante de vínculo na data da decisão.

BEGIN;

-- Cargos e áreas --------------------------------------------------------------
INSERT INTO job_title (name) VALUES
    ('Gerente de Produto'),
    ('Head de Produto'),
    ('Designer de Produto'),
    ('Analista de Dados'),
    ('Coordenador de Precificação')
ON CONFLICT (name) DO NOTHING;

INSERT INTO org_area (name, manager_person_id) VALUES
    ('Produto', (SELECT id FROM person WHERE email = 'ana@exemplo.com.br')),
    ('Dados', (SELECT id FROM person WHERE email = 'diego@exemplo.com.br')),
    ('Precificação', (SELECT id FROM person WHERE email = 'diego@exemplo.com.br'))
ON CONFLICT (name) DO NOTHING;

-- Vínculos, com histórico -----------------------------------------------------
INSERT INTO assignment (person_id, job_title_id, org_area_id, valid_from, valid_to)
SELECT p.id, j.id, a.id, v.valid_from::date, v.valid_to::date
  FROM (VALUES
    ('ana@exemplo.com.br',        'Gerente de Produto',          'Produto',      '2025-01-01', '2026-04-01'),
    ('ana@exemplo.com.br',        'Head de Produto',             'Produto',      '2026-04-01', NULL),
    ('bruno@exemplo.com.br',      'Designer de Produto',         'Produto',      '2025-06-01', NULL),
    ('carla@exemplo.com.br',      'Gerente de Produto',          'Produto',      '2025-03-01', NULL),
    ('diego@exemplo.com.br',      'Coordenador de Precificação', 'Precificação', '2025-01-01', NULL),
    ('marcus.vggarcia@gmail.com', 'Analista de Dados',           'Dados',        '2026-01-01', NULL)
  ) AS v(email, job, area, valid_from, valid_to)
  JOIN person p    ON p.email = v.email
  JOIN job_title j ON j.name = v.job
  JOIN org_area a  ON a.name = v.area
ON CONFLICT (person_id, valid_from) DO NOTHING;

-- As decisões das fixtures entraram antes de existir vínculo, então o gatilho
-- não teve o que copiar. Preenche o cargo e a área da data da decisão, sem
-- sobrescrever o que já estiver preenchido.
UPDATE decision d
   SET job_title_at_decision = a.job_title_id,
       org_area_at_decision  = a.org_area_id
  FROM assignment a
 WHERE a.person_id = d.decider_person_id
   AND daterange(a.valid_from, a.valid_to, '[)') @> d.decided_on
   AND d.job_title_at_decision IS NULL
   AND d.org_area_at_decision IS NULL;

-- Indicadores por projeto -----------------------------------------------------
INSERT INTO indicator (project_id, name, unit, direction)
SELECT pr.id, v.name, v.unit, v.direction::metric_direction
  FROM (VALUES
    ('Checkout 2026',         'Conversão do checkout',    '%',   'increase'),
    ('Checkout 2026',         'Pedidos cancelados',       '%',   'decrease'),
    ('Ativação e onboarding', 'Ativação em 7 dias',       '%',   'increase'),
    ('Preços e frete',        'Receita líquida por sessão', 'R$', 'increase'),
    ('Busca interna',         'Buscas sem resultado',     '%',   'decrease')
  ) AS v(project, name, unit, direction)
  JOIN project pr ON pr.name = v.project
ON CONFLICT (project_id, name) DO NOTHING;

-- Medições mensais, jan a set de 2026 -----------------------------------------
-- Os números acompanham a história das fixtures: a conversão do checkout sobe
-- depois de março (remoção da confirmação), as buscas sem resultado caem depois
-- de fevereiro (sinônimos) e a ativação ainda não se mexeu depois de junho.
INSERT INTO measurement (indicator_id, period, value)
SELECT i.id, make_date(2026, v.month, 1), v.value
  FROM (VALUES
    ('Conversão do checkout', 1, 3.10), ('Conversão do checkout', 2, 3.05),
    ('Conversão do checkout', 3, 3.12), ('Conversão do checkout', 4, 3.38),
    ('Conversão do checkout', 5, 3.41), ('Conversão do checkout', 6, 3.36),
    ('Conversão do checkout', 7, 3.44), ('Conversão do checkout', 8, 3.40),
    ('Conversão do checkout', 9, 3.43),
    ('Pedidos cancelados', 1, 2.10), ('Pedidos cancelados', 2, 2.15),
    ('Pedidos cancelados', 3, 2.08), ('Pedidos cancelados', 4, 2.12),
    ('Pedidos cancelados', 5, 2.09), ('Pedidos cancelados', 6, 2.11),
    ('Pedidos cancelados', 7, 2.07), ('Pedidos cancelados', 8, 2.13),
    ('Pedidos cancelados', 9, 2.10),
    ('Ativação em 7 dias', 1, 41.0), ('Ativação em 7 dias', 2, 40.6),
    ('Ativação em 7 dias', 3, 41.2), ('Ativação em 7 dias', 4, 40.9),
    ('Ativação em 7 dias', 5, 41.1), ('Ativação em 7 dias', 6, 40.8),
    ('Ativação em 7 dias', 7, 41.3), ('Ativação em 7 dias', 8, 41.0),
    ('Ativação em 7 dias', 9, 41.2),
    ('Receita líquida por sessão', 1, 1.82), ('Receita líquida por sessão', 2, 1.79),
    ('Receita líquida por sessão', 3, 1.84), ('Receita líquida por sessão', 4, 1.83),
    ('Receita líquida por sessão', 5, 1.86), ('Receita líquida por sessão', 6, 1.85),
    ('Receita líquida por sessão', 7, 1.80), ('Receita líquida por sessão', 8, 1.78),
    ('Receita líquida por sessão', 9, 1.79),
    ('Buscas sem resultado', 1, 12.4), ('Buscas sem resultado', 2, 12.1),
    ('Buscas sem resultado', 3, 9.8),  ('Buscas sem resultado', 4, 9.5),
    ('Buscas sem resultado', 5, 9.6),  ('Buscas sem resultado', 6, 9.3),
    ('Buscas sem resultado', 7, 9.4),  ('Buscas sem resultado', 8, 9.2),
    ('Buscas sem resultado', 9, 9.3)
  ) AS v(indicator, month, value)
  JOIN indicator i ON i.name = v.indicator
ON CONFLICT (indicator_id, period) DO NOTHING;

COMMIT;
