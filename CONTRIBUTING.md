# Contribuindo

## Mudanças na especificação exigem ADR

O contrato em `spec/` condiciona todos os produtores. Qualquer mudança nele — campo novo, campo removido, regra alterada — vem acompanhada de um ADR em `docs/adr/`, a partir de [`0000-template.md`](docs/adr/0000-template.md).

Campo novo entra como opcional. Tornar obrigatório um campo existente é mudança incompatível e exige nova versão maior do contrato.

## Mudanças no schema do banco

Toda invariante que o banco garante tem teste em `db/test_invariants.sql`. Mudança que altera ou cria invariante altera ou cria o teste correspondente. As invariantes incluem o papel `dm_app` do servidor MCP, por isso `db/grants.sql` roda antes dos testes.

```bash
psql -v ON_ERROR_STOP=1 -d <banco_vazio> -f db/schema.sql -f db/grants.sql -f db/test_invariants.sql
```

## Ferramentas MCP

São seis, e não haverá uma sétima sem ADR. Melhorias na descrição de uma ferramenta são bem-vindas e contam como mudança de produto: explique no PR em que situação o agente deixava de chamá-la.

## Exportadores de plataformas internas

Exportadores para plataformas internas ficam no repositório de quem os escreveu, não aqui. Se o seu exportador não coube em uma tarde de trabalho, abra uma issue: o problema provavelmente está no contrato.

## Registro de decisões

Registre também as alternativas que você descartou e a evidência que contrariou sua proposta. É o que este projeto pede dos outros.
