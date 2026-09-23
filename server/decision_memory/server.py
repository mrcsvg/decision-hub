"""As seis ferramentas de MCP_TOOLS.md sobre o Postgres.

As descrições são cópia literal de MCP_TOOLS.md, e há teste para isso: é a
descrição que faz o agente chamar a ferramenta na hora certa.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

import mcp.types as t
import psycopg
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from psycopg_pool import ConnectionPool
from pydantic import Field

from . import guard, identity, models, pending, reads, writes

log = logging.getLogger("decision_memory")
T = TypeVar("T")

READ = t.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = dict(read_only_hint=False, destructive_hint=False, open_world_hint=False)

DESCRIPTIONS = {
    "search_evidence": (
        "Chame ANTES de redigir proposta, PRD, plano de experimento ou qualquer recomendação "
        "de produto, para saber o que a organização já testou, decidiu ou aprendeu sobre o "
        'tema. Chame também sempre que alguém perguntar "já testamos isso?" ou "por que '
        'decidimos X?". Retorna evidências, lições e decisões, com a força de cada evidência.'
    ),
    "get_decision": (
        "Chame quando precisar do raciocínio completo por trás de uma decisão específica: "
        "contexto, alternativas descartadas e por quê, evidências que sustentaram ou "
        "contradisseram, lições derivadas e revisões realizadas."
    ),
    "propose_decision": (
        'Chame quando uma escolha de produto for fechada na conversa — "vamos com a opção B", '
        '"decidimos não lançar", "mantemos o fluxo atual" — para que ela entre no registro com '
        "contexto e alternativas enquanto o raciocínio ainda está fresco. O registro nasce como "
        "proposta e será confirmado por uma pessoa."
    ),
    "attach_evidence": (
        "Chame quando uma evidência — resultado de experimento, estudo, análise, entrevista ou "
        "documento — tiver pesado numa decisão, seja a favor ou contra. Registre também a "
        "evidência que contradisse a escolha: evidência contrária registrada é o que distingue "
        "memória de justificativa."
    ),
    "record_learning": (
        "Chame quando um experimento terminar, uma decisão for revisada ou alguém formular uma "
        'conclusão que valha para além do caso atual — "usuários preferem X quando Y". Escreva '
        "a lição como afirmação reutilizável, não como resumo do caso."
    ),
    "list_pending_reviews": (
        "Chame no início de uma sessão de planejamento, ao retomar um projeto ou quando alguém "
        "perguntar o que está pendente: lista decisões cuja revisão de desfecho venceu ou está "
        "próxima, e registros propostos aguardando atestação."
    ),
}

NOTHING_FOUND = (
    "Nada encontrado no registro sobre esse tema. Diga isso explicitamente ao usuário em vez "
    "de seguir como se não houvesse consultado."
)


# Consulta ecoada no resumo em texto; o `structured_content` leva a íntegra.
MAX_ECHO = 120


def _echo(query: str) -> str:
    return query if len(query) <= MAX_ECHO else query[:MAX_ECHO] + "…"


def _text(*parts: str) -> list[t.TextContent]:
    return [t.TextContent(type="text", text=" ".join(p for p in parts if p))]


def _pending_sentence(block: models.Pending) -> str:
    bits = []
    if block.reviews_due:
        nearest = block.reviews_due[0]
        bits.append(f"Revisão vencida há {nearest.overdue_days} dias: {nearest.title}.")
    if block.on_this_record:
        bits.append("Falta: " + "; ".join(block.on_this_record) + ".")
    return " ".join(bits)


def _result(summary: str, body: Any, block: models.Pending) -> t.CallToolResult:
    return t.CallToolResult(content=_text(summary, _pending_sentence(block)),
                            structured_content=body.model_dump(mode="json"))


def with_db(pool: ConnectionPool, work: Callable[[psycopg.Connection], T]) -> T:
    """Uma conexão, uma transação. Erro de banco não vaza SQL para o agente."""
    try:
        with pool.connection() as conn:
            return work(conn)
    except ToolError:
        raise
    except Exception as exc:
        # Qualquer outra falha, de banco ou não, sai com a mesma mensagem: o
        # detalhe (SQL, traceback) fica no log, achável pela ref.
        ref = uuid.uuid4().hex[:8]
        bug = not isinstance(exc, psycopg.OperationalError)
        log.error(json.dumps({"event": "db_error" if isinstance(exc, psycopg.Error)
                              else "internal_error",
                              "ref": ref, "bug": bug, "type": type(exc).__name__,
                              "sqlstate": getattr(exc, "sqlstate", None),
                              "error": str(exc)}))
        raise ToolError(
            f"Erro interno ao acessar o registro (ref {ref}). Nada foi gravado. "
            "Tente de novo; se persistir, avise quem administra o servidor."
        ) from None


def build_server(pool: ConnectionPool, audiences: tuple[str, ...] = ()) -> MCPServer:
    server = MCPServer(
        name="decision-memory",
        version="0.2.0",
        instructions=(
            "Registro de decisões e aprendizagens de produto. Consulte antes de redigir "
            "proposta, PRD ou plano de experimento, e registre a decisão quando ela for "
            "fechada na conversa. Confiança e resultado esperado são preenchidos por "
            "pessoa na atestação, nunca por agente."
        ),
    )

    @server.tool(name="search_evidence", description=DESCRIPTIONS["search_evidence"],
                 annotations=READ)
    def search_evidence(
        query: str,
        tags: list[str] | None = None,
        kinds: list[str] | None = None,
        source_system: str | None = None,
        include: list[str] | None = None,
        limit: int = 10,
    ) -> Annotated[t.CallToolResult, models.SearchResponse]:
        def work(conn):
            items, total = reads.search(conn, query, tags, kinds, source_system, include, limit)
            hit_tags = {tag for item in items for tag in item.tags}
            block = pending.build(conn, context_tags=hit_tags or set(reads.terms(query)))
            return items, total, block

        items, total, block = with_db(pool, work)
        body = models.SearchResponse(
            data=models.SearchData(query=query, items=items, total=total,
                                   note=None if items else NOTHING_FOUND),
            pending=block)
        summary = (f"{len(items)} de {total} resultados para '{_echo(query)}'." if items
                   else f"Nenhum resultado para '{_echo(query)}'.")
        return _result(summary, body, block)

    @server.tool(name="get_decision", description=DESCRIPTIONS["get_decision"], annotations=READ)
    def get_decision(
        id: str | None = None, slug: str | None = None
    ) -> Annotated[t.CallToolResult, models.DecisionResponse]:
        def work(conn):
            data = reads.get_decision(conn, id, slug)
            return data, pending.build(conn, context_tags=set(data.tags))

        data, block = with_db(pool, work)
        contra = sum(1 for e in data.evidence if e.role == "contradicts")
        summary = (f"{data.title} ({data.state}): {len(data.evidence)} evidências, "
                   f"{contra} contrária(s), {len(data.alternatives)} alternativa(s) descartada(s).")
        return _result(summary, models.DecisionResponse(data=data, pending=block), block)

    @server.tool(name="list_pending_reviews", description=DESCRIPTIONS["list_pending_reviews"],
                 annotations=READ)
    def list_pending_reviews(
        owner_email: str | None = None,
        project: str | None = None,
        overdue_only: bool = False,
        include_unattested: bool = True,
    ) -> Annotated[t.CallToolResult, models.PendingReviewsResponse]:
        owner = owner_email or identity.current_email()

        def work(conn):
            data = reads.pending_reviews(conn, owner, project, overdue_only, include_unattested)
            return data, pending.build(conn)

        data, block = with_db(pool, work)
        summary = (f"{len(data.reviews)} revisões em aberto, {data.overdue_count} vencida(s); "
                   f"{len(data.unattested)} registro(s) aguardando atestação.")
        return t.CallToolResult(
            content=_text(summary),
            structured_content=models.PendingReviewsResponse(data=data, pending=block)
            .model_dump(mode="json"))

    @server.tool(name="propose_decision", description=DESCRIPTIONS["propose_decision"],
                 annotations=t.ToolAnnotations(idempotent_hint=True, **WRITE))
    def propose_decision(
        title: str,
        description: str,
        decider_email: str,
        context: str | None = None,
        decided_on: str | None = None,
        door: str = "two_way",
        project: str | None = None,
        alternatives: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> Annotated[t.CallToolResult, models.ProposeResponse]:
        def work(conn):
            data, on_this_record = writes.propose_decision(
                conn, title=title, description=description, decider_email=decider_email,
                context=context, decided_on=decided_on, door=door, project=project,
                alternatives=alternatives, tags=tags, evidence=evidence,
                idempotency_key=idempotency_key)
            return data, pending.build(conn, context_tags=set(writes.normalize_tags(tags)),
                                       on_this_record=on_this_record)

        data, block = with_db(pool, work)
        body = models.ProposeResponse(data=models.ProposeData(**data), pending=block)
        if data["reused"]:
            summary = (f"Essa idempotency_key já tinha registrado a decisão {data['decision_id']} "
                       f"({data['state']}); o conteúdo desta chamada não foi aplicado.")
        else:
            summary = (f"Decisão registrada como proposta ({data['decision_id']}). Uma pessoa "
                       "precisa atestá-la e registrar a expectativa; ainda não há superfície "
                       "de atestação, então ela fica proposta.")
        return _result(summary, body, block)

    @server.tool(name="attach_evidence", description=DESCRIPTIONS["attach_evidence"],
                 annotations=t.ToolAnnotations(idempotent_hint=True, **WRITE))
    def attach_evidence(
        decision_id: str,
        role: str,
        evidence_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        # strict: sem ele, "0.5" e true virariam número em silêncio.
        weight: Annotated[float | None, Field(strict=True)] = None,
        note: str | None = None,
    ) -> Annotated[t.CallToolResult, models.AttachResponse]:
        def work(conn):
            data, nudge = writes.attach_evidence(conn, decision_id=decision_id, role=role,
                                                 evidence_id=evidence_id, evidence=evidence,
                                                 weight=weight, note=note)
            tags = {r["name"] for r in conn.execute(
                "SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id "
                "WHERE x.decision_id = %s", (data["decision_id"],)).fetchall()}
            return data, pending.build(conn, context_tags=tags, on_this_record=nudge)

        data, block = with_db(pool, work)
        if data["already_linked"]:
            summary = (f"Evidência {data['evidence_id']} já estava vinculada como "
                       f"{data['role']}; nada mudou.")
        else:
            summary = (f"Evidência {data['evidence_id']} vinculada como {data['role']}"
                       + (" (reaproveitada, não duplicada)." if data["reused_existing"] else "."))
        return _result(summary, models.AttachResponse(data=models.AttachData(**data),
                                                      pending=block), block)

    @server.tool(name="record_learning", description=DESCRIPTIONS["record_learning"],
                 annotations=t.ToolAnnotations(idempotent_hint=False, **WRITE))
    def record_learning(
        summary: str,
        decision_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Annotated[t.CallToolResult, models.LearningResponse]:
        def work(conn):
            data = writes.record_learning(conn, summary=summary, decision_ids=decision_ids,
                                          evidence_ids=evidence_ids, tags=tags)
            nudge = (["Lição muito curta para viajar entre projetos: em que condição ela vale?"]
                     if len(summary.split()) < 8 else [])
            return data, pending.build(conn, context_tags=set(writes.normalize_tags(tags)),
                                       on_this_record=nudge)

        data, block = with_db(pool, work)
        body = models.LearningResponse(data=models.LearningData(**data), pending=block)
        return _result(f"Lição registrada como proposta ({data['learning_id']}).", body, block)

    # Ordem importa: a identidade é resolvida antes das recusas e da ferramenta.
    server.middleware.append(identity.middleware(audiences))
    server.middleware.append(guard.refuse_expectation)
    server.middleware.append(guard.refuse_nul)
    return server
