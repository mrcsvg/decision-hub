"""Consultas de leitura: busca, decisão, revisões pendentes, linha do tempo e
decisões relacionadas."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import models
from .config import today
from .db import evidence_attested
from .guard import fold
from .writes import fold_tags

STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem
    e ou que se ao aos à às pelo pela como mais menos ja já nao não sobre entre
    """.split()
)

EVIDENCE_STATE = f"CASE WHEN {evidence_attested('e.id')} THEN 'attested' ELSE 'proposed' END"

INCLUDE = ("evidence", "learning", "decision")

# O dicionário 'simple' guarda o acento, e a coluna `search` do schema também:
# "confirmacao" não casaria com "confirmação". Tirar o acento no próprio schema
# (extensão unaccent na coluna gerada) é mudança de spec e pede ADR; por ora o
# texto é dobrado aqui, na hora da consulta, e os termos saem dobrados de
# `terms()`. Letras maiúsculas acentuadas estão na lista porque, com collation
# C, lower() não mexe em Ç nem Ã. Letra acentuada fora da lista (não usada em
# português) continua com acento no banco e não casa.
#
# Custo: o tsvector é calculado linha a linha e o índice GIN de `search` não
# é usado. Com o volume da v0 não pesa; quando pesar, a saída é unaccent numa
# coluna gerada com índice, via ADR.
ACCENTED = "áàâãäéèêëíìîïóòôõöúùûüçñÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ"
PLAIN = "aaaaaeeeeiiiiooooouuuucn" * 2


def fold_sql(text_sql: str) -> str:
    """Expressão SQL que põe `text_sql` em minúsculas e sem acento, como `fold`."""
    return f"translate(lower({text_sql}), '{ACCENTED}', '{PLAIN}')"


def _folded_tsvector(text_sql: str) -> str:
    return f"to_tsvector('simple', {fold_sql(text_sql)})"


# O mesmo texto de que o schema gera a coluna `search` de cada tabela.
_EV_TEXT = _folded_tsvector("coalesce(e.title, '') || ' ' || coalesce(e.summary, '')")
_DC_TEXT = _folded_tsvector(
    "coalesce(d.title, '') || ' ' || coalesce(d.context, '') || ' ' || coalesce(d.description, '')"
)
_LN_TEXT = _folded_tsvector("l.summary")

# Consulta com mais palavras que isso não é pergunta, é texto colado; e cada
# termo custa um to_tsquery por linha em _HITS. As primeiras bastam.
MAX_TERMS = 32

# Termos em OR: a relevância é decidida em Python, com a mesma regra do stub.
# Tag pode ter hífen ou espaço (spec: `^[a-z0-9][a-z0-9 _./-]*$`), então é
# comparada palavra a palavra, pelo mesmo tsvector dobrado.
_HITS = "(SELECT count(*) FROM unnest(%(terms)s::text[]) term WHERE {vec} @@ to_tsquery('simple', term))"
_CANDIDATE = (
    "(%(any)s::text IS NULL OR {vec} @@ to_tsquery('simple', %(any)s)"
    " OR {tag_vec} @@ to_tsquery('simple', %(any)s))"
)


def _candidate(vec: str, tags_sql: str) -> str:
    # Ponto e barra viram espaço antes do parser: sem isso, 'growth/pricing'
    # seria um caminho e 'v2.checkout' um host, cada um um token só, e a tag não
    # casaria palavra a palavra como em `terms()`.
    tag_text = f"translate(array_to_string({tags_sql}, ' '), './', '  ')"
    return _CANDIDATE.format(vec=vec, tag_vec=_folded_tsvector(tag_text))


_EV_TAGS = "ARRAY(SELECT t.name FROM evidence_tag x JOIN tag t ON t.id = x.tag_id WHERE x.evidence_id = e.id)"
_LN_TAGS = "ARRAY(SELECT t.name FROM learning_tag x JOIN tag t ON t.id = x.tag_id WHERE x.learning_id = l.id)"
_DC_TAGS = "ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id WHERE x.decision_id = d.id)"

SEARCH_EVIDENCE_SQL = f"""
SELECT e.id::text AS id, e.title, e.summary, e.strength::text AS strength,
       e.source_system, e.external_id, e.url, {_EV_TAGS} AS tags,
       {_HITS.format(vec=_EV_TEXT)} AS hits, {EVIDENCE_STATE} AS state
  FROM evidence e
 WHERE {_candidate(_EV_TEXT, _EV_TAGS)}
   AND (%(kinds)s::text[] IS NULL OR e.kind::text = ANY(%(kinds)s))
   AND (%(source_system)s::text IS NULL OR e.source_system = %(source_system)s)
   AND (%(tags)s::text[] IS NULL OR {_EV_TAGS} && %(tags)s::text[])
"""

SEARCH_LEARNING_SQL = f"""
SELECT l.id::text AS id, l.summary AS title, NULL AS summary, l.state::text AS state,
       {_LN_TAGS} AS tags, {_HITS.format(vec=_LN_TEXT)} AS hits
  FROM learning l
 WHERE {_candidate(_LN_TEXT, _LN_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_LN_TAGS} && %(tags)s::text[])
"""

SEARCH_DECISION_SQL = f"""
SELECT d.id::text AS id, d.title, d.description AS summary, d.state::text AS state,
       {_DC_TAGS} AS tags, {_HITS.format(vec=_DC_TEXT)} AS hits
  FROM decision d
 WHERE {_candidate(_DC_TEXT, _DC_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_DC_TAGS} && %(tags)s::text[])
"""


def terms(text: str) -> list[str]:
    """Palavras da consulta, em minúsculas e sem acento, sem stopwords e sem
    repetição.

    Só letras e dígitos sobrevivem: cada termo vai para to_tsquery, e nenhum
    operador (& | ! : * ( ) ' <->) pode chegar lá. A dobra vem antes do corte
    para que texto em forma decomposta (e + acento combinante) não parta a
    palavra ao meio.
    """
    raw = "".join(ch if ch.isalnum() else " " for ch in fold(text)).split()
    out: list[str] = []
    for word in raw:
        if len(word) > 2 and word not in STOPWORDS and word not in out:
            out.append(word)
    return out


def relevance(n_terms: int, hits: int, tag_hits: int) -> float:
    """A regra do stub: relevância textual pura, nunca efeito."""
    if n_terms == 0:
        return 1.0
    if tag_hits == 0:
        # Uma palavra solta em comum não é relevância: sem casar tag, consulta de
        # três termos ou mais precisa de pelo menos dois.
        needed = 1 if n_terms <= 2 else 2
        if hits < needed:
            return 0.0
    return hits + 2.0 * tag_hits


def _match_params(query: str, tags: list[str] | None) -> tuple[list[str], dict[str, Any]]:
    """Termos e parâmetros de casamento, iguais em toda leitura por tema."""
    # O filtro por tag é pelo nome exato, dobrado como na escrita (fold_tags);
    # tag fora do padrão não é recusada, só não casa com nada. Como no stub, as
    # palavras da tag também entram como termos, passando por `terms()`: tag
    # crua em to_tsquery seria erro de sintaxe com "(" ou "&".
    wanted_tags = fold_tags(tags)
    words = terms(query)
    for tag in wanted_tags:
        words += [w for w in terms(tag) if w not in words]
    words = words[:MAX_TERMS]
    return words, {"terms": words, "any": " | ".join(words) or None,
                   "tags": wanted_tags or None}


def _row_score(words: list[str], row: dict) -> float:
    tag_tokens = {w for tag in row["tags"] for w in terms(tag)}
    return relevance(len(words), row["hits"], len(set(words) & tag_tokens))


def search(conn, query: str, tags: list[str] | None, kinds: list[str] | None,
           source_system: str | None, include: list[str] | None,
           limit: int) -> tuple[list[models.SearchItem], int]:
    unknown = sorted(set(include or []) - set(INCLUDE))
    if unknown:
        raise ToolError(f"include aceita só {', '.join(INCLUDE)}; recebeu {', '.join(unknown)}.")
    include = include or list(INCLUDE)
    if kinds or source_system:
        # Tipo e origem são atributos de evidência; pedir por eles restringe a busca.
        include = ["evidence"]
    limit = max(1, min(int(limit), 50))
    words, params = _match_params(query, tags)
    params.update(kinds=kinds or None, source_system=source_system)

    scored: list[tuple[float, models.SearchItem]] = []

    def keep(row: dict, item: models.SearchItem) -> None:
        score = _row_score(words, row)
        if score > 0:
            scored.append((score, item))

    if "evidence" in include:
        for row in conn.execute(SEARCH_EVIDENCE_SQL, params).fetchall():
            source = None
            if row["source_system"] or row["url"]:
                source = models.SourceRef(system=row["source_system"],
                                          external_id=row["external_id"], url=row["url"])
            keep(row, models.SearchItem(
                type="evidence", id=row["id"], title=row["title"], summary=row["summary"],
                strength=row["strength"], source=source, tags=row["tags"], state=row["state"]))
    if "learning" in include:
        for row in conn.execute(SEARCH_LEARNING_SQL, params).fetchall():
            keep(row, models.SearchItem(type="learning", id=row["id"], title=row["title"],
                                        tags=row["tags"], state=row["state"]))
    if "decision" in include:
        for row in conn.execute(SEARCH_DECISION_SQL, params).fetchall():
            keep(row, models.SearchItem(type="decision", id=row["id"], title=row["title"],
                                        summary=row["summary"], tags=row["tags"],
                                        state=row["state"]))

    # Relevância textual e, no empate, id — nunca efeito (ADR 0003).
    scored.sort(key=lambda r: (r[0], r[1].id), reverse=True)
    return [item for _, item in scored[:limit]], len(scored)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _decision_row(conn, id: str | None, slug: str | None) -> dict:
    if not id and not slug:
        raise ToolError("Informe id ou slug da decisão.")
    row = conn.execute(
        """
        SELECT d.*, d.id::text AS id_text, p.name AS decider_name, pr.name AS project_name,
               ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id
                      WHERE x.decision_id = d.id ORDER BY t.name) AS tags
          FROM decision d
          JOIN person p ON p.id = d.decider_person_id
          LEFT JOIN project pr ON pr.id = d.project_id
         WHERE d.id = %(id)s OR d.slug = %(slug)s
         ORDER BY (d.id = %(id)s) DESC NULLS LAST  -- vindo os dois, o id vence
         LIMIT 1
        """,
        {"id": _as_uuid(id), "slug": slug},
    ).fetchone()
    if row is None:
        raise ToolError(
            f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
        )
    return row


def get_decision(conn, id: str | None, slug: str | None) -> models.DecisionData:
    row = _decision_row(conn, id, slug)
    did = row["id"]

    # Alternativas e lições não têm coluna de ordem de inserção: o id (uuid)
    # dá ordem estável, não cronológica.
    alternatives = [
        models.Alternative(description=a["description"], rejection_reason=a["rejection_reason"])
        for a in conn.execute(
            "SELECT description, rejection_reason FROM alternative WHERE decision_id = %s "
            "ORDER BY id", (did,)).fetchall()
    ]
    evidence = [
        models.DecisionEvidence(
            evidence_id=e["id"], title=e["title"], kind=e["kind"], role=e["role"],
            weight=float(e["weight"]) if e["weight"] is not None else None,
            strength=e["strength"], note=e["note"],
            source=models.SourceRef(system=e["source_system"], external_id=e["external_id"],
                                    url=e["url"]) if e["source_system"] or e["url"] else None)
        for e in conn.execute(
            """
            SELECT e.id::text AS id, e.title, e.kind::text AS kind, de.role::text AS role,
                   de.weight, e.strength::text AS strength, de.note,
                   e.source_system, e.external_id, e.url
              FROM decision_evidence de JOIN evidence e ON e.id = de.evidence_id
             WHERE de.decision_id = %s ORDER BY de.weight DESC NULLS LAST, e.id
            """, (did,)).fetchall()
    ]
    learnings = [
        models.DecisionLearning(learning_id=ln["id"], summary=ln["summary"], state=ln["state"])
        for ln in conn.execute(
            "SELECT l.id::text AS id, l.summary, l.state::text AS state FROM decision_learning x "
            "JOIN learning l ON l.id = x.learning_id WHERE x.decision_id = %s ORDER BY l.id",
            (did,)).fetchall()
    ]
    reviews = [
        models.DecisionReview(review_id=r["id"], due_on=r["due_on"].isoformat(),
                              done_on=r["done_on"].isoformat() if r["done_on"] else None,
                              verdict=r["verdict"], notes=r["notes"])
        for r in conn.execute(
            "SELECT id::text AS id, due_on, done_on, verdict::text AS verdict, notes "
            "FROM review WHERE decision_id = %s ORDER BY due_on, id", (did,)).fetchall()
    ]

    expectation = None
    if row["state"] == "attested":
        x = conn.execute(
            "SELECT p.name, x.recorded_at, x.confidence, x.expected_metric, "
            "x.expected_magnitude, x.due_on FROM expectation x "
            "JOIN person p ON p.id = x.recorded_by WHERE x.decision_id = %s "
            "ORDER BY x.recorded_at DESC LIMIT 1", (did,)).fetchone()
        if x:
            expectation = models.Expectation(
                recorded_by=x["name"], recorded_at=x["recorded_at"].isoformat(),
                confidence=float(x["confidence"]), expected_metric=x["expected_metric"],
                expected_magnitude=x["expected_magnitude"], due_on=x["due_on"].isoformat())

    prov = conn.execute(
        """
        SELECT pv.author_kind::text AS author_kind, pv.model, pv.source_ref, pv.attested_at,
               pp.name AS principal, pa.name AS attested_by
          FROM provenance pv
          LEFT JOIN person pp ON pp.id = pv.principal_person_id
          LEFT JOIN person pa ON pa.id = pv.attested_by
         WHERE pv.object_type = 'decision' AND pv.object_id = %s
         ORDER BY pv.created_at, pv.id LIMIT 1
        """, (did,)).fetchone()

    return models.DecisionData(
        decision_id=row["id_text"], slug=row["slug"], title=row["title"],
        context=row["context"], description=row["description"], door=row["door"],
        decided_on=row["decided_on"].isoformat(), decider=row["decider_name"],
        project=row["project_name"], state=row["state"], tags=row["tags"],
        alternatives=alternatives, evidence=evidence, learnings=learnings, reviews=reviews,
        expectation=expectation,
        provenance=models.Provenance(
            author_kind=prov["author_kind"], model=prov["model"], principal=prov["principal"],
            source_ref=prov["source_ref"], attested_by=prov["attested_by"],
            attested_at=prov["attested_at"].isoformat() if prov["attested_at"] else None,
        ) if prov else None,
    )


def _like_literal(text: str | None) -> str | None:
    """`text` para ILIKE ... ESCAPE '\\' casando como texto: % e _ de quem chama
    não viram curinga."""
    if text is None:
        return None
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def pending_reviews(conn, owner_email: str | None, project: str | None, overdue_only: bool,
                    include_unattested: bool) -> models.PendingReviewsData:
    reference = today()
    rows = conn.execute(
        """
        SELECT r.id::text AS review_id, d.id::text AS decision_id, d.slug, d.title, r.due_on,
               p.name AS owner, pr.name AS project
          FROM review r
          JOIN decision d ON d.id = r.decision_id
          JOIN person p ON p.id = d.decider_person_id
          LEFT JOIN project pr ON pr.id = d.project_id
         WHERE r.done_on IS NULL
           AND (%(owner)s::text IS NULL OR lower(p.email) = lower(%(owner)s))
           AND (%(project)s::text IS NULL
                OR pr.name ILIKE '%%' || %(project)s || '%%' ESCAPE '\\')
         ORDER BY r.due_on, d.id, r.id
        """,
        {"owner": owner_email, "project": _like_literal(project)},
    ).fetchall()
    reviews = []
    for r in rows:
        overdue = (reference - r["due_on"]).days
        if overdue_only and overdue < 0:
            continue
        reviews.append(models.PendingReviewItem(
            review_id=r["review_id"], decision_id=r["decision_id"], slug=r["slug"],
            title=r["title"], due_on=r["due_on"].isoformat(), overdue_days=overdue,
            owner=r["owner"], project=r["project"]))
    # Mais vencida primeiro; decisão e revisão desempatam, para a ordem não
    # depender do plano de execução.
    reviews.sort(key=lambda r: (-r.overdue_days, r.decision_id, r.review_id))

    unattested: list[models.UnattestedItem] = []
    if include_unattested:
        unattested = [
            models.UnattestedItem(type=u["type"], id=u["id"], title=u["title"],
                                  created_on=u["created_on"].isoformat())
            for u in conn.execute(
                f"""
                SELECT 'decision' AS type, id::text AS id, title, decided_on AS created_on
                  FROM decision WHERE state = 'proposed'
                UNION ALL
                SELECT 'learning', id::text, summary, recorded_on FROM learning WHERE state = 'proposed'
                UNION ALL
                SELECT 'evidence', e.id::text, e.title, e.created_at::date FROM evidence e
                 WHERE {EVIDENCE_STATE} = 'proposed'
                ORDER BY 4 DESC, 2
                """).fetchall()
        ]

    return models.PendingReviewsData(
        reviews=reviews, unattested=unattested,
        overdue_count=sum(1 for r in reviews if r.overdue_days >= 0))


# Linha do tempo --------------------------------------------------------------
# Mesma seleção de search_evidence (termos, tags, relevância); a ordem de saída
# é a data. Nenhuma coluna de expectativa é lida aqui (ADR 0006).

TIMELINE_DECISION_SQL = f"""
SELECT d.id::text AS id, d.slug, d.title, d.state::text AS state, d.door::text AS door,
       d.decided_on, pr.name AS project, {_DC_TAGS} AS tags,
       {_HITS.format(vec=_DC_TEXT)} AS hits
  FROM decision d
  LEFT JOIN project pr ON pr.id = d.project_id
 WHERE {_candidate(_DC_TEXT, _DC_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_DC_TAGS} && %(tags)s::text[])
"""

TIMELINE_LEARNING_SQL = f"""
SELECT l.id::text AS id, l.summary AS title, l.state::text AS state, l.recorded_on,
       {_LN_TAGS} AS tags, {_HITS.format(vec=_LN_TEXT)} AS hits
  FROM learning l
 WHERE {_candidate(_LN_TEXT, _LN_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_LN_TAGS} && %(tags)s::text[])
"""

TIMELINE_REVIEW_SQL = """
SELECT r.id::text AS id, r.decision_id::text AS decision_id, d.title, r.done_on,
       r.verdict::text AS verdict, r.notes
  FROM review r JOIN decision d ON d.id = r.decision_id
 WHERE r.done_on IS NOT NULL AND r.decision_id::text = ANY(%s)
"""

# No mesmo dia, a decisão vem antes da revisão, e a revisão antes da lição que
# ela gerou.
_EVENT_ORDER = {"decision": 0, "review": 1, "learning": 2}


def timeline(conn, query: str, tags: list[str] | None,
             limit: int) -> tuple[list[models.TimelineEvent], int]:
    limit = max(1, min(int(limit), 50))
    words, params = _match_params(query, tags)

    scored: list[tuple[float, models.TimelineEvent]] = []
    for row in conn.execute(TIMELINE_DECISION_SQL, params).fetchall():
        score = _row_score(words, row)
        if score > 0:
            scored.append((score, models.TimelineEvent(
                on=row["decided_on"].isoformat(), type="decision", id=row["id"],
                title=row["title"], decision_id=row["id"], slug=row["slug"],
                state=row["state"], door=row["door"], project=row["project"],
                tags=row["tags"])))
    for row in conn.execute(TIMELINE_LEARNING_SQL, params).fetchall():
        score = _row_score(words, row)
        if score > 0:
            scored.append((score, models.TimelineEvent(
                on=row["recorded_on"].isoformat(), type="learning", id=row["id"],
                title=row["title"], state=row["state"], tags=row["tags"])))

    # Quem entra é decidido pela relevância, com o id desempatando, como na busca.
    scored.sort(key=lambda r: (r[0], r[1].id), reverse=True)
    events = [event for _, event in scored[:limit]]

    decisions = {e.id: e for e in events if e.type == "decision"}
    if decisions:
        for row in conn.execute(TIMELINE_REVIEW_SQL, (list(decisions),)).fetchall():
            events.append(models.TimelineEvent(
                on=row["done_on"].isoformat(), type="review", id=row["id"],
                title=row["title"], decision_id=row["decision_id"], verdict=row["verdict"],
                notes=row["notes"], tags=decisions[row["decision_id"]].tags))

    events.sort(key=lambda e: (e.on, _EVENT_ORDER[e.type], e.id))
    return events, len(scored)


# Decisões relacionadas -------------------------------------------------------

RELATED_EVIDENCE_SQL = """
SELECT o.decision_id::text AS decision_id, e.id::text AS evidence_id, e.title,
       s.role::text AS role_here, o.role::text AS role_there
  FROM decision_evidence s
  JOIN decision_evidence o ON o.evidence_id = s.evidence_id AND o.decision_id <> s.decision_id
  JOIN evidence e ON e.id = s.evidence_id
 WHERE s.decision_id = %s
 ORDER BY e.id
"""

RELATED_LEARNING_SQL = """
SELECT o.decision_id::text AS decision_id, l.id::text AS learning_id, l.summary
  FROM decision_learning s
  JOIN decision_learning o ON o.learning_id = s.learning_id AND o.decision_id <> s.decision_id
  JOIN learning l ON l.id = s.learning_id
 WHERE s.decision_id = %s
 ORDER BY l.id
"""

RELATED_TAG_SQL = """
SELECT o.decision_id::text AS decision_id, t.name
  FROM decision_tag s
  JOIN decision_tag o ON o.tag_id = s.tag_id AND o.decision_id <> s.decision_id
  JOIN tag t ON t.id = s.tag_id
 WHERE s.decision_id = %s
 ORDER BY t.name
"""

RELATED_DECISION_SQL = """
SELECT d.id::text AS id, d.slug, d.title, d.decided_on, d.state::text AS state,
       pr.name AS project
  FROM decision d LEFT JOIN project pr ON pr.id = d.project_id
 WHERE d.id::text = ANY(%s)
"""


def related(conn, id: str | None, slug: str | None,
            limit: int) -> tuple[dict, list[models.RelatedDecision], int]:
    limit = max(1, min(int(limit), 30))
    source = _decision_row(conn, id, slug)
    did = source["id"]

    evidence: dict[str, list[models.SharedEvidence]] = {}
    for r in conn.execute(RELATED_EVIDENCE_SQL, (did,)).fetchall():
        evidence.setdefault(r["decision_id"], []).append(models.SharedEvidence(
            evidence_id=r["evidence_id"], title=r["title"], role_here=r["role_here"],
            role_there=r["role_there"]))
    learnings: dict[str, list[models.SharedLearning]] = {}
    for r in conn.execute(RELATED_LEARNING_SQL, (did,)).fetchall():
        learnings.setdefault(r["decision_id"], []).append(models.SharedLearning(
            learning_id=r["learning_id"], summary=r["summary"]))
    tags: dict[str, list[str]] = {}
    for r in conn.execute(RELATED_TAG_SQL, (did,)).fetchall():
        tags.setdefault(r["decision_id"], []).append(r["name"])

    ids = sorted(set(evidence) | set(learnings) | set(tags))
    items = [
        models.RelatedDecision(
            decision_id=d["id"], slug=d["slug"], title=d["title"],
            decided_on=d["decided_on"].isoformat(), state=d["state"], project=d["project"],
            shared_evidence=evidence.get(d["id"], []),
            shared_learnings=learnings.get(d["id"], []),
            shared_tags=tags.get(d["id"], []))
        for d in conn.execute(RELATED_DECISION_SQL, (ids,)).fetchall()
    ] if ids else []
    items.sort(key=related_order, reverse=True)
    return source, items[:limit], len(items)


def related_order(item: models.RelatedDecision) -> tuple:
    """Vínculos por evidência e lição, depois por tag, depois a data e o id.
    Nunca efeito (ADR 0003)."""
    return (len(item.shared_evidence) + len(item.shared_learnings), len(item.shared_tags),
            item.decided_on, item.decision_id)
