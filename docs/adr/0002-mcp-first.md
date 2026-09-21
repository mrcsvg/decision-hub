# ADR 0002 — Interface MCP-first

- **Status:** Proposto
- **Data:** 2026-09-21
- **Decisor:** *a preencher*
- **Tipo de porta:** reversível

## Contexto

Registros de decisão morrem pelo custo de escrita: quem acabou de fechar uma escolha difícil não quer abrir um formulário. E morrem pelo abandono da leitura: um repositório que ninguém consulta não gera incentivo para ninguém alimentar.

Uma parcela crescente do trabalho de produto — redigir proposta, revisar PR, resumir reunião — passa por agentes. Esses agentes já estão no ponto exato onde a decisão é tomada.

## Decisão

A interface primária do registro é um servidor MCP com seis ferramentas, especificadas em [`MCP_TOOLS.md`](../../MCP_TOOLS.md). Não há frontend no primeiro ciclo.

Três regras acompanham a decisão:

1. **Registro criado por agente nasce `proposed`** e só vira `attested` com confirmação humana, fora do MCP.
2. **Nenhuma ferramenta escreve expectativa.** Confiança e resultado esperado são digitados por pessoa na atestação. Uma confiança estimada por agente destrói a curva de calibração, que é a medida de longo prazo que justifica o sistema.
3. **O servidor é distribuído junto com uma skill** que carrega a regra "consulte antes de propor". O servidor não controla quando é chamado; a skill sim.

Perfis que produzem evidência qualitativa — gestão de produto e pesquisa — raramente trabalham em clientes de agente. Para eles haverá uma superfície Slack sobre o mesmo backend.

## Alternativas consideradas

| Alternativa | Por que foi descartada |
| --- | --- |
| Aplicação web como interface primária | Mantém o custo de escrita que mata esse tipo de sistema |
| MCP como interface única | Exclui quem produz a evidência qualitativa |
| Permitir que o agente registre confiança sugerida pelo usuário | Inverificável: não há como distinguir confiança citada de confiança inventada |

## Evidências

- Servidores MCP de decision log já existem para decisões técnicas, locais e monousuário; validam a interface, não o recorte de produto.
- Implementações públicas do padrão de lembrete embutido na resposta das ferramentas relatam aumento expressivo na taxa de fechamento do loop — com a maioria dos loops ainda vazando, o que exige camada assíncrona de cobrança.

## Consequências

- A descrição de cada ferramenta vira trabalho de produto, não documentação.
- A camada assíncrona de cobrança de revisão (Slack, e-mail ou tarefa agendada) é obrigatória; MCP é pull e não cobra ninguém.
- A atestação precisa de uma superfície humana mínima desde o primeiro ciclo.

## Expectativa

*Preenchida pelo decisor antes do aceite. Nunca por agente.*

- **Resultado esperado:** *a preencher* (sugestão de métrica: proporção de decisões registradas sem abertura de formulário)
- **Confiança:** *a preencher*

## Revisão

- **Data prevista:** *a definir*
