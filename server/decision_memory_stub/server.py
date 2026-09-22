"""As seis ferramentas de MCP_TOOLS.md, respondendo a partir de fixtures.

São seis e não haverá uma sétima sem ADR. As descrições abaixo são as do
MCP_TOOLS.md, escritas como gatilho ("chame quando…"): é a descrição que faz o
agente chamar a ferramenta no momento certo, e é justamente isso que este stub
existe para medir.
"""

from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import date
from typing import Annotated, Any

import mcp.types as t
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import models, pending, writes
from .corpus import Corpus, load_corpus, score, today, tokenize

ATTEST_BASE = "https://decision-memory.exemplo.com.br/atestar"

# Termos que denunciam tentativa de gravar expectativa por agente. Nenhum deles
# é parâmetro de ferramenta alguma; se chegar um, a resposta é recusa explícita.
FORBIDDEN_ARGUMENT_TERMS = (
    "confidence",
    "confianca",
    "expectation",
    "expectativa",
    "expected_metric",
    "expected_magnitude",
    "resultado_esperado",
    "magnitude_esperada",
    "certeza",
)

EXPECTATION_REFUSAL = (
    "Expectativa é registrada pela pessoa na atestação, não pelo agente. Siga sem ela."
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _forbidden_keys(arguments: Any) -> list[str]:
    if not isinstance(arguments, dict):
        return []
    found = []
    for key in arguments:
        folded = _fold(str(key))
        if any(term in folded for term in FORBIDDEN_ARGUMENT_TERMS):
            found.append(str(key))
    return sorted(found)


def _source_of(evidence: dict[str, Any]) -> models.SourceRef | None:
    if not evidence.get("source_system") and not evidence.get("url"):
        return None
    return models.SourceRef(
        system=evidence.get("source_system"),
        external_id=evidence.get("external_id"),
        url=evidence.get("url"),
    )


def _text(*parts: str) -> list[t.TextContent]:
    """Texto curto equivalente, para clientes que não leem structuredContent."""
    return [t.TextContent(type="text", text=" ".join(p for p in parts if p))]


def _pending_sentence(block: models.Pending) -> str:
    bits = []
    if block.reviews_due:
        nearest = block.reviews_due[0]
        bits.append(
            f"Revisão vencida há {nearest.overdue_days} dias: {nearest.title}."
        )
    if block.on_this_record:
        bits.append("Falta: " + "; ".join(block.on_this_record) + ".")
    return " ".join(bits)


def build_server(corpus: Corpus | None = None) -> MCPServer:
    corpus = corpus or load_corpus()
    idempotency: dict[str, tuple[str, str]] = {}

    server = MCPServer(
        name="decision-memory",
        version="0.1.0",
        instructions=(
            "Registro de decisões e aprendizagens de produto. Consulte antes de redigir "
            "proposta, PRD ou plano de experimento, e registre a decisão quando ela for "
            "fechada na conversa. Confiança e resultado esperado são preenchidos por "
            "pessoa na atestação, nunca por agente."
        ),
    )

    # ---------------------------------------------------------------- leitura

    @server.tool(
        name="search_evidence",
        description=(
            "Chame ANTES de redigir proposta, PRD, plano de experimento ou qualquer "
            "recomendação de produto, para saber o que a organização já testou, decidiu "
            "ou aprendeu sobre o tema. Chame também sempre que alguém perguntar 'já "
            "testamos isso?' ou 'por que decidimos X?'. Retorna evidências, lições e "
            "decisões, com a força de cada evidência."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def search_evidence(
        query: str,
        tags: list[str] | None = None,
        kinds: list[str] | None = None,
        source_system: str | None = None,
        include: list[str] | None = None,
        limit: int = 10,
    ) -> Annotated[t.CallToolResult, models.SearchResponse]:
        include = include or ["evidence", "learning", "decision"]
        if kinds or source_system:
            # Tipo e origem são atributos de evidência. Lição e decisão não têm
            # como satisfazer esse filtro, então pedir por ele restringe a busca.
            include = [kind for kind in include if kind == "evidence"] or ["evidence"]
        limit = max(1, min(int(limit), 50))
        wanted_tags = {tag.lower() for tag in (tags or [])}
        query_tokens = tokenize(query) | {tok for tag in wanted_tags for tok in tokenize(tag)}

        scored: list[tuple[float, models.SearchItem]] = []

        if "evidence" in include:
            for item in corpus.evidence:
                if kinds and item["kind"] not in kinds:
                    continue
                if source_system and item.get("source_system") != source_system:
                    continue
                if wanted_tags and not wanted_tags & set(item.get("tags", [])):
                    continue
                relevance = score(
                    query_tokens, item["title"], item.get("summary", ""), tags=item.get("tags")
                )
                if relevance <= 0:
                    continue
                scored.append(
                    (
                        relevance,
                        models.SearchItem(
                            type="evidence",
                            id=item["id"],
                            title=item["title"],
                            summary=item.get("summary"),
                            strength=item.get("strength"),
                            source=_source_of(item),
                            tags=item.get("tags", []),
                            # Evidência não tem estado próprio no modelo lógico; o corpus
                            # vem de importação curada. Ver README, "Divergências".
                            state="attested",
                        ),
                    )
                )

        if "learning" in include:
            for item in corpus.learnings:
                if wanted_tags and not wanted_tags & set(item.get("tags", [])):
                    continue
                relevance = score(query_tokens, item["summary"], tags=item.get("tags"))
                if relevance <= 0:
                    continue
                scored.append(
                    (
                        relevance,
                        models.SearchItem(
                            type="learning",
                            id=item["id"],
                            title=item["summary"],
                            summary=None,
                            tags=item.get("tags", []),
                            state=item["state"],
                        ),
                    )
                )

        if "decision" in include:
            for item in corpus.decisions:
                if wanted_tags and not wanted_tags & set(item.get("tags", [])):
                    continue
                relevance = score(
                    query_tokens,
                    item["title"],
                    item.get("context", ""),
                    item.get("description", ""),
                    tags=item.get("tags"),
                )
                if relevance <= 0:
                    continue
                scored.append(
                    (
                        relevance,
                        models.SearchItem(
                            type="decision",
                            id=item["id"],
                            title=item["title"],
                            summary=item.get("description"),
                            tags=item.get("tags", []),
                            state=item["state"],
                        ),
                    )
                )

        # Ordena por relevância textual e, para empate, por id — nunca por efeito.
        scored.sort(key=lambda row: (row[0], row[1].id), reverse=True)
        items = [item for _, item in scored[:limit]]

        hit_tags: set[str] = set()
        for item in items:
            hit_tags |= set(item.tags)

        note = None
        if not items:
            note = (
                "Nada encontrado no registro sobre esse tema. Diga isso explicitamente "
                "ao usuário em vez de seguir como se não houvesse consultado."
            )

        block = pending.build(corpus, context_tags=hit_tags or tokenize(query))
        body = models.SearchResponse(
            data=models.SearchData(
                query=query, items=items, total=len(scored), note=note
            ),
            pending=block,
        )
        resumo = (
            f"{len(items)} de {len(scored)} resultados para '{query}'."
            if items
            else f"Nenhum resultado para '{query}'."
        )
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
            structured_content=body.model_dump(mode="json"),
        )

    @server.tool(
        name="get_decision",
        description=(
            "Chame quando precisar do raciocínio completo por trás de uma decisão "
            "específica: contexto, alternativas descartadas e por quê, evidências que "
            "sustentaram ou contradisseram, lições derivadas e revisões realizadas."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_decision(
        id: str | None = None, slug: str | None = None
    ) -> Annotated[t.CallToolResult, models.DecisionResponse]:
        if not id and not slug:
            raise ToolError("Informe id ou slug da decisão.")
        decision = corpus.decision_by_id(id) if id else None
        if decision is None and slug:
            decision = corpus.decision_by_slug(slug)
        if decision is None:
            raise ToolError(
                f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
            )

        evidence_rows = []
        for link in decision.get("evidence", []):
            item = corpus.evidence_by_id(link["evidence_id"])
            if item is None:
                continue
            evidence_rows.append(
                models.DecisionEvidence(
                    evidence_id=item["id"],
                    title=item["title"],
                    kind=item["kind"],
                    role=link["role"],
                    weight=link.get("weight"),
                    strength=item.get("strength"),
                    source=_source_of(item),
                    note=link.get("note") or None,
                )
            )

        learning_rows = []
        for learning_id in decision.get("learnings", []):
            item = corpus.learning_by_id(learning_id)
            if item is None:
                continue
            learning_rows.append(
                models.DecisionLearning(
                    learning_id=item["id"], summary=item["summary"], state=item["state"]
                )
            )

        review_rows = [
            models.DecisionReview(
                review_id=r["id"],
                due_on=r["due_on"],
                done_on=r.get("done_on"),
                verdict=r.get("verdict"),
                notes=r.get("notes"),
            )
            for r in corpus.reviews_of(decision["id"])
        ]

        person = corpus.person(decision.get("decider"))
        project = corpus.project(decision.get("project"))
        attested = decision["state"] == "attested"

        expectation = None
        if attested and decision.get("expectation"):
            raw = decision["expectation"]
            recorded_by = corpus.person(raw["recorded_by"])
            expectation = models.Expectation(
                recorded_by=recorded_by["name"] if recorded_by else raw["recorded_by"],
                recorded_at=raw["recorded_at"],
                confidence=raw["confidence"],
                expected_metric=raw["expected_metric"],
                expected_magnitude=raw["expected_magnitude"],
                due_on=raw["due_on"],
            )

        block = pending.build(corpus, context_tags=set(decision.get("tags", [])))
        body = models.DecisionResponse(
            data=models.DecisionData(
                decision_id=decision["id"],
                slug=decision["slug"],
                title=decision["title"],
                context=decision.get("context"),
                description=decision["description"],
                door=decision["door"],
                decided_on=decision["decided_on"],
                decider=person["name"] if person else None,
                project=project["name"] if project else None,
                state=decision["state"],
                tags=decision.get("tags", []),
                alternatives=[models.Alternative(**a) for a in decision.get("alternatives", [])],
                evidence=evidence_rows,
                learnings=learning_rows,
                reviews=review_rows,
                expectation=expectation,
                provenance=models.Provenance(
                    author_kind="human" if attested else "agent",
                    attested_by=person["name"] if attested and person else None,
                    attested_at=decision.get("expectation", {}).get("recorded_at")
                    if attested
                    else None,
                ),
            ),
            pending=block,
        )
        contra = sum(1 for e in evidence_rows if e.role == "contradicts")
        resumo = (
            f"{decision['title']} ({decision['state']}): "
            f"{len(evidence_rows)} evidências, {contra} contrária(s), "
            f"{len(decision.get('alternatives', []))} alternativa(s) descartada(s)."
        )
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
            structured_content=body.model_dump(mode="json"),
        )

    @server.tool(
        name="list_pending_reviews",
        description=(
            "Chame no início de uma sessão de planejamento, ao retomar um projeto ou "
            "quando alguém perguntar o que está pendente: lista decisões cuja revisão de "
            "desfecho venceu ou está próxima, e registros propostos aguardando atestação."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def list_pending_reviews(
        owner_email: str | None = None,
        project: str | None = None,
        overdue_only: bool = False,
        include_unattested: bool = True,
    ) -> Annotated[t.CallToolResult, models.PendingReviewsResponse]:
        reference = today()
        rows: list[models.PendingReviewItem] = []
        for review in corpus.open_reviews():
            decision = corpus.decision_by_id(review["decision_id"])
            if decision is None:
                continue
            owner = corpus.person(decision.get("decider"))
            proj = corpus.project(decision.get("project"))
            if owner_email and (owner is None or owner["email"].lower() != owner_email.lower()):
                continue
            if project and (proj is None or project.lower() not in proj["name"].lower()):
                continue
            overdue = (reference - date.fromisoformat(review["due_on"])).days
            if overdue_only and overdue < 0:
                continue
            rows.append(
                models.PendingReviewItem(
                    review_id=review["id"],
                    decision_id=decision["id"],
                    slug=decision["slug"],
                    title=decision["title"],
                    due_on=review["due_on"],
                    overdue_days=overdue,
                    owner=owner["name"] if owner else None,
                    project=proj["name"] if proj else None,
                )
            )
        rows.sort(key=lambda r: r.overdue_days, reverse=True)

        unattested: list[models.UnattestedItem] = []
        if include_unattested:
            unattested = [
                models.UnattestedItem(
                    type="decision", id=d["id"], title=d["title"], created_on=d["decided_on"]
                )
                for d in corpus.decisions
                if d["state"] == "proposed"
            ] + [
                models.UnattestedItem(
                    type="learning", id=ln["id"], title=ln["summary"], created_on=ln["recorded_on"]
                )
                for ln in corpus.learnings
                if ln["state"] == "proposed"
            ]

        overdue_count = sum(1 for r in rows if r.overdue_days >= 0)
        block = pending.build(corpus)
        body = models.PendingReviewsResponse(
            data=models.PendingReviewsData(
                reviews=rows, unattested=unattested, overdue_count=overdue_count
            ),
            pending=block,
        )
        resumo = (
            f"{len(rows)} revisões em aberto, {overdue_count} vencida(s); "
            f"{len(unattested)} registro(s) aguardando atestação."
        )
        return t.CallToolResult(
            content=_text(resumo),
            structured_content=body.model_dump(mode="json"),
        )

    # ----------------------------------------------------------------- escrita

    @server.tool(
        name="propose_decision",
        description=(
            "Chame quando uma escolha de produto for fechada na conversa — 'vamos com a "
            "opção B', 'decidimos não lançar', 'mantemos o fluxo atual' — para que ela "
            "entre no registro com contexto e alternativas enquanto o raciocínio ainda "
            "está fresco. O registro nasce como proposta e será confirmado por uma pessoa."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
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
        person = corpus.person_by_email(decider_email)
        if person is None:
            raise ToolError(
                "Pessoa não encontrada. Confirme o e-mail com o usuário; "
                "o registro não foi criado."
            )
        if door not in ("one_way", "two_way"):
            raise ToolError("door deve ser one_way ou two_way.")

        if idempotency_key and idempotency_key in idempotency:
            decision_id, slug = idempotency[idempotency_key]
            reused = True
        else:
            decision_id = writes.new_id("dec")
            slug = "-".join(tokenize(title)) or decision_id
            reused = False
            if idempotency_key:
                idempotency[idempotency_key] = (decision_id, slug)

        missing: list[str] = []
        if not alternatives:
            missing.append("alternatives")
        if not evidence:
            missing.append("evidence")
        if not context:
            missing.append("context")

        prompts = {
            "alternatives": "Sem alternativas descartadas: quais opções foram consideradas e por que perderam?",
            "evidence": "Sem evidência vinculada: o que sustentou ou contradisse essa escolha?",
            "context": "Sem contexto: qual era o problema no momento da decisão?",
        }
        on_this_record = [prompts[field] for field in missing]

        if not reused:
            writes.record(
                "propose_decision",
                {
                    "decision_id": decision_id,
                    "title": title,
                    "decider_email": decider_email,
                    "door": door,
                    "tags": tags or [],
                    "alternatives_count": len(alternatives or []),
                    "evidence_count": len(evidence or []),
                    "missing": missing,
                },
            )

        block = pending.build(
            corpus, context_tags=set(tags or []), on_this_record=on_this_record
        )
        body = models.ProposeResponse(
            data=models.ProposeData(
                decision_id=decision_id,
                slug=slug,
                state="proposed",
                attest_url=f"{ATTEST_BASE}/{decision_id}",
                missing=missing,
            ),
            pending=block,
        )
        resumo = (
            f"Decisão registrada como proposta ({decision_id}). "
            "Uma pessoa precisa atestá-la e registrar a expectativa."
        )
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
            structured_content=body.model_dump(mode="json"),
        )

    @server.tool(
        name="attach_evidence",
        description=(
            "Chame quando uma evidência — resultado de experimento, estudo, análise, "
            "entrevista ou documento — tiver pesado numa decisão, seja a favor ou contra. "
            "Registre também a evidência que contradisse a escolha: evidência contrária "
            "registrada é o que distingue memória de justificativa."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def attach_evidence(
        decision_id: str,
        role: str,
        evidence_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        weight: float | None = None,
        note: str | None = None,
    ) -> Annotated[t.CallToolResult, models.AttachResponse]:
        if role not in ("supports", "contradicts", "discarded"):
            raise ToolError("role deve ser supports, contradicts ou discarded.")
        if not evidence_id and not evidence:
            raise ToolError("Informe evidence_id de uma evidência existente ou o objeto evidence.")
        if weight is not None and not 0 <= weight <= 1:
            raise ToolError("weight vai de 0 a 1.")

        reused = False
        if evidence_id:
            existing = corpus.evidence_by_id(evidence_id)
            if existing is None:
                raise ToolError(
                    f"Evidência não encontrada: {evidence_id}. "
                    "Use search_evidence ou envie o objeto evidence."
                )
            resolved_id = existing["id"]
            reused = True
        else:
            system = (evidence or {}).get("source_system")
            external = (evidence or {}).get("external_id")
            existing = (
                corpus.evidence_by_source(system, external) if system and external else None
            )
            if existing is not None:
                resolved_id = existing["id"]
                reused = True
            else:
                resolved_id = writes.new_id("ev")

        writes.record(
            "attach_evidence",
            {
                "decision_id": decision_id,
                "evidence_id": resolved_id,
                "role": role,
                "weight": weight,
                "reused_existing": reused,
                "new_evidence": None if reused else evidence,
            },
        )

        decision = corpus.decision_by_id(decision_id)
        context_tags = set(decision.get("tags", [])) if decision else set()
        on_this_record = []
        if role == "supports":
            on_this_record.append(
                "Só há evidência favorável registrada até agora: houve algo que contradisse a escolha?"
            )
        block = pending.build(corpus, context_tags=context_tags, on_this_record=on_this_record)
        body = models.AttachResponse(
            data=models.AttachData(
                decision_id=decision_id,
                evidence_id=resolved_id,
                role=role,
                reused_existing=reused,
                state="proposed",
            ),
            pending=block,
        )
        resumo = (
            f"Evidência {resolved_id} vinculada como {role}"
            + (" (reaproveitada, não duplicada)." if reused else ".")
        )
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
            structured_content=body.model_dump(mode="json"),
        )

    @server.tool(
        name="record_learning",
        description=(
            "Chame quando um experimento terminar, uma decisão for revisada ou alguém "
            "formular uma conclusão que valha para além do caso atual — 'usuários "
            "preferem X quando Y'. Escreva a lição como afirmação reutilizável, não como "
            "resumo do caso."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def record_learning(
        summary: str,
        decision_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> Annotated[t.CallToolResult, models.LearningResponse]:
        if not decision_ids and not evidence_ids:
            raise ToolError(
                "Uma lição precisa de origem: informe decision_ids ou evidence_ids."
            )
        learning_id = writes.new_id("ln")
        writes.record(
            "record_learning",
            {
                "learning_id": learning_id,
                "summary": summary,
                "decision_ids": decision_ids or [],
                "evidence_ids": evidence_ids or [],
                "tags": tags or [],
            },
        )
        on_this_record = []
        if len(summary.split()) < 8:
            on_this_record.append(
                "Lição muito curta para viajar entre projetos: em que condição ela vale?"
            )
        block = pending.build(corpus, context_tags=set(tags or []), on_this_record=on_this_record)
        body = models.LearningResponse(
            data=models.LearningData(
                learning_id=learning_id,
                state="proposed",
                linked_decisions=decision_ids or [],
                linked_evidence=evidence_ids or [],
            ),
            pending=block,
        )
        return t.CallToolResult(
            content=_text(
                f"Lição registrada como proposta ({learning_id}).", _pending_sentence(block)
            ),
            structured_content=body.model_dump(mode="json"),
        )

    # -------------------------------------------------------------- middleware

    async def guard_and_log(ctx, call_next):
        """Recusa expectativa vinda de agente e registra toda invocação."""
        if ctx.method == "tools/call" and isinstance(ctx.params, dict):
            name = ctx.params.get("name")
            arguments = ctx.params.get("arguments") or {}
            offending = _forbidden_keys(arguments)
            if offending:
                writes.record(
                    "refused_expectation",
                    {"tool": name, "arguments_offered": offending},
                )
                # Resultado de ferramenta com is_error, não erro de protocolo: a
                # recusa manda seguir sem a expectativa, e para o agente seguir ele
                # precisa ler a mensagem, não tomar uma falha de transporte na cara.
                return t.CallToolResult(
                    is_error=True,
                    content=_text(
                        EXPECTATION_REFUSAL,
                        f"Campos recusados: {', '.join(offending)}.",
                        "Chame de novo sem eles.",
                    ),
                )
            writes.record(
                "tool_call",
                {"tool": name, "arguments": arguments},
            )
        return await call_next(ctx)

    server.middleware.append(guard_and_log)
    return server
