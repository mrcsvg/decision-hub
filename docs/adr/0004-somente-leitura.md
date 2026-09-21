# ADR 0004 — Somente leitura sobre as fontes

- **Status:** Proposto
- **Data:** 2026-09-21
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível, mas com custo alto de reverter depois de adotado

## Contexto

Um registro que integra plataformas de experimentação é naturalmente tentado a agir sobre elas: encerrar um experimento a partir de uma decisão, marcar o vencedor, criar o próximo teste. Cada uma dessas ações parece conveniente isoladamente.

## Decisão

O sistema nunca escreve em fonte alguma. Ele consome resultados e produz decisões; não orquestra, não encerra e não configura experimentos.

Quando um agente precisar agir sobre uma plataforma, ele usa o servidor MCP daquela plataforma diretamente. Este servidor não intermedeia.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Escrita de volta opcional, desligada por padrão | Uma vez disponível, vira dependência; e exige credenciais de escrita em produção |
| Escrita limitada a metadado (tags, links) | Mesmo problema de credencial, com ganho marginal |

## Evidências

- Instalar uma ferramenta que só lê exige aprovação de segurança muito menor do que uma que escreve em sistema de produção.

## Consequências

- O sistema nunca se torna dependência crítica de produção, o que mantém sua instalação simples.
- Credenciais exigidas pelos adapters são apenas de leitura.
- Fluxos que parecem pedir escrita são resolvidos por composição de servidores MCP no agente.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher*
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
