# Contrato de ingestão — `experiment-record` v0

Formato pelo qual qualquer plataforma de experimentação — comercial ou interna — entrega resultados ao registro. JSON Schema 2020-12.

O contrato normaliza **metadado e conclusão**, nunca estatística. Um efeito de 3% medido em duas plataformas diferentes não é a mesma grandeza, e o contrato torna isso explícito: todo `effect` carrega `comparable_across_sources: false`. Racional em [ADR 0003](../docs/adr/0003-normalizar-afirmacao.md).

## Níveis de conformidade

O produtor declara o nível em `conformance_level`. Só os campos de L0 são obrigatórios para todos.

| Nível | Campos exigidos | O que habilita |
| --- | --- | --- |
| 0 | `source`, `name`, `started_on`, `variants` | Busca e resposta a "isso já foi testado?" |
| 1 | + `hypothesis`, `owner`, `outcome`, `tags`, `learning` | Corpo de conhecimento consultável |
| 2 | + `primary_metric`, `effect` | Meta-análise dentro de uma mesma origem |

Regras adicionais:

- Exatamente uma variante com `is_control: true`.
- `outcome` final (`ship`, `rollback`, `inconclusive`, `iterate`) exige `ended_on`.
- Em L1 ou acima, `outcome` final exige `learning` preenchido. Com `outcome: running`, `learning` pode ser nulo.
- `(source.system, source.external_id)` é a chave de idempotência: reenviar o mesmo par atualiza, não duplica.
- Campos fora do contrato são rejeitados. Use `extensions` para o que o contrato não modela e `raw_payload` para a resposta original.

## Validar

Qualquer validador de JSON Schema 2020-12 serve. Com Python:

```bash
pip install jsonschema
python -c "
import json, sys
from jsonschema import Draft202012Validator as V
s = json.load(open('spec/experiment-record-v0.schema.json'))
V(s, format_checker=V.FORMAT_CHECKER).validate(json.load(open(sys.argv[1])))
print('ok')
" spec/examples/l2-full.json
```

Com Node:

```bash
npx ajv-cli validate --spec=draft2020 -c ajv-formats \
  -s spec/experiment-record-v0.schema.json -d spec/examples/l2-full.json
```

## Exemplos

- [`examples/l0-minimal.json`](examples/l0-minimal.json) — o mínimo que uma plataforma interna consegue enviar no primeiro dia
- [`examples/l2-full.json`](examples/l2-full.json) — registro completo, como sairia de um adapter do GrowthBook

## Teste de qualidade do contrato

Um exportador de plataforma interna precisa caber em uma tarde de trabalho. Se não couber, o problema está neste arquivo, não no exportador.
