"""As oito ferramentas de MCP_TOOLS.md, respondendo a partir de fixtures.

São oito e não haverá uma nona sem ADR (a sétima e a oitava vieram do ADR 0006). As descrições abaixo são as do
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


# No mesmo dia, a decisão vem antes da revisão, e a revisão antes da lição que
# ela gerou.
EVENT_ORDER = {"decision": 0, "review": 1, "learning": 2}


def related_order(item: models.RelatedDecision) -> tuple:
    """Vínculos por evidência e lição, depois por tag, depois a data e o id.
    Nunca efeito (ADR 0003)."""
    return (
        len(item.shared_evidence) + len(item.shared_learnings),
        len(item.shared_tags),
        item.decided_on,
        item.decision_id,
    )


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

    @server.tool(
        name="get_topic_timeline",
        description=(
            "Chame quando a pergunta for sobre a trajetória de um tema, e não só sobre o que "
            'se sabe dele — "já mudamos de ideia sobre isso?", "o que veio antes desta '
            'decisão?", "como chegamos ao fluxo atual?" — e antes de propor reverter ou '
            "retomar algo que já foi decidido. Retorna, em ordem cronológica, as decisões "
            "sobre o tema, as revisões de desfecho e as lições registradas."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def get_topic_timeline(
        query: str,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> Annotated[t.CallToolResult, models.TimelineResponse]:
        limit = max(1, min(int(limit), 50))
        wanted_tags = {tag.lower() for tag in (tags or [])}
        query_tokens = tokenize(query) | {tok for tag in wanted_tags for tok in tokenize(tag)}

        # A seleção é a de search_evidence; só a ordem de saída é a data.
        scored: list[tuple[float, models.TimelineEvent]] = []
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
            project = corpus.project(item.get("project"))
            scored.append(
                (
                    relevance,
                    models.TimelineEvent(
                        on=item["decided_on"],
                        type="decision",
                        id=item["id"],
                        title=item["title"],
                        decision_id=item["id"],
                        slug=item["slug"],
                        state=item["state"],
                        door=item["door"],
                        project=project["name"] if project else None,
                        tags=item.get("tags", []),
                    ),
                )
            )
        for item in corpus.learnings:
            if wanted_tags and not wanted_tags & set(item.get("tags", [])):
                continue
            relevance = score(query_tokens, item["summary"], tags=item.get("tags"))
            if relevance <= 0:
                continue
            scored.append(
                (
                    relevance,
                    models.TimelineEvent(
                        on=item["recorded_on"],
                        type="learning",
                        id=item["id"],
                        title=item["summary"],
                        state=item["state"],
                        tags=item.get("tags", []),
                    ),
                )
            )

        scored.sort(key=lambda row: (row[0], row[1].id), reverse=True)
        events = [event for _, event in scored[:limit]]

        # Só revisão realizada é evento; a que está por vir é assunto do `pending`.
        decisions = {event.id: event for event in events if event.type == "decision"}
        for review in corpus.reviews:
            decision = decisions.get(review["decision_id"])
            if decision is None or not review.get("done_on"):
                continue
            events.append(
                models.TimelineEvent(
                    on=review["done_on"],
                    type="review",
                    id=review["id"],
                    title=decision.title,
                    decision_id=decision.id,
                    verdict=review.get("verdict"),
                    notes=review.get("notes"),
                    tags=decision.tags,
                )
            )
        events.sort(key=lambda e: (e.on, EVENT_ORDER[e.type], e.id))

        hit_tags = {tag for event in events for tag in event.tags}
        note = None
        if not events:
            note = (
                "Nada encontrado no registro sobre esse tema. Diga isso explicitamente "
                "ao usuário em vez de seguir como se não houvesse consultado."
            )
        block = pending.build(corpus, context_tags=hit_tags or tokenize(query))
        body = models.TimelineResponse(
            data=models.TimelineData(query=query, events=events, total=len(scored), note=note),
            pending=block,
        )
        if events:
            count = {
                kind: sum(1 for e in events if e.type == kind)
                for kind in ("decision", "review", "learning")
            }
            resumo = (
                f"{len(events)} eventos sobre '{query}', de {events[0].on} a {events[-1].on}: "
                f"{count['decision']} decisão(ões), {count['review']} revisão(ões), "
                f"{count['learning']} lição(ões)."
            )
        else:
            resumo = f"Nenhum evento para '{query}'."
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
            structured_content=body.model_dump(mode="json"),
        )

    @server.tool(
        name="find_related",
        description=(
            "Chame antes de rever, reverter ou contrariar uma decisão, ou quando alguém "
            "perguntar o que mais depende dela: lista as outras decisões ligadas a ela por "
            "evidência em comum — com o papel que a evidência teve em cada uma —, por lição "
            "em comum ou por tag. É o que mostra quem mais se apoiou nas mesmas premissas."
        ),
        annotations=t.ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def find_related(
        id: str | None = None, slug: str | None = None, limit: int = 10
    ) -> Annotated[t.CallToolResult, models.RelatedResponse]:
        if not id and not slug:
            raise ToolError("Informe id ou slug da decisão.")
        decision = corpus.decision_by_id(id) if id else None
        if decision is None and slug:
            decision = corpus.decision_by_slug(slug)
        if decision is None:
            raise ToolError(
                f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
            )
        limit = max(1, min(int(limit), 30))

        roles_here = {link["evidence_id"]: link["role"] for link in decision.get("evidence", [])}
        learnings_here = corpus.learnings_of(decision["id"])
        tags_here = set(decision.get("tags", []))

        items: list[models.RelatedDecision] = []
        for other in corpus.decisions:
            if other["id"] == decision["id"]:
                continue
            shared_evidence = []
            for link in sorted(other.get("evidence", []), key=lambda x: x["evidence_id"]):
                if link["evidence_id"] not in roles_here:
                    continue
                item = corpus.evidence_by_id(link["evidence_id"])
                shared_evidence.append(
                    models.SharedEvidence(
                        evidence_id=link["evidence_id"],
                        title=item["title"] if item else link["evidence_id"],
                        role_here=roles_here[link["evidence_id"]],
                        role_there=link["role"],
                    )
                )
            shared_learnings = [
                models.SharedLearning(
                    learning_id=learning_id,
                    summary=corpus.learning_by_id(learning_id)["summary"],
                )
                for learning_id in sorted(learnings_here & corpus.learnings_of(other["id"]))
                if corpus.learning_by_id(learning_id)
            ]
            shared_tags = sorted(tags_here & set(other.get("tags", [])))
            if not (shared_evidence or shared_learnings or shared_tags):
                continue
            project = corpus.project(other.get("project"))
            items.append(
                models.RelatedDecision(
                    decision_id=other["id"],
                    slug=other["slug"],
                    title=other["title"],
                    decided_on=other["decided_on"],
                    state=other["state"],
                    project=project["name"] if project else None,
                    shared_evidence=shared_evidence,
                    shared_learnings=shared_learnings,
                    shared_tags=shared_tags,
                )
            )
        items.sort(key=related_order, reverse=True)
        total = len(items)
        items = items[:limit]

        note = None
        if not items:
            note = (
                "Nenhuma outra decisão compartilha evidência, lição ou tag com esta. Diga "
                "isso ao usuário: no registro, ela não tem vizinhas."
            )
        block = pending.build(corpus, context_tags=tags_here)
        body = models.RelatedResponse(
            data=models.RelatedData(
                decision_id=decision["id"],
                slug=decision["slug"],
                title=decision["title"],
                related=items,
                total=total,
                note=note,
            ),
            pending=block,
        )
        if items:
            por_evidencia = sum(1 for i in items if i.shared_evidence)
            opostas = sum(
                1
                for i in items
                for e in i.shared_evidence
                if {e.role_here, e.role_there} == {"supports", "contradicts"}
            )
            resumo = (
                f"{decision['title']}: {len(items)} de {total} decisões relacionadas, "
                f"{por_evidencia} por evidência em comum."
            )
            if opostas:
                resumo += f" {opostas} evidência(s) com papel oposto entre as duas decisões."
        else:
            resumo = f"{decision['title']}: nenhuma decisão relacionada."
        return t.CallToolResult(
            content=_text(resumo, _pending_sentence(block)),
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
