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

from . import guard, identity, models, pending, reads

log = logging.getLogger("decision_memory")
T = TypeVar("T")

READ = t.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

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
    except psycopg.Error as exc:
        ref = uuid.uuid4().hex[:8]
        bug = isinstance(exc, psycopg.errors.InsufficientPrivilege)
        log.error(json.dumps({"event": "db_error", "ref": ref, "bug": bug,
                              "sqlstate": exc.sqlstate, "error": str(exc)}))
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
        summary = (f"{len(items)} de {total} resultados para '{query}'." if items
                   else f"Nenhum resultado para '{query}'.")
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

    # As escritas entram aqui (tarefa 10).

    # Ordem importa: a identidade é resolvida antes da guarda e da ferramenta.
    server.middleware.append(identity.middleware(audiences))
    server.middleware.append(guard.refuse_expectation)
    return server
