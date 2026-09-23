-- Corpus ampliado para demonstração. Fictício, como as fixtures.
--
-- Rodar depois da carga das fixtures (python -m decision_memory.seed) e de
-- db/demo.sql, como dm_admin. Rodar de novo não duplica nada: os ids são fixos
-- e toda inserção é condicionada.
--
--   psql -v ON_ERROR_STOP=1 -f db/demo.sql -f db/demo_corpus.sql
--
-- O que ele traz para a demonstração:
--   - três projetos novos (Pagamentos, App mobile, Recomendação), com indicadores
--     e medições mensais;
--   - catorze decisões em todos os estados: atestadas com expectativa, propostas
--     por agente (sem expectativa) e uma descartada;
--   - revisões realizadas com os quatro vereditos, vencidas e futuras — o bastante
--     para a curva de calibração (confiança declarada x desfecho);
--   - evidência de todos os tipos e três origens, inclusive contrária, e duas
--     criadas por agente que ainda aguardam atestação;
--   - lições atestadas e uma proposta.
--
-- Nada aqui fala de fidelidade ou programa de pontos, de propósito: é o tema
-- ausente que mostra a busca dizendo "não achei".

BEGIN;

-- Pessoas, cargos, áreas e vínculos --------------------------------------------
INSERT INTO person (name, email) VALUES
    ('Elisa Prado',    'elisa@exemplo.com.br'),
    ('Fábio Lima',     'fabio@exemplo.com.br'),
    ('Gabriela Souza', 'gabriela@exemplo.com.br'),
    ('Heitor Nunes',   'heitor@exemplo.com.br'),
    ('Isabela Rocha',  'isabela@exemplo.com.br'),
    ('João Martins',   'joao@exemplo.com.br')
ON CONFLICT (email) DO NOTHING;

INSERT INTO job_title (name) VALUES
    ('Engenheiro de Software'),
    ('Pesquisadora de UX'),
    ('Head de Dados'),
    ('Coordenador de Risco')
ON CONFLICT (name) DO NOTHING;

INSERT INTO org_area (name, manager_person_id) VALUES
    ('Engenharia', (SELECT id FROM person WHERE email = 'heitor@exemplo.com.br')),
    ('Pesquisa',   (SELECT id FROM person WHERE email = 'isabela@exemplo.com.br')),
    ('Pagamentos', (SELECT id FROM person WHERE email = 'joao@exemplo.com.br'))
ON CONFLICT (name) DO NOTHING;

-- Fábio é promovido em maio: a decisão dele de fevereiro guarda "Analista de
-- Dados" e a de junho, "Head de Dados".
INSERT INTO assignment (person_id, job_title_id, org_area_id, valid_from, valid_to)
SELECT p.id, j.id, a.id, v.valid_from::date, v.valid_to::date
  FROM (VALUES
    ('elisa@exemplo.com.br',    'Designer de Produto',    'Produto',    '2025-05-01', NULL),
    ('fabio@exemplo.com.br',    'Analista de Dados',      'Dados',      '2025-10-01', '2026-05-01'),
    ('fabio@exemplo.com.br',    'Head de Dados',          'Dados',      '2026-05-01', NULL),
    ('gabriela@exemplo.com.br', 'Gerente de Produto',     'Produto',    '2025-08-01', NULL),
    ('heitor@exemplo.com.br',   'Engenheiro de Software', 'Engenharia', '2025-09-01', NULL),
    ('isabela@exemplo.com.br',  'Pesquisadora de UX',     'Pesquisa',   '2026-01-15', NULL),
    ('joao@exemplo.com.br',     'Coordenador de Risco',   'Pagamentos', '2025-06-01', NULL)
  ) AS v(email, job, area, valid_from, valid_to)
  JOIN person p    ON p.email = v.email
  JOIN job_title j ON j.name = v.job
  JOIN org_area a  ON a.name = v.area
ON CONFLICT (person_id, valid_from) DO NOTHING;

-- Projetos, indicadores e medições ---------------------------------------------
INSERT INTO project (id, name, starts_on, status) VALUES
    ('bb000000-0000-4000-8000-000000000001', 'Pagamentos',   '2025-10-01', 'active'),
    ('bb000000-0000-4000-8000-000000000002', 'App mobile',   '2026-01-01', 'active'),
    ('bb000000-0000-4000-8000-000000000003', 'Recomendação', '2026-02-01', 'active')
ON CONFLICT (id) DO NOTHING;

INSERT INTO indicator (project_id, name, unit, direction) VALUES
    ('bb000000-0000-4000-8000-000000000001', 'Aprovação de pagamento', '%', 'increase'),
    ('bb000000-0000-4000-8000-000000000001', 'Chargebacks',            '%', 'decrease'),
    ('bb000000-0000-4000-8000-000000000002', 'Retenção D30',           '%', 'increase'),
    ('bb000000-0000-4000-8000-000000000002', 'Crash rate',             '%', 'decrease'),
    ('bb000000-0000-4000-8000-000000000003', 'CTR de recomendações',   '%', 'increase')
ON CONFLICT (project_id, name) DO NOTHING;

-- A aprovação sobe em março (Pix), os chargebacks caem em abril (3DS), o CTR sobe
-- em abril (filtragem colaborativa) e o crash rate começa a cair em julho (Flutter).
INSERT INTO measurement (indicator_id, period, value)
SELECT i.id, make_date(2026, m.month::int, 1), m.value
  FROM (VALUES
    ('Aprovação de pagamento', ARRAY[82.5, 83.0, 86.9, 87.1, 87.4, 87.0, 86.8, 87.3, 87.2]),
    ('Chargebacks',            ARRAY[0.62, 0.60, 0.58, 0.49, 0.41, 0.40, 0.42, 0.39, 0.40]),
    ('Retenção D30',           ARRAY[18.2, 18.0, 18.4, 18.9, 18.7, 19.0, 18.8, 18.9, 19.1]),
    ('Crash rate',             ARRAY[1.9, 2.1, 2.0, 1.8, 1.9, 2.0, 1.7, 1.5, 1.4]),
    ('CTR de recomendações',   ARRAY[4.1, 4.0, 4.2, 4.9, 5.6, 5.8, 5.7, 5.9, 5.8])
  ) AS v(indicator, series)
  JOIN indicator i ON i.name = v.indicator
 CROSS JOIN LATERAL unnest(v.series) WITH ORDINALITY AS m(value, month)
ON CONFLICT (indicator_id, period) DO NOTHING;

-- Tags ---------------------------------------------------------------------------
INSERT INTO tag (name) VALUES
    ('pagamentos'), ('fraude'), ('design'), ('engenharia'), ('recomendacao'),
    ('checkout'), ('conversao'), ('mobile'), ('onboarding'), ('ativacao'),
    ('pesquisa'), ('frete'), ('precos'), ('margem'), ('busca')
ON CONFLICT (name) DO NOTHING;

-- Evidências ---------------------------------------------------------------------
-- Experimentos de plataforma trazem o registro normalizado pelo contrato
-- (spec/experiment-record-v0.schema.json); o efeito fica como a origem calculou,
-- nunca comparável entre origens (ADR 0003).
INSERT INTO evidence (id, kind, title, summary, url, source_system, external_id, strength,
                      conformance_level, normalized, imported_at)
VALUES
('ee000000-0000-4000-8000-000000000001', 'experiment', 'Pix como primeira opção de pagamento',
 'Colocar o Pix no topo subiu a aprovação de pagamento; o cartão perdeu participação sem queda na conversão.',
 'https://growthbook.exemplo.com.br/experiment/exp_pix_primeiro', 'growthbook', 'exp_pix_primeiro', 'causal', 2,
 '{"spec_version":"0.1.0","conformance_level":2,"source":{"system":"growthbook","external_id":"exp_pix_primeiro","url":"https://growthbook.exemplo.com.br/experiment/exp_pix_primeiro"},"name":"Pix como primeira opção de pagamento","experiment_type":"ab","started_on":"2025-12-01","ended_on":"2026-01-12","variants":[{"key":"control","name":"Cartão primeiro","is_control":true,"allocation":0.5},{"key":"pix","name":"Pix primeiro","is_control":false,"allocation":0.5}],"hypothesis":"Com o Pix em primeiro lugar, mais pedidos são aprovados sem perder conversão.","owner":{"name":"Gabriela Souza","email":"gabriela@exemplo.com.br"},"outcome":"ship","tags":["pagamentos","checkout"],"learning":"A ordem dos meios de pagamento muda a escolha sem custo de conversão.","primary_metric":{"name":"aprovação de pagamento","direction":"increase","unit":"p.p."},"effect":{"point":4.1,"ci_low":2.9,"ci_high":5.3,"ci_level":0.95,"relative":false,"method":"bayesian","comparable_across_sources":false}}',
 now()),
('ee000000-0000-4000-8000-000000000002', 'experiment', '3DS só em pedidos acima de R$ 800',
 'Os chargebacks caíram sem efeito detectável na conversão dos pedidos abaixo do limite.',
 'https://absmartly.exemplo.com.br/experiments/ab_3ds_800', 'absmartly', 'ab_3ds_800', 'causal', 1,
 '{"spec_version":"0.1.0","conformance_level":1,"source":{"system":"absmartly","external_id":"ab_3ds_800"},"name":"3DS só em pedidos acima de R$ 800","started_on":"2026-01-05","ended_on":"2026-02-02","variants":[{"key":"control","name":"Sem 3DS","is_control":true},{"key":"3ds_800","name":"3DS acima de R$ 800","is_control":false}],"hypothesis":"Exigir 3DS só no ticket alto reduz fraude sem fricção no resto.","owner":{"name":"João Martins","email":"joao@exemplo.com.br"},"outcome":"ship","tags":["pagamentos","fraude"],"learning":"Fricção seletiva por valor corta chargeback sem mexer na conversão geral."}',
 now()),
('ee000000-0000-4000-8000-000000000003', 'analysis', 'Cobranças duplicadas após o retry automático',
 'Depois do retry, 0,7% dos pedidos tiveram cobrança duplicada e as reclamações triplicaram na semana.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000004', 'experiment', 'Navegar no app sem cadastro',
 'Mais instalações chegaram à primeira busca; a retenção D30 não teve diferença significativa.',
 NULL, 'internal.lab', 'exp-2026-0031', 'causal', 2,
 '{"spec_version":"0.1.0","conformance_level":2,"source":{"system":"internal.lab","external_id":"exp-2026-0031"},"name":"Navegar no app sem cadastro","experiment_type":"ab","started_on":"2026-03-10","ended_on":"2026-05-20","variants":[{"key":"control","name":"Cadastro na abertura","is_control":true,"allocation":0.5},{"key":"sem_cadastro","name":"Cadastro só na compra","is_control":false,"allocation":0.5}],"hypothesis":"Adiar o cadastro aumenta a retenção de quem instala o app.","owner":{"name":"Gabriela Souza","email":"gabriela@exemplo.com.br"},"outcome":"inconclusive","tags":["mobile","onboarding"],"learning":"Tirar o cadastro da entrada move o topo do funil, não a retenção.","primary_metric":{"name":"retenção D30","direction":"increase","unit":"p.p."},"effect":{"point":0.4,"ci_low":-0.6,"ci_high":1.4,"ci_level":0.95,"relative":false,"method":"frequentist","comparable_across_sources":false}}',
 now()),
('ee000000-0000-4000-8000-000000000005', 'study', 'Doze entrevistas sobre o cadastro no app',
 'O cadastro obrigatório foi citado como motivo de desinstalação por 7 das 12 pessoas.',
 NULL, NULL, NULL, 'anecdotal', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000006', 'analysis', 'Uso do app por faixa horária',
 '43% das sessões acontecem entre 20h e 1h.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000007', 'document', 'Avaliação técnica: Flutter ou nativo',
 'Compara desempenho, custo de manutenção e contratação para os dois caminhos.',
 NULL, NULL, NULL, 'anecdotal', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000008', 'experiment', 'Recomendação por filtragem colaborativa',
 'O CTR das recomendações subiu; o efeito foi maior nas categorias com catálogo grande.',
 'https://growthbook.exemplo.com.br/experiment/exp_reco_colaborativa', 'growthbook', 'exp_reco_colaborativa', 'causal', 2,
 '{"spec_version":"0.1.0","conformance_level":2,"source":{"system":"growthbook","external_id":"exp_reco_colaborativa","url":"https://growthbook.exemplo.com.br/experiment/exp_reco_colaborativa"},"name":"Recomendação por filtragem colaborativa","experiment_type":"ab","started_on":"2026-03-15","ended_on":"2026-04-26","variants":[{"key":"control","name":"Regras por categoria","is_control":true,"allocation":0.5},{"key":"colab","name":"Filtragem colaborativa","is_control":false,"allocation":0.5}],"hypothesis":"Recomendação colaborativa gera mais cliques que regras manuais.","owner":{"name":"Fábio Lima","email":"fabio@exemplo.com.br"},"outcome":"ship","tags":["recomendacao","conversao"],"learning":"Colaborativa ganha das regras quando o catálogo por categoria é grande.","primary_metric":{"name":"CTR de recomendações","direction":"increase","unit":"p.p."},"effect":{"point":1.6,"ci_low":1.1,"ci_high":2.1,"ci_level":0.95,"relative":false,"method":"bayesian","comparable_across_sources":false}}',
 now()),
('ee000000-0000-4000-8000-000000000009', 'analysis', 'CTR de recomendação por posição na página',
 'Recomendações abaixo da dobra têm um terço do CTR das que aparecem acima.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000010', 'external', 'Relatório de mercado sobre busca por voz no varejo',
 'O relatório não encontra ganho de conversão com busca por voz em varejo generalista.',
 'https://relatorio.exemplo.com.br/busca-por-voz-2026', NULL, NULL, 'anecdotal', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000011', 'analysis', 'Participação da busca por voz no app',
 'Só 0,8% das buscas no app usam o microfone do teclado.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000012', 'experiment', 'Frete expresso em São Paulo',
 'Os pedidos com entrega em 24 horas cresceram e a receita por sessão ficou estável.',
 NULL, 'absmartly', 'ab_frete_expresso_sp', 'causal', 1,
 '{"spec_version":"0.1.0","conformance_level":1,"source":{"system":"absmartly","external_id":"ab_frete_expresso_sp"},"name":"Frete expresso em São Paulo","started_on":"2026-06-01","ended_on":"2026-07-20","variants":[{"key":"control","name":"Frete padrão","is_control":true},{"key":"expresso","name":"Opção de frete expresso","is_control":false}],"hypothesis":"Oferecer entrega em 24 horas aumenta a receita por sessão na capital.","owner":{"name":"Diego Alencar","email":"diego@exemplo.com.br"},"outcome":"iterate","tags":["frete","precos"],"learning":"Prazo curto atrai pedido novo; o preço do expresso ainda precisa de ajuste."}',
 now()),
('ee000000-0000-4000-8000-000000000013', 'study', 'Pesquisa com 300 clientes sobre prazo de entrega',
 'Para 58% dos clientes das capitais, o prazo pesa mais que o preço do frete.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000014', 'experiment', 'Parcelamento em 12x contra 6x sem juros',
 'Reduzir para 6x derrubou a conversão nos pedidos acima de R$ 500.',
 'https://growthbook.exemplo.com.br/experiment/exp_parcelamento_12x', 'growthbook', 'exp_parcelamento_12x', 'causal', 2,
 '{"spec_version":"0.1.0","conformance_level":2,"source":{"system":"growthbook","external_id":"exp_parcelamento_12x","url":"https://growthbook.exemplo.com.br/experiment/exp_parcelamento_12x"},"name":"Parcelamento em 12x contra 6x sem juros","experiment_type":"ab","started_on":"2025-09-01","ended_on":"2025-10-15","variants":[{"key":"control","name":"12x sem juros","is_control":true,"allocation":0.5},{"key":"6x","name":"6x sem juros","is_control":false,"allocation":0.5}],"hypothesis":"Reduzir o parcelamento para 6x não afeta a conversão e corta custo financeiro.","owner":{"name":"João Martins","email":"joao@exemplo.com.br"},"outcome":"rollback","tags":["pagamentos","precos"],"learning":"Parcelamento longo sustenta a conversão no ticket alto.","primary_metric":{"name":"conversão acima de R$ 500","direction":"increase","unit":"p.p."},"effect":{"point":-1.2,"ci_low":-1.9,"ci_high":-0.5,"ci_level":0.95,"relative":false,"method":"bayesian","comparable_across_sources":false}}',
 now()),
('ee000000-0000-4000-8000-000000000015', 'analysis', 'Abandono de carrinho por tempo desde a última visita',
 'Metade dos carrinhos abandonados é retomada em até 2 horas; quase nenhum volta depois de 48 horas.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000016', 'experiment', 'Cupom de 20% na primeira compra',
 'A conversão de clientes novos subiu, mas a recompra em 90 dias caiu.',
 NULL, 'internal.lab', 'exp-2025-0412', 'causal', 1,
 '{"spec_version":"0.1.0","conformance_level":1,"source":{"system":"internal.lab","external_id":"exp-2025-0412"},"name":"Cupom de 20% na primeira compra","started_on":"2025-04-01","ended_on":"2025-07-31","variants":[{"key":"control","name":"Sem cupom","is_control":true},{"key":"cupom20","name":"Cupom de 20%","is_control":false}],"hypothesis":"Cupom de boas-vindas converte cliente novo sem prejudicar a recompra.","owner":{"name":"Diego Alencar","email":"diego@exemplo.com.br"},"outcome":"rollback","tags":["precos","margem"],"learning":"Desconto de entrada atrai quem não volta."}',
 now()),
('ee000000-0000-4000-8000-000000000017', 'analysis', 'Margem dos pedidos com cupom de primeira compra',
 'Pedidos com cupom de 20% têm margem negativa em 4 de 6 categorias.',
 NULL, NULL, NULL, 'correlational', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000018', 'study', 'Teste de usabilidade do checkout como convidado',
 '5 de 6 participantes concluíram a compra sem criar conta, contra 3 de 6 no fluxo atual.',
 NULL, NULL, NULL, 'anecdotal', NULL, NULL, NULL),
('ee000000-0000-4000-8000-000000000019', 'experiment', 'Recomendações no carrinho',
 'O ticket médio subiu 3% e a conversão do checkout não teve diferença detectável.',
 'https://growthbook.exemplo.com.br/experiment/exp_reco_carrinho', 'growthbook', 'exp_reco_carrinho', 'causal', 1,
 '{"spec_version":"0.1.0","conformance_level":1,"source":{"system":"growthbook","external_id":"exp_reco_carrinho","url":"https://growthbook.exemplo.com.br/experiment/exp_reco_carrinho"},"name":"Recomendações no carrinho","started_on":"2026-05-02","ended_on":"2026-06-05","variants":[{"key":"control","name":"Carrinho sem recomendações","is_control":true},{"key":"reco","name":"Carrinho com recomendações","is_control":false}],"hypothesis":"Recomendar no carrinho sobe o ticket sem atrapalhar o fechamento.","owner":{"name":"Fábio Lima","email":"fabio@exemplo.com.br"},"outcome":"ship","tags":["recomendacao","checkout"],"learning":"O carrinho aceita recomendação sem custo de conversão."}',
 now())
ON CONFLICT (id) DO NOTHING;

INSERT INTO evidence_tag (evidence_id, tag_id)
SELECT ('ee000000-0000-4000-8000-0000000000' || v.n)::uuid, t.id
  FROM (VALUES
    ('01', 'pagamentos'), ('01', 'checkout'), ('01', 'conversao'),
    ('02', 'pagamentos'), ('02', 'fraude'),
    ('03', 'pagamentos'), ('03', 'engenharia'),
    ('04', 'mobile'), ('04', 'onboarding'), ('04', 'ativacao'),
    ('05', 'mobile'), ('05', 'pesquisa'), ('05', 'onboarding'),
    ('06', 'mobile'), ('06', 'design'),
    ('07', 'mobile'), ('07', 'engenharia'),
    ('08', 'recomendacao'), ('08', 'conversao'),
    ('09', 'recomendacao'),
    ('10', 'busca'), ('10', 'mobile'),
    ('11', 'busca'), ('11', 'mobile'),
    ('12', 'frete'), ('12', 'precos'),
    ('13', 'frete'), ('13', 'pesquisa'),
    ('14', 'pagamentos'), ('14', 'precos'),
    ('15', 'ativacao'), ('15', 'checkout'),
    ('16', 'precos'), ('16', 'margem'),
    ('17', 'margem'), ('17', 'precos'),
    ('18', 'checkout'), ('18', 'pesquisa'),
    ('19', 'recomendacao'), ('19', 'checkout')
  ) AS v(n, tag)
  JOIN tag t ON t.name = v.tag
ON CONFLICT DO NOTHING;

-- Decisões -----------------------------------------------------------------------
-- O gatilho grava o cargo e a área do decisor na data de cada uma.
INSERT INTO decision (id, slug, title, context, description, door, decided_on,
                      decider_person_id, project_id, state)
SELECT ('de000000-0000-4000-8000-0000000000' || v.n)::uuid, v.slug, v.title, v.context,
       v.description, v.door::door_type, v.decided_on::date, p.id, v.project::uuid,
       v.state::record_state
  FROM (VALUES
    ('01', 'pix-primeira-opcao', 'Oferecer o Pix como primeira opção de pagamento',
     'A aprovação de cartão estava em 82% e o custo por transação subindo.',
     'Colocar o Pix no topo da lista de meios de pagamento para todos os clientes.',
     'two_way', '2026-01-20', 'gabriela@exemplo.com.br', 'bb000000-0000-4000-8000-000000000001', 'attested'),
    ('02', '3ds-acima-de-800', 'Exigir 3DS só em pedidos acima de R$ 800',
     'Os chargebacks passaram de 0,6% e a operadora ameaçou reajustar a taxa.',
     'Ativar a autenticação 3DS apenas para pedidos acima de R$ 800.',
     'one_way', '2026-02-10', 'joao@exemplo.com.br', 'bb000000-0000-4000-8000-000000000001', 'attested'),
    ('03', 'retry-cartao-recusado', 'Tentar de novo cartões recusados por timeout',
     'Parte das recusas vinha de timeout da operadora, não do banco emissor.',
     'Repetir automaticamente uma vez a cobrança recusada por timeout.',
     'two_way', '2026-04-02', 'heitor@exemplo.com.br', 'bb000000-0000-4000-8000-000000000001', 'attested'),
    ('04', 'app-sem-cadastro', 'Deixar navegar no app sem cadastro',
     'Metade das instalações não passava da tela de cadastro.',
     'Pedir cadastro só no momento da compra, não na abertura do app.',
     'two_way', '2026-03-05', 'gabriela@exemplo.com.br', 'bb000000-0000-4000-8000-000000000002', 'attested'),
    ('05', 'modo-escuro-app', 'Lançar o modo escuro no app',
     'Modo escuro era o pedido mais frequente nas avaliações da loja.',
     'Oferecer tema escuro, com opção manual e padrão do sistema.',
     'two_way', '2026-05-20', 'elisa@exemplo.com.br', 'bb000000-0000-4000-8000-000000000002', 'attested'),
    ('06', 'app-em-flutter', 'Reescrever o app em Flutter',
     'Dois times mantinham iOS e Android com funcionalidades divergentes e crash rate de 2%.',
     'Migrar os dois apps para uma base única em Flutter até o fim do ano.',
     'one_way', '2026-07-01', 'heitor@exemplo.com.br', 'bb000000-0000-4000-8000-000000000002', 'attested'),
    ('07', 'recomendacao-colaborativa', 'Trocar a recomendação por regras por filtragem colaborativa',
     'As regras manuais não davam conta do catálogo novo de casa e decoração.',
     'Substituir as regras por categoria por um modelo de filtragem colaborativa.',
     'two_way', '2026-02-28', 'fabio@exemplo.com.br', 'bb000000-0000-4000-8000-000000000003', 'attested'),
    ('08', 'recomendacao-no-carrinho', 'Mostrar recomendações no carrinho',
     'O ticket médio estava parado havia três trimestres.',
     'Exibir até quatro recomendações no carrinho, abaixo dos itens.',
     'two_way', '2026-06-10', 'fabio@exemplo.com.br', 'bb000000-0000-4000-8000-000000000003', 'attested'),
    ('09', 'frete-expresso-capitais', 'Oferecer frete expresso nas capitais',
     'Clientes das capitais reclamavam do prazo mais do que do preço.',
     'Oferecer entrega em 24 horas nas capitais, com preço próprio.',
     'two_way', '2026-08-05', 'diego@exemplo.com.br', NULL, 'attested'),
    ('10', 'sem-busca-por-voz-2026', 'Não investir em busca por voz em 2026',
     'A diretoria pediu avaliação de busca por voz depois de um concorrente lançar.',
     'Não desenvolver busca por voz neste ano; reavaliar em 2027.',
     'two_way', '2026-04-15', 'bruno@exemplo.com.br', NULL, 'attested'),
    ('11', 'checkout-como-convidado', 'Permitir checkout como convidado',
     NULL,
     'Deixar concluir a compra sem criar conta, pedindo só e-mail e endereço.',
     'two_way', '2026-09-21', 'ana@exemplo.com.br', NULL, 'proposed'),
    ('12', 'lembrete-carrinho-abandonado', 'Enviar lembrete de carrinho abandonado após 2 horas',
     'Os lembretes saíam depois de 24 horas, quando quase ninguém volta.',
     'Antecipar o primeiro lembrete de carrinho abandonado para 2 horas.',
     'two_way', '2026-09-22', 'carla@exemplo.com.br', NULL, 'proposed'),
    ('13', 'cupom-primeira-compra', 'Dar cupom de 20% na primeira compra',
     'Marketing pediu um incentivo para a campanha de aquisição do segundo semestre.',
     'Oferecer cupom de 20% na primeira compra de todo cliente novo.',
     'two_way', '2026-08-20', 'diego@exemplo.com.br', NULL, 'discarded'),
    ('14', 'parcelamento-12x-sem-juros', 'Manter parcelamento sem juros em até 12x',
     'O financeiro propôs reduzir para 6x para cortar custo de antecipação.',
     'Manter o parcelamento sem juros em até 12x para pedidos acima de R$ 300.',
     'one_way', '2025-11-10', 'joao@exemplo.com.br', 'bb000000-0000-4000-8000-000000000001', 'attested')
  ) AS v(n, slug, title, context, description, door, decided_on, decider, project, state)
  JOIN person p ON p.email = v.decider
ON CONFLICT (id) DO NOTHING;

-- Projetos das fixtures, por nome: frete e busca já existem.
UPDATE decision SET project_id = (SELECT id FROM project WHERE name = 'Preços e frete' LIMIT 1)
 WHERE id IN ('de000000-0000-4000-8000-000000000009', 'de000000-0000-4000-8000-000000000013')
   AND project_id IS NULL;
UPDATE decision SET project_id = (SELECT id FROM project WHERE name = 'Busca interna' LIMIT 1)
 WHERE id = 'de000000-0000-4000-8000-000000000010' AND project_id IS NULL;
UPDATE decision SET project_id = (SELECT id FROM project WHERE name = 'Checkout 2026' LIMIT 1)
 WHERE id = 'de000000-0000-4000-8000-000000000011' AND project_id IS NULL;
UPDATE decision SET project_id = (SELECT id FROM project WHERE name = 'Ativação e onboarding' LIMIT 1)
 WHERE id = 'de000000-0000-4000-8000-000000000012' AND project_id IS NULL;

INSERT INTO decision_tag (decision_id, tag_id)
SELECT ('de000000-0000-4000-8000-0000000000' || v.n)::uuid, t.id
  FROM (VALUES
    ('01', 'pagamentos'), ('01', 'checkout'), ('01', 'conversao'),
    ('02', 'pagamentos'), ('02', 'fraude'),
    ('03', 'pagamentos'), ('03', 'engenharia'),
    ('04', 'mobile'), ('04', 'onboarding'), ('04', 'ativacao'),
    ('05', 'mobile'), ('05', 'design'),
    ('06', 'mobile'), ('06', 'engenharia'),
    ('07', 'recomendacao'), ('07', 'conversao'),
    ('08', 'recomendacao'), ('08', 'checkout'),
    ('09', 'frete'), ('09', 'precos'),
    ('10', 'busca'), ('10', 'mobile'),
    ('11', 'checkout'), ('11', 'conversao'),
    ('12', 'ativacao'), ('12', 'checkout'),
    ('13', 'precos'), ('13', 'margem'),
    ('14', 'pagamentos'), ('14', 'precos')
  ) AS v(n, tag)
  JOIN tag t ON t.name = v.tag
ON CONFLICT DO NOTHING;

INSERT INTO alternative (id, decision_id, description, rejection_reason)
SELECT ('af000000-0000-4000-8000-0000000000' || v.n)::uuid,
       ('de000000-0000-4000-8000-0000000000' || v.d)::uuid, v.description, v.reason
  FROM (VALUES
    ('01', '01', 'Pix com 5% de desconto', 'Custa margem sem evidência de que é o desconto que converte.'),
    ('02', '02', '3DS em todos os pedidos', 'Fricção alta; o piloto de 2025 derrubou a aprovação.'),
    ('03', '03', 'Deixar o cliente tentar de novo', 'Exige que o cliente perceba a recusa, e a maioria abandona.'),
    ('04', '04', 'Cadastro só com e-mail na abertura', 'Continua sendo uma barreira antes de ver os produtos.'),
    ('05', '05', 'Seguir só o tema do sistema', 'O pedido nas avaliações era poder escolher.'),
    ('06', '06', 'Manter os apps nativos e reduzir escopo', 'Dois times para a mesma funcionalidade; o crash rate não caía.'),
    ('07', '06', 'Migrar para React Native', 'A avaliação técnica favoreceu Flutter em desempenho e o time já conhecia Dart.'),
    ('08', '07', 'Manter as regras por categoria', 'Não escalam para o catálogo novo.'),
    ('09', '08', 'Recomendar só na página de produto', 'É no carrinho que o ticket médio pode subir.'),
    ('10', '09', 'Frete expresso em todo o país', 'O custo logístico fora das capitais inviabiliza o preço.'),
    ('11', '10', 'Lançar busca por voz no app', 'Menos de 1% das buscas usam voz e o relatório de mercado não mostra ganho.'),
    ('12', '13', 'Frete grátis na primeira compra', 'Mesmo efeito de conversão com custo menor no teste de 2025.'),
    ('13', '14', 'Reduzir para 6x sem juros', 'O teste mostrou queda de conversão acima de R$ 500.')
  ) AS v(n, d, description, reason)
ON CONFLICT (id) DO NOTHING;

-- Evidência por decisão, com o papel que teve ------------------------------------
INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note)
SELECT ('de000000-0000-4000-8000-0000000000' || v.d)::uuid, v.e::uuid, v.role::evidence_role, v.weight, v.note
  FROM (VALUES
    ('01', 'ee000000-0000-4000-8000-000000000001', 'supports', 0.9, 'Experimento controlado com efeito claro.'),
    ('02', 'ee000000-0000-4000-8000-000000000002', 'supports', 0.8, NULL),
    ('03', 'ee000000-0000-4000-8000-000000000003', 'contradicts', 0.9, 'Levantada depois do lançamento; motivou a reversão.'),
    ('04', 'ee000000-0000-4000-8000-000000000004', 'supports', 0.6, 'Ganho no topo do funil, retenção inconclusiva.'),
    ('04', 'ee000000-0000-4000-8000-000000000005', 'supports', 0.4, 'Explica o porquê, amostra pequena.'),
    ('05', 'ee000000-0000-4000-8000-000000000006', 'supports', 0.5, NULL),
    ('06', 'ee000000-0000-4000-8000-000000000007', 'supports', 0.7, NULL),
    ('07', 'ee000000-0000-4000-8000-000000000008', 'supports', 0.9, NULL),
    ('07', 'ee000000-0000-4000-8000-000000000009', 'discarded', 0.2, 'Posição na página não era a pergunta.'),
    ('08', 'ee000000-0000-4000-8000-000000000019', 'supports', 0.7, NULL),
    ('08', 'ee000000-0000-4000-8000-000000000009', 'supports', 0.3, 'Por isso as recomendações ficam logo abaixo dos itens.'),
    ('09', 'ee000000-0000-4000-8000-000000000012', 'supports', 0.7, NULL),
    ('09', 'ee000000-0000-4000-8000-000000000013', 'supports', 0.5, NULL),
    ('10', 'ee000000-0000-4000-8000-000000000010', 'supports', 0.4, 'Fonte externa, sem dado nosso.'),
    ('10', 'ee000000-0000-4000-8000-000000000011', 'supports', 0.8, NULL),
    ('11', 'ee000000-0000-4000-8000-000000000018', 'supports', 0.5, NULL),
    ('12', 'ee000000-0000-4000-8000-000000000015', 'supports', 0.6, NULL),
    ('13', 'ee000000-0000-4000-8000-000000000016', 'contradicts', 0.8, 'A recompra caiu no teste de 2025.'),
    ('13', 'ee000000-0000-4000-8000-000000000017', 'contradicts', 0.7, NULL),
    ('14', 'ee000000-0000-4000-8000-000000000014', 'supports', 0.9, NULL)
  ) AS v(d, e, role, weight, note)
ON CONFLICT DO NOTHING;

-- A proposta de checkout como convidado esbarra no experimento de página única
-- das fixtures: evidência contrária que a demonstração deve mostrar.
INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note)
SELECT 'de000000-0000-4000-8000-000000000011', e.id, 'contradicts', 0.4,
       'Mexer na estrutura do checkout já saiu caro uma vez.'
  FROM evidence e
 WHERE e.source_system = 'internal.lab' AND e.external_id = 'exp-2025-0311'
ON CONFLICT DO NOTHING;

-- Expectativas, antes das revisões (o banco recusa expectativa depois do desfecho).
-- As propostas (11, 12) e a descartada (13) não têm: expectativa é da pessoa, na atestação.
INSERT INTO expectation (decision_id, recorded_at, recorded_by, confidence,
                         expected_metric, expected_magnitude, due_on)
SELECT d.id, (d.decided_on + time '10:00') AT TIME ZONE 'America/Sao_Paulo',
       d.decider_person_id, v.confidence, v.metric, v.magnitude, v.due_on::date
  FROM (VALUES
    ('01', 0.80, 'aprovação de pagamento',    '+3 p.p.',          '2026-04-20'),
    ('02', 0.60, 'chargebacks',               '-0,2 p.p.',        '2026-05-10'),
    ('03', 0.50, 'aprovação de pagamento',    '+0,5 p.p.',        '2026-07-02'),
    ('04', 0.70, 'retenção D30',              '+2 p.p.',          '2026-06-05'),
    ('05', 0.40, 'retenção D30',              '+0,5 p.p.',        '2026-09-01'),
    ('06', 0.55, 'crash rate',                '-30%',             '2027-01-10'),
    ('07', 0.65, 'CTR de recomendações',      '+1 p.p.',          '2026-05-31'),
    ('08', 0.50, 'conversão do checkout',     'sem queda',        '2026-09-10'),
    ('09', 0.70, 'receita líquida por sessão', '+R$ 0,05',        '2026-11-05'),
    ('10', 0.75, 'buscas sem resultado',      'sem mudança',      '2026-10-15'),
    ('14', 0.85, 'conversão geral',           'sem queda',        '2026-02-10')
  ) AS v(n, confidence, metric, magnitude, due_on)
  JOIN decision d ON d.id = ('de000000-0000-4000-8000-0000000000' || v.n)::uuid
 WHERE NOT EXISTS (SELECT 1 FROM expectation x WHERE x.decision_id = d.id);

-- Revisões: realizadas com os quatro vereditos, vencidas e futuras. Hoje, na
-- história da demonstração, é setembro de 2026.
INSERT INTO review (id, decision_id, due_on, done_on, verdict, notes, reviewed_by)
SELECT ('ec000000-0000-4000-8000-0000000000' || v.n)::uuid, d.id, v.due_on::date,
       v.done_on::date, v.verdict::review_verdict, v.notes,
       CASE WHEN v.done_on IS NULL THEN NULL ELSE d.decider_person_id END
  FROM (VALUES
    ('01', '01', '2026-04-20', '2026-04-22', 'better',       'A aprovação subiu 4,1 p.p. e o Pix virou 38% dos pedidos.'),
    ('02', '02', '2026-05-10', '2026-05-15', 'as_expected',  'Os chargebacks caíram 0,2 p.p. sem efeito na conversão.'),
    ('03', '03', '2026-07-02', '2026-07-10', 'worse',        'O retry gerou cobranças duplicadas; revertido em 8 de julho.'),
    ('04', '04', '2026-06-05', '2026-06-12', 'inconclusive', 'A retenção D30 oscilou dentro do ruído.'),
    ('05', '05', '2026-09-01', NULL,         NULL,           NULL),
    ('06', '06', '2027-01-10', NULL,         NULL,           NULL),
    ('07', '07', '2026-05-31', '2026-06-03', 'better',       'O CTR subiu 1,6 p.p., acima do esperado.'),
    ('08', '08', '2026-09-10', NULL,         NULL,           NULL),
    ('09', '09', '2026-11-05', NULL,         NULL,           NULL),
    ('10', '10', '2026-10-15', NULL,         NULL,           NULL),
    ('11', '14', '2026-02-10', '2026-02-12', 'as_expected',  'Conversão estável; custo financeiro dentro do previsto.')
  ) AS v(n, d, due_on, done_on, verdict, notes)
  JOIN decision d ON d.id = ('de000000-0000-4000-8000-0000000000' || v.d)::uuid
ON CONFLICT (id) DO NOTHING;

-- Lições ---------------------------------------------------------------------------
INSERT INTO learning (id, summary, recorded_on, state) VALUES
('ea000000-0000-4000-8000-000000000001',
 'Colocar o meio de pagamento mais barato em primeiro lugar muda a escolha sem custo de conversão.', '2026-04-22', 'attested'),
('ea000000-0000-4000-8000-000000000002',
 'Automação que repete cobrança precisa de idempotência na operadora antes de ir para produção.', '2026-07-10', 'attested'),
('ea000000-0000-4000-8000-000000000003',
 'Tirar a barreira de entrada do app aumenta o topo do funil, mas não move a retenção sozinho.', '2026-06-12', 'attested'),
('ea000000-0000-4000-8000-000000000004',
 'Recomendação colaborativa ganha das regras quando o catálogo por categoria passa de alguns milhares de itens.', '2026-06-03', 'attested'),
('ea000000-0000-4000-8000-000000000005',
 'Desconto de entrada atrai quem não volta: medir a recompra, não só a conversão.', '2026-08-20', 'attested'),
('ea000000-0000-4000-8000-000000000006',
 'Nas capitais, o prazo de entrega pesa mais que o preço do frete.', '2026-08-05', 'attested'),
('ea000000-0000-4000-8000-000000000007',
 'Carrinho abandonado se recupera nas primeiras horas ou não se recupera mais.', '2026-09-22', 'proposed'),
('ea000000-0000-4000-8000-000000000008',
 'Parcelamento longo sustenta a conversão no ticket alto.', '2025-11-10', 'attested')
ON CONFLICT (id) DO NOTHING;

INSERT INTO decision_learning (decision_id, learning_id)
SELECT ('de000000-0000-4000-8000-0000000000' || v.d)::uuid, ('ea000000-0000-4000-8000-0000000000' || v.l)::uuid
  FROM (VALUES ('01','01'), ('03','02'), ('04','03'), ('07','04'), ('13','05'),
               ('09','06'), ('12','07'), ('14','08')) AS v(d, l)
ON CONFLICT DO NOTHING;

INSERT INTO evidence_learning (evidence_id, learning_id)
SELECT ('ee000000-0000-4000-8000-0000000000' || v.e)::uuid, ('ea000000-0000-4000-8000-0000000000' || v.l)::uuid
  FROM (VALUES ('01','01'), ('03','02'), ('04','03'), ('05','03'), ('08','04'),
               ('16','05'), ('17','05'), ('12','06'), ('13','06'), ('15','07'),
               ('14','08')) AS v(e, l)
ON CONFLICT DO NOTHING;

INSERT INTO learning_tag (learning_id, tag_id)
SELECT ('ea000000-0000-4000-8000-0000000000' || v.l)::uuid, t.id
  FROM (VALUES
    ('01', 'pagamentos'), ('01', 'checkout'),
    ('02', 'pagamentos'), ('02', 'engenharia'),
    ('03', 'mobile'), ('03', 'onboarding'), ('03', 'ativacao'),
    ('04', 'recomendacao'),
    ('05', 'precos'), ('05', 'margem'),
    ('06', 'frete'),
    ('07', 'ativacao'), ('07', 'checkout'),
    ('08', 'pagamentos'), ('08', 'precos')
  ) AS v(l, tag)
  JOIN tag t ON t.name = v.tag
ON CONFLICT DO NOTHING;

-- Procedência ------------------------------------------------------------------------
-- Pessoas criaram e atestaram quase tudo. O agente, em nome de Marcus, criou as
-- duas propostas (11, 12), duas evidências (15, 18) e uma lição (07): nascem sem
-- atestação e aparecem como pendentes.
INSERT INTO provenance (object_type, object_id, author_kind, principal_person_id, model,
                        source_ref, attested_by, attested_at)
SELECT v.object_type, v.object_id::uuid, v.author_kind::author_kind, pr.id,
       CASE WHEN v.author_kind = 'agent' THEN 'unknown' END,
       CASE WHEN v.author_kind = 'agent' THEN 'mcp; client=claude-code (demonstração)'
            ELSE 'demo' END,
       at.id, CASE WHEN at.id IS NULL THEN NULL ELSE v.attested_at::timestamptz END
  FROM (VALUES
    -- decisões: quem decidiu criou e atestou; a descartada não tem atestação
    ('decision', 'de000000-0000-4000-8000-000000000001', 'human', 'gabriela@exemplo.com.br', 'gabriela@exemplo.com.br', '2026-01-20 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000002', 'human', 'joao@exemplo.com.br',     'joao@exemplo.com.br',     '2026-02-10 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000003', 'human', 'heitor@exemplo.com.br',   'heitor@exemplo.com.br',   '2026-04-02 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000004', 'human', 'gabriela@exemplo.com.br', 'gabriela@exemplo.com.br', '2026-03-05 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000005', 'human', 'elisa@exemplo.com.br',    'elisa@exemplo.com.br',    '2026-05-20 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000006', 'human', 'heitor@exemplo.com.br',   'heitor@exemplo.com.br',   '2026-07-01 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000007', 'human', 'fabio@exemplo.com.br',    'fabio@exemplo.com.br',    '2026-02-28 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000008', 'human', 'fabio@exemplo.com.br',    'fabio@exemplo.com.br',    '2026-06-10 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000009', 'human', 'diego@exemplo.com.br',    'diego@exemplo.com.br',    '2026-08-05 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000010', 'human', 'bruno@exemplo.com.br',    'bruno@exemplo.com.br',    '2026-04-15 10:00-03'),
    ('decision', 'de000000-0000-4000-8000-000000000011', 'agent', 'marcus.vggarcia@gmail.com', NULL, NULL),
    ('decision', 'de000000-0000-4000-8000-000000000012', 'agent', 'marcus.vggarcia@gmail.com', NULL, NULL),
    ('decision', 'de000000-0000-4000-8000-000000000013', 'human', 'diego@exemplo.com.br',    NULL, NULL),
    ('decision', 'de000000-0000-4000-8000-000000000014', 'human', 'joao@exemplo.com.br',     'joao@exemplo.com.br',     '2025-11-10 10:00-03'),
    -- evidências: importadas e atestadas por Fábio, menos as duas do agente
    ('evidence', 'ee000000-0000-4000-8000-000000000001', 'import', NULL, 'fabio@exemplo.com.br', '2026-01-13 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000002', 'import', NULL, 'fabio@exemplo.com.br', '2026-02-03 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000003', 'human', 'heitor@exemplo.com.br', 'fabio@exemplo.com.br', '2026-07-09 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000004', 'import', NULL, 'fabio@exemplo.com.br', '2026-05-21 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000005', 'human', 'isabela@exemplo.com.br', 'isabela@exemplo.com.br', '2026-02-20 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000006', 'human', 'fabio@exemplo.com.br', 'fabio@exemplo.com.br', '2026-05-10 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000007', 'human', 'heitor@exemplo.com.br', 'heitor@exemplo.com.br', '2026-06-20 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000008', 'import', NULL, 'fabio@exemplo.com.br', '2026-04-27 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000009', 'human', 'fabio@exemplo.com.br', 'fabio@exemplo.com.br', '2026-02-15 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000010', 'human', 'bruno@exemplo.com.br', 'bruno@exemplo.com.br', '2026-04-10 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000011', 'human', 'fabio@exemplo.com.br', 'fabio@exemplo.com.br', '2026-04-10 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000012', 'import', NULL, 'fabio@exemplo.com.br', '2026-07-21 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000013', 'human', 'isabela@exemplo.com.br', 'isabela@exemplo.com.br', '2026-07-01 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000014', 'import', NULL, 'fabio@exemplo.com.br', '2025-10-16 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000015', 'agent', 'marcus.vggarcia@gmail.com', NULL, NULL),
    ('evidence', 'ee000000-0000-4000-8000-000000000016', 'import', NULL, 'fabio@exemplo.com.br', '2025-08-01 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000017', 'human', 'diego@exemplo.com.br', 'diego@exemplo.com.br', '2026-08-18 09:00-03'),
    ('evidence', 'ee000000-0000-4000-8000-000000000018', 'agent', 'marcus.vggarcia@gmail.com', NULL, NULL),
    ('evidence', 'ee000000-0000-4000-8000-000000000019', 'import', NULL, 'fabio@exemplo.com.br', '2026-06-06 09:00-03'),
    -- lições: atestadas por quem decidiu; a 07 é do agente
    ('learning', 'ea000000-0000-4000-8000-000000000001', 'human', 'gabriela@exemplo.com.br', 'gabriela@exemplo.com.br', '2026-04-22 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000002', 'human', 'heitor@exemplo.com.br',   'joao@exemplo.com.br',     '2026-07-10 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000003', 'human', 'gabriela@exemplo.com.br', 'gabriela@exemplo.com.br', '2026-06-12 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000004', 'human', 'fabio@exemplo.com.br',    'fabio@exemplo.com.br',    '2026-06-03 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000005', 'human', 'diego@exemplo.com.br',    'diego@exemplo.com.br',    '2026-08-20 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000006', 'human', 'isabela@exemplo.com.br',  'diego@exemplo.com.br',    '2026-08-05 10:00-03'),
    ('learning', 'ea000000-0000-4000-8000-000000000007', 'agent', 'marcus.vggarcia@gmail.com', NULL, NULL),
    ('learning', 'ea000000-0000-4000-8000-000000000008', 'human', 'joao@exemplo.com.br',     'joao@exemplo.com.br',     '2025-11-10 10:00-03')
  ) AS v(object_type, object_id, author_kind, principal, attester, attested_at)
  LEFT JOIN person pr ON pr.email = v.principal
  LEFT JOIN person at ON at.email = v.attester
 WHERE NOT EXISTS (SELECT 1 FROM provenance p
                    WHERE p.object_type = v.object_type AND p.object_id = v.object_id::uuid
                      AND p.source_ref IN ('demo', 'mcp; client=claude-code (demonstração)'));

-- A proposta de checkout como convidado veio com chave de idempotência.
INSERT INTO idempotency_key (principal_person_id, key, object_type, object_id)
SELECT p.id, 'demo-checkout-convidado', 'decision', 'de000000-0000-4000-8000-000000000011'
  FROM person p WHERE p.email = 'marcus.vggarcia@gmail.com'
ON CONFLICT DO NOTHING;

COMMIT;
