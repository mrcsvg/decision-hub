# Stub do servidor MCP

Responde as seis ferramentas de [`MCP_TOOLS.md`](../MCP_TOOLS.md) a partir de
fixtures fictícias. **Não é protótipo de backend.** É o instrumento do item de
roadmap "testar a invocação das ferramentas em cliente real, antes de existir
backend" ([concepção](../docs/concepcao.md#próximos-passos)).

A pergunta que ele existe para responder está no
[ADR 0002](../docs/adr/0002-mcp-first.md): o agente chama `search_evidence`
antes de redigir, sem ser pedido? Responder isso agora custa uma tarde.
Responder depois de construir o backend custa o backend.

## O que ele não faz

- Não tem banco. `db/schema.sql` não é tocado.
- Não persiste escrita. `propose_decision`, `attach_evidence` e `record_learning`
  devolvem um id e gravam uma linha em `run/tool-calls.jsonl`; nada mais.
- Não fala com GrowthBook, ABsmartly nem plataforma alguma
  ([ADR 0004](../docs/adr/0004-somente-leitura.md)).
- Não ranqueia por efeito. A ordem de `search_evidence` é relevância textual
  pura, e há teste para isso ([ADR 0003](../docs/adr/0003-normalizar-afirmacao.md)).
- Não aceita confiança nem expectativa, em parâmetro nenhum. Tentativa é recusada
  com a mensagem de `MCP_TOOLS.md` e registrada no log.

## Rodar

```bash
python -m venv .venv
.venv/bin/pip install -r server/requirements.txt
PYTHONPATH=server .venv/bin/python -m decision_memory_stub
```

Fica falando MCP por stdio. Para instalar num cliente, aponte para o mesmo
comando:

```json
{
  "mcpServers": {
    "decision-memory": {
      "command": "/caminho/para/.venv/bin/python",
      "args": ["-m", "decision_memory_stub"],
      "env": { "PYTHONPATH": "/caminho/para/decision-hub/server" }
    }
  }
}
```

Distribua junto a skill em [`skill/SKILL.md`](skill/SKILL.md): servidor sem skill
é chamado por acidente, e medir os dois braços é o desenho do experimento.

### Variáveis

| Variável | Para quê |
| --- | --- |
| `DECISION_MEMORY_STUB_LOG` | Onde gravar o log de invocações. Padrão `run/tool-calls.jsonl`. |
| `DECISION_MEMORY_STUB_TODAY` | Data de referência, em ISO. Torna as revisões vencidas determinísticas. |
| `DECISION_MEMORY_STUB_FIXTURES` | Outro diretório de fixtures. |

## Testes

```bash
.venv/bin/python -m pytest server/tests -q
```

Cobrem o que as regras do projeto exigem: são seis ferramentas e não sete,
nenhum schema de entrada menciona confiança, a ordem da busca não muda quando os
efeitos mudam, decisão proposta não devolve expectativa, e o bloco `pending`
nunca está ausente.

## O corpus

Fictício, em `fixtures/`: 12 evidências, 6 decisões (uma proposta), 4 lições (uma
proposta) e 5 revisões (duas vencidas). Os registros de experimento validam
contra [`spec/experiment-record-v0.schema.json`](../spec/experiment-record-v0.schema.json),
e a CI reprova se pararem de validar.

Três armadilhas deliberadas, porque um corpus sem elas não testa nada:

- **Evidência contrária.** `ev-checkout-pagina-unica` derrubou a conversão em
  2025. Quem propuser "simplificar o checkout" sem citá-la passou por cima da
  memória.
- **Tema ausente.** Não há nada sobre fidelidade ou assinatura. O agente deve
  dizer que não achou, em voz alta.
- **Registro não atestado.** `dec-push-diario` está `proposed` e sem expectativa.

## Divergências em relação a `MCP_TOOLS.md`

Registradas aqui de propósito, para não virarem divergência silenciosa:

- **Transporte.** `MCP_TOOLS.md` especifica Streamable HTTP com OAuth 2.1. O stub
  usa **stdio, sem autenticação**, para tirar instalação e identidade do caminho
  do experimento. Se o transporte do produto mudar por causa disso, é ADR.
- **Estado da evidência.** `search_evidence` devolve `state` em todo item, mas
  evidência não tem estado no modelo lógico — ele mora em `decision` e `learning`
  no `db/schema.sql` e em `PROCEDENCIA` na concepção. O stub responde `attested`
  para a evidência do corpus, que vem de importação curada. A divergência é real
  e precisa de decisão antes do backend.
- **Procedência.** O stub preenche `provenance` de forma rasa, sem `principal_person_id`
  nem modelo, porque não há OAuth para dizer em nome de quem o agente agiu.

## Depois do experimento

O resultado vai para
[`scenarios/TOOL_INVOCATION_REPORT.md`](scenarios/TOOL_INVOCATION_REPORT.md). Se
ele pedir mudança nas descrições das ferramentas, a mudança é em `MCP_TOOLS.md` —
e melhoria de descrição conta como mudança de produto, conforme o
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

Este diretório é andaime. Se o servidor de verdade nascer, nasce empacotado, com
banco e com OAuth; nada aqui é base para ele além das descrições e do corpus.
