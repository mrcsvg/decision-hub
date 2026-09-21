# ADR 0003 — Normalizar a afirmação, não a estimativa

- **Status:** Proposto
- **Data:** 2026-09-21
- **Decisor:** *a preencher*
- **Tipo de porta:** irreversível na prática — o contrato publicado condiciona todos os produtores

## Contexto

Plataformas de experimentação usam motores estatísticos distintos: bayesiano ou frequentista, com ou sem CUPED, com ou sem teste sequencial. Definições de lift e de métrica variam por empresa e às vezes por time. Um efeito de 3% medido em duas plataformas não é a mesma grandeza.

A tentação natural de uma camada de consolidação é tornar esses efeitos comparáveis. É um problema sem solução correta, e a tentativa tende a consumir o projeto inteiro.

## Decisão

O contrato de ingestão normaliza **metadado e conclusão** — hipótese, variantes, desfecho, aprendizado, tags — e nunca a estatística.

O efeito, quando enviado, é armazenado como a origem o calculou, com o método declarado, e carrega `comparable_across_sources: false` como constante do contrato. Nenhuma funcionalidade do sistema agrega, compara ou ranqueia efeitos de origens diferentes.

A resposta original da origem é sempre guardada em `raw_payload`, para que uma normalização errada possa ser refeita.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Recalcular efeitos a partir de dados brutos com um motor único | Exige acesso a dados de exposição e evento de cada fonte; vira uma plataforma de experimentação |
| Converter efeitos para uma escala comum por heurística | Produz números com aparência de comparáveis que não são; pior que não comparar |
| Não aceitar efeito algum | Perde a meta-análise legítima dentro de uma mesma origem |

## Evidências

- A diversidade de métodos está documentada nas próprias plataformas; o GrowthBook sozinho oferece motores bayesiano e frequentista.
- O framework ISA, das ciências da vida, resolveu problema análogo padronizando metadado de experimento e deixando o resultado no formato do ensaio.

## Consequências

- Meta-análise é possível apenas dentro de uma mesma origem.
- O sistema fica deliberadamente fora da discussão estatística, que pertence às plataformas.
- Qualquer proposta de comparação entre origens exige um novo ADR que supere este.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher*
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
