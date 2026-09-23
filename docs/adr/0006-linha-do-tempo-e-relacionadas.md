# ADR 0006 — Linha do tempo por tema e decisões relacionadas como sétima e oitava ferramentas

- **Status:** Proposto
- **Data:** 2026-09-23
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível

## Contexto

`search_evidence` responde "o que já sabemos sobre X?": devolve evidências, lições e decisões ordenadas por relevância textual. Ela não responde duas perguntas que aparecem justamente quando uma decisão antiga volta à mesa:

- **"Como a nossa posição sobre X mudou?"** A ordem da busca é relevância, não tempo, e revisões de desfecho não aparecem nela. Para reconstruir a trajetória, o agente precisa chamar `get_decision` em cada resultado e ordenar tudo por conta própria, e na prática não faz isso.
- **"O que mais depende desta decisão?"** `get_decision` mostra as evidências e lições de uma decisão, mas não quem mais usou as mesmas. Antes de reverter uma escolha, é isso que falta saber: que outra decisão se apoiou na mesma evidência, às vezes com papel oposto.

A [concepção](../concepcao.md#superfície-de-ferramentas) fixou "seis, no máximo", e o [ADR 0002](0002-mcp-first.md) especificou seis. O limite existe porque cada ferramenta nova disputa a atenção do agente com `search_evidence`, que é a mais importante.

## Decisão

Acrescentar duas ferramentas somente leitura a [`MCP_TOOLS.md`](../../MCP_TOOLS.md), `get_topic_timeline` e `find_related`, e subir o limite para oito. Uma nona exige ADR.

`get_topic_timeline` recebe um tema (texto livre e, se quiser, tags) e devolve em ordem cronológica as decisões, as revisões realizadas e as lições que casam com ele. A seleção usa a mesma regra de relevância de `search_evidence`; só a ordem de apresentação é a data.

`find_related` recebe uma decisão e devolve as outras ligadas a ela por evidência em comum (com o papel da evidência em cada uma), lição em comum ou tag em comum.

Quatro regras valem para as duas:

1. **Somente leitura**, sem parâmetro de confiança ou expectativa, como as outras.
2. **Nenhuma expectativa na saída.** A linha do tempo mostra o veredito das revisões, não a confiança declarada: ela é lida em série, e confiança posta ao lado de desfecho vira comparação informal de calibração, sem o cuidado que a curva de calibração exige.
3. **Nenhuma ordem por efeito** ([ADR 0003](0003-normalizar-afirmacao.md)). A linha do tempo ordena por data. As relacionadas ordenam pela quantidade de vínculos (evidência e lição antes de tag), depois pela data da decisão.
4. **Evidência não vira evento da linha do tempo.** A data de uma evidência é da origem (período do experimento) ou da carga (`imported_at`), e nenhuma delas diz quando a organização mudou de posição. A evidência aparece em `find_related`, pelo vínculo, e em `get_decision`.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Parâmetro `order_by: "date"` em `search_evidence` | Muda o comportamento da ferramenta mais importante e não traz as revisões. Além disso, uma ferramenta com dois modos precisa de uma descrição que cubra os dois gatilhos, e isso enfraquece o principal. |
| Parâmetro `include_related` em `get_decision` | Engorda a resposta mais pesada do servidor, e o agente não tem gatilho para pedir o parâmetro: a descrição de `get_decision` fala do raciocínio de uma decisão, não do entorno. |
| MCP prompt que encadeia `search_evidence` e `get_decision` | Custa uma chamada por decisão, não traz quem compartilha evidência e depende de o cliente expor prompts. |
| Não fazer nada até o experimento de invocação rodar | É a objeção mais forte (ver Evidências). A escolha foi seguir e medir as duas junto com as seis, nos cenários 11 e 12 do roteiro. |

## Evidências

- **Contrária:** o experimento de invocação ([`server/scenarios/TOOL_INVOCATION_REPORT.md`](../../server/scenarios/TOOL_INVOCATION_REPORT.md)) ainda não rodou. Não há medida de que o agente deixe de reconstruir trajetórias com as seis ferramentas, nem de que as duas novas não roubem chamadas de `search_evidence`. O [próximo passo](../concepcao.md#próximos-passos) do roadmap é justamente essa medição.
- O corpus já tem as duas situações. Nas fixtures, o experimento de página única de 2025 contradiz a remoção da confirmação em 2026. No corpus de demonstração, o 3DS obrigatório de 2025 foi revertido para o 3DS só acima de R$ 800, e o retry de cartão de abril voltou em agosto, com idempotência. Com as seis ferramentas, nenhuma dessas trajetórias aparece em uma chamada.
- O modelo de dados já tem o que as duas precisam (`decision.decided_on`, `review.done_on`, `learning.recorded_on`, `decision_evidence`, `decision_learning`, `decision_tag`). Nenhuma tabela nem coluna nova.

## Consequências

- O stub e o servidor real passam a responder oito ferramentas, e os testes de superfície verificam as oito.
- A descrição das duas é escrita para não sobrepor a de `search_evidence`: os gatilhos são "trajetória" e "reverter ou retomar", não "o que sabemos". Se o experimento mostrar sobreposição, o conserto começa pelas descrições e pode terminar na remoção da ferramenta.
- O roteiro do experimento ganha dois cenários (11 e 12), e o critério de sucesso passa a verificar também que `search_evidence` continua disparando nos cenários 1 e 10.
- A skill ganha a regra de consultar a trajetória antes de reverter ou retomar uma decisão.
- "Seis, no máximo" deixa de valer. A concepção registra a mudança e aponta para este ADR.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher*
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
