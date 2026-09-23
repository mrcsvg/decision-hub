"""Consultas de leitura: busca, decisão e revisões pendentes."""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import models
from .config import today
from .guard import fold

STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem
    e ou que se ao aos à às pelo pela como mais menos ja já nao não sobre entre
    """.split()
)

EVIDENCE_STATE = """
CASE WHEN EXISTS (SELECT 1 FROM provenance p WHERE p.object_type = 'evidence'
                   AND p.object_id = e.id AND p.attested_at IS NOT NULL)
     THEN 'attested' ELSE 'proposed' END
"""

# Termos em OR: a relevância é decidida em Python, com a mesma regra do stub.
_HITS = "(SELECT count(*) FROM unnest(%(terms)s::text[]) term WHERE {alias}.search @@ to_tsquery('simple', term))"
_CANDIDATE = "(%(any)s::text IS NULL OR {alias}.search @@ to_tsquery('simple', %(any)s) OR {tags} && %(folded)s::text[])"

_EV_TAGS = "ARRAY(SELECT t.name FROM evidence_tag x JOIN tag t ON t.id = x.tag_id WHERE x.evidence_id = e.id)"
_LN_TAGS = "ARRAY(SELECT t.name FROM learning_tag x JOIN tag t ON t.id = x.tag_id WHERE x.learning_id = l.id)"
_DC_TAGS = "ARRAY(SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id WHERE x.decision_id = d.id)"

SEARCH_EVIDENCE_SQL = f"""
SELECT e.id::text AS id, e.title, e.summary, e.strength::text AS strength,
       e.source_system, e.external_id, e.url, {_EV_TAGS} AS tags,
       {_HITS.format(alias="e")} AS hits, {EVIDENCE_STATE} AS state
  FROM evidence e
 WHERE {_CANDIDATE.format(alias="e", tags=_EV_TAGS)}
   AND (%(kinds)s::text[] IS NULL OR e.kind::text = ANY(%(kinds)s))
   AND (%(source_system)s::text IS NULL OR e.source_system = %(source_system)s)
   AND (%(tags)s::text[] IS NULL OR {_EV_TAGS} && %(tags)s::text[])
"""

SEARCH_LEARNING_SQL = f"""
SELECT l.id::text AS id, l.summary AS title, NULL AS summary, l.state::text AS state,
       {_LN_TAGS} AS tags, {_HITS.format(alias="l")} AS hits
  FROM learning l
 WHERE {_CANDIDATE.format(alias="l", tags=_LN_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_LN_TAGS} && %(tags)s::text[])
"""

SEARCH_DECISION_SQL = f"""
SELECT d.id::text AS id, d.title, d.description AS summary, d.state::text AS state,
       {_DC_TAGS} AS tags, {_HITS.format(alias="d")} AS hits
  FROM decision d
 WHERE {_CANDIDATE.format(alias="d", tags=_DC_TAGS)}
   AND (%(tags)s::text[] IS NULL OR {_DC_TAGS} && %(tags)s::text[])
"""


def terms(text: str) -> list[str]:
    """Palavras da consulta, em minúsculas e com acento (o dicionário 'simple'
    guarda o acento), sem stopwords e sem repetição."""
    raw = "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
    out: list[str] = []
    for word in raw:
        if len(word) > 2 and fold(word) not in STOPWORDS and word not in out:
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


def search(conn, query: str, tags: list[str] | None, kinds: list[str] | None,
           source_system: str | None, include: list[str] | None,
           limit: int) -> tuple[list[models.SearchItem], int]:
    include = include or ["evidence", "learning", "decision"]
    if kinds or source_system:
        # Tipo e origem são atributos de evidência; pedir por eles restringe a busca.
        include = ["evidence"]
    limit = max(1, min(int(limit), 50))
    wanted_tags = [fold(tag) for tag in (tags or [])]
    words = terms(query) + [w for w in wanted_tags if w not in terms(query)]
    folded = [fold(w) for w in words]
    params: dict[str, Any] = {
        "terms": words, "folded": folded,
        "any": " | ".join(words) or None,
        "kinds": kinds or None, "source_system": source_system,
        "tags": wanted_tags or None,
    }

    scored: list[tuple[float, models.SearchItem]] = []

    def keep(row: dict, item: models.SearchItem) -> None:
        tag_hits = len(set(folded) & set(row["tags"]))
        score = relevance(len(words), row["hits"], tag_hits)
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


def get_decision(conn, id: str | None, slug: str | None) -> models.DecisionData:
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
         WHERE d.id = %s OR d.slug = %s
         LIMIT 1
        """,
        (_as_uuid(id), slug),
    ).fetchone()
    if row is None:
        raise ToolError(
            f"Decisão não encontrada: {id or slug}. Use search_evidence para localizá-la."
        )
    did = row["id"]

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
            "FROM review WHERE decision_id = %s ORDER BY due_on", (did,)).fetchall()
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
         ORDER BY pv.created_at LIMIT 1
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
           AND (%(project)s::text IS NULL OR pr.name ILIKE '%%' || %(project)s || '%%')
        """,
        {"owner": owner_email, "project": project},
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
    reviews.sort(key=lambda r: r.overdue_days, reverse=True)

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
