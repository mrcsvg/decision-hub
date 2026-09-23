---
name: decision-memory
description: Use antes de redigir PRD, proposta, plano de experimento ou recomendação de produto,
  e sempre que uma decisão de produto for tomada na conversa.
---

# decision-memory

O servidor não controla quando é chamado; quem controla é o cliente. Esta skill
carrega a regra de uso, e é por isso que ela é distribuída junto do servidor
([ADR 0002](../../docs/adr/0002-mcp-first.md)).

## Antes de escrever

Antes de redigir qualquer proposta, PRD, plano de experimento ou recomendação de
produto, chame `search_evidence` com o tema e cite o que já foi testado, decidido
ou aprendido.

Se não encontrar nada, **diga isso explicitamente** ao usuário. Silêncio depois
de uma busca vazia é indistinguível de não ter buscado.

Se a busca trouxer evidência que **contradiz** o caminho proposto, traga essa
evidência para o usuário antes de recomendar. Evidência contrária registrada é o
que distingue memória de justificativa.

## Antes de reverter ou retomar

Quando a conversa for sobre voltar a um tema já decidido — reverter, retomar,
"já mudamos de ideia sobre isso?" —, chame `get_topic_timeline` com o tema e
conte a trajetória em ordem: o que se decidiu, o que a revisão mostrou, o que se
aprendeu. Antes de propor mudar uma decisão específica, chame `find_related` com
ela e diga ao usuário quais outras decisões se apoiam na mesma evidência,
principalmente quando a evidência teve papel oposto nas duas.

## Quando a escolha fecha

Quando uma decisão for fechada na conversa — "vamos com a opção B", "decidimos
não lançar", "mantemos o fluxo atual" — chame `propose_decision` com as
alternativas consideradas e o motivo de cada descarte.

O registro nasce como proposta e será atestado por uma pessoa.

## O que nunca fazer

Não estime confiança nem resultado esperado. Não passe esses valores em
parâmetro algum, sob nenhum nome. Isso é preenchido por pessoa no momento da
atestação; confiança estimada por agente transforma a curva de calibração em
ruído, que é a medida de longo prazo que justifica o sistema.

Não compare, agregue nem ranqueie efeitos de fontes diferentes: um efeito de 3%
medido em duas plataformas não é a mesma grandeza
([ADR 0003](../../docs/adr/0003-normalizar-afirmacao.md)).

## Pendências

Se a resposta trouxer `pending.reviews_due`, mencione ao usuário antes de
prosseguir. Se trouxer `pending.on_this_record`, pergunte o que falta enquanto o
raciocínio ainda está fresco.
