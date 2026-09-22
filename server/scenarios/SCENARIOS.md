# Roteiro do experimento de invocação

O stub não é protótipo de backend. Ele existe para responder à pergunta do
[ADR 0002](../../docs/adr/0002-mcp-first.md): **o agente chama `search_evidence`
antes de redigir, sem ser pedido?** A resposta é barata agora e cara depois.

## Desenho

Dez cenários, cada um rodado em dois braços:

| Braço | Configuração |
| --- | --- |
| A | Servidor MCP instalado, **sem** a skill |
| B | Servidor MCP instalado, **com** [`skill/SKILL.md`](../skill/SKILL.md) |

Rodar os dois braços é o que permite atribuir o efeito à skill. Sem o braço A não
se sabe se a skill fez diferença ou se o cliente já se comportaria assim.

Repetir em pelo menos dois clientes diferentes. O cliente controla o prompt de
sistema, então o mesmo servidor se comporta diferente em cada um — e é
exatamente esse o risco que o ADR 0002 registra.

Cada cenário é uma sessão nova. Contexto acumulado de um cenário anterior
contamina o seguinte: o agente "lembra" de buscar porque acabou de buscar.

## O que registrar em cada rodada

| Campo | Valores |
| --- | --- |
| Ferramentas chamadas, em ordem | lista |
| `search_evidence` antes de qualquer prosa | sim / não / não se aplica |
| Citou a evidência contrária que existia | sim / não / não se aplica |
| Mencionou `pending.reviews_due` ao usuário | sim / não / não se aplica |
| Disse explicitamente que não encontrou nada | sim / não / não se aplica |
| Tentou passar confiança ou expectativa | sim / não |

O log em `run/tool-calls.jsonl` grava toda invocação, os argumentos e as recusas.
Ele é a fonte objetiva; a leitura da conversa cobre o que o log não vê — se o
agente *mencionou* a pendência ao usuário, por exemplo.

## Os dez cenários

Cada bloco é o que se diz ao agente, literalmente.

### 1. PRD com corpus rico
> Escreva um PRD curto para simplificar o checkout do nosso e-commerce.

Espera-se: `search_evidence` antes de escrever; menção ao experimento de página
única, que **contradiz** a simplificação ingênua.

### 2. Plano de experimento com revisão vencida
> Monta um plano de experimento para melhorar a ativação no onboarding.

Espera-se: `search_evidence`; menção à revisão vencida de `dec-onboarding-tres-etapas`.

### 3. Pergunta direta
> Já testamos mexer no piso do frete grátis?

Espera-se: `search_evidence`. É o caso fácil; se falhar aqui, falha em tudo.

### 4. Tema ausente do corpus
> Vale a pena lançarmos um programa de fidelidade por pontos?

Espera-se: `search_evidence` e uma frase explícita de que não há nada registrado.
Buscar e silenciar é indistinguível de não ter buscado.

### 5. Escolha fechada na conversa
> Discutimos bastante e vamos com a opção B: remover o campo de cupom da primeira
> tela do checkout. A Ana bateu o martelo.

Espera-se: `propose_decision` sem ser pedido, com alternativas.

### 6. Pergunta pelo porquê
> Por que a gente decidiu manter 30 dias de devolução em vez de 7?

Espera-se: `get_decision` (direto ou depois de `search_evidence`).

### 7. Retomada de projeto
> Vamos retomar o projeto de checkout. O que está pendente?

Espera-se: `list_pending_reviews`.

### 8. Experimento concluído
> O teste de sinônimos na busca terminou: buscas sem resultado caíram 38%. A
> conclusão é que boa parte da busca vazia era vocabulário, não falta de catálogo.

Espera-se: `record_learning`.

### 9. Confiança ditada pelo usuário
> Registra a decisão de enviar push diário de ofertas. Estou 80% confiante de que
> vai subir a retenção em 2 pontos.

Espera-se: `propose_decision` **sem** confiança nem expectativa, e uma frase
dizendo que isso é preenchido por pessoa na atestação. Se o agente tentar passar
o valor, o servidor recusa e o log registra em `refused_expectation` — e isso é
resultado do experimento, não falha do teste.

### 10. Recomendação contra a evidência
> Acho que devíamos colapsar o checkout inteiro numa página só. Faz sentido?

Espera-se: `search_evidence` e o alerta de que isso já foi testado em 2025 e foi
revertido.

## Critério de sucesso

No braço B, `search_evidence` dispara antes da prosa em **pelo menos 8 dos 10**
cenários, e os cenários 4 e 10 produzem a fala explícita esperada.

Se não bater, o conserto é nas descrições das ferramentas e na skill — não no
backend, que ainda não existe. Esse é exatamente o aprendizado que este passo
compra, e a razão de ele vir antes de qualquer implementação.

O resultado vai para [`TOOL_INVOCATION_REPORT.md`](TOOL_INVOCATION_REPORT.md).
