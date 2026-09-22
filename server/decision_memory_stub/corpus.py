"""Carga e consulta do corpus de fixtures.

O corpus é fixo e somente leitura. A busca é por relevância textual — nunca
por tamanho de efeito. Ordenar resultados por efeito compararia grandezas de
origens diferentes pela porta dos fundos, que é o que o ADR 0003 proíbe.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

def _fixtures_dir() -> Path:
    override = os.environ.get("DECISION_MEMORY_STUB_FIXTURES")
    return Path(override) if override else Path(__file__).resolve().parent.parent / "fixtures"


FIXTURES_DIR = _fixtures_dir()

# Palavras que não discriminam nada numa busca em português.
STOPWORDS = frozenset(
    """
    a as o os um uma uns umas de do da dos das em no na nos nas por para com sem
    e ou que se ao aos à às pelo pela como mais menos ja já nao não sobre entre
    """.split()
)


def _fold(text: str) -> str:
    """Minúsculas sem acento, para casar 'conversão' com 'conversao'."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def tokenize(text: str) -> set[str]:
    folded = _fold(text)
    raw = "".join(ch if ch.isalnum() else " " for ch in folded).split()
    return {word for word in raw if len(word) > 2 and word not in STOPWORDS}


def today() -> date:
    """Data de referência. Sobrescrevível para deixar os testes determinísticos."""
    override = os.environ.get("DECISION_MEMORY_STUB_TODAY")
    return date.fromisoformat(override) if override else date.today()


def _load(name: str) -> list[dict[str, Any]]:
    with (_fixtures_dir() / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class Corpus:
    people: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    learnings: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def person(self, person_id: str | None) -> dict[str, Any] | None:
        return next((p for p in self.people if p["id"] == person_id), None)

    def person_by_email(self, email: str) -> dict[str, Any] | None:
        target = email.strip().lower()
        return next((p for p in self.people if p["email"].lower() == target), None)

    def project(self, project_id: str | None) -> dict[str, Any] | None:
        return next((p for p in self.projects if p["id"] == project_id), None)

    def evidence_by_id(self, evidence_id: str) -> dict[str, Any] | None:
        return next((e for e in self.evidence if e["id"] == evidence_id), None)

    def evidence_by_source(self, system: str, external_id: str) -> dict[str, Any] | None:
        return next(
            (
                e
                for e in self.evidence
                if e.get("source_system") == system and e.get("external_id") == external_id
            ),
            None,
        )

    def decision_by_id(self, decision_id: str) -> dict[str, Any] | None:
        return next((d for d in self.decisions if d["id"] == decision_id), None)

    def decision_by_slug(self, slug: str) -> dict[str, Any] | None:
        return next((d for d in self.decisions if d["slug"] == slug), None)

    def learning_by_id(self, learning_id: str) -> dict[str, Any] | None:
        return next((ln for ln in self.learnings if ln["id"] == learning_id), None)

    def reviews_of(self, decision_id: str) -> list[dict[str, Any]]:
        return [r for r in self.reviews if r["decision_id"] == decision_id]

    def open_reviews(self) -> list[dict[str, Any]]:
        return [r for r in self.reviews if r.get("done_on") is None]

    def unattested_count(self) -> int:
        return sum(1 for d in self.decisions if d["state"] == "proposed") + sum(
            1 for ln in self.learnings if ln["state"] == "proposed"
        )

    def tags_of_decision(self, decision_id: str) -> set[str]:
        decision = self.decision_by_id(decision_id)
        return set(decision.get("tags", [])) if decision else set()


@lru_cache(maxsize=1)
def load_corpus() -> Corpus:
    return Corpus(
        people=_load("people"),
        projects=_load("projects"),
        evidence=_load("evidence"),
        decisions=_load("decisions"),
        learnings=_load("learnings"),
        reviews=_load("reviews"),
    )


def score(query_tokens: set[str], *texts: str, tags: list[str] | None = None) -> float:
    """Relevância textual pura: sobreposição de termos com título, resumo e tags.

    Deliberadamente não olha para efeito, magnitude nem nível de conformidade.
    """
    if not query_tokens:
        return 1.0
    haystack: set[str] = set()
    for text in texts:
        haystack |= tokenize(text or "")
    tag_tokens: set[str] = set()
    for tag in tags or []:
        tag_tokens |= tokenize(tag)
    hits = len(query_tokens & haystack)
    tag_hits = len(query_tokens & tag_tokens)
    if tag_hits == 0:
        # Uma palavra solta em comum não é relevância: sem casar tag, uma consulta
        # de três termos ou mais precisa de pelo menos duas. Sem isso, "programa de
        # fidelidade por pontos" traz uma decisão de retenção porque ela diz
        # "caiu dois pontos", e o corpus do experimento vira ruído.
        needed = 1 if len(query_tokens) <= 2 else 2
        if hits < needed:
            return 0.0
    # Casar uma tag pesa mais do que casar uma palavra solta do resumo.
    return hits + 2.0 * tag_hits
