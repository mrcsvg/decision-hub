#!/usr/bin/env python3
"""Verifica se os links relativos dos arquivos .md apontam para algo que existe.

Cobre links inline `[texto](alvo)`, imagens e definições de referência
`[rótulo]: alvo`. Links externos, `mailto:` e âncoras puras são ignorados.
Quando o alvo é um `.md` com fragmento, a âncora também é conferida contra
os títulos do arquivo destino — link para seção renomeada é link quebrado.

Blocos de código cercados não são verificados: o que está dentro deles é
exemplo, não navegação.

Uso: python scripts/check_links.py
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
INLINE_LINK_RE = re.compile(
    r"!?\[(?:[^\[\]\\]|\\.)*\]\(\s*(<[^>\n]*>|[^\s()]*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
REFERENCE_DEF_RE = re.compile(r"^\s{0,3}\[(?:[^\[\]\\]|\\.)+\]:\s*(<[^>\n]*>|\S+)")
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
ATX_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def strip_fenced_blocks(lines: list[str]) -> list[tuple[int, str]]:
    """Devolve (número da linha, conteúdo) fora de blocos de código cercados."""
    kept: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0]
                continue
            kept.append((number, line))
        elif match and match.group(1)[0] == fence:
            fence = None
    return kept


def slugify(heading: str) -> str:
    """Reproduz a âncora que o GitHub gera para um título."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*~]", "", text)
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    text = unicodedata.normalize("NFC", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for _, line in strip_fenced_blocks(lines):
        match = ATX_HEADING_RE.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        if not slug:
            continue
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return anchors


def targets_in(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str]] = []
    for number, line in strip_fenced_blocks(lines):
        raw = [match.group(1) for match in INLINE_LINK_RE.finditer(line)]
        definition = REFERENCE_DEF_RE.match(line)
        if definition:
            raw.append(definition.group(1))
        for target in raw:
            target = target.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            if target:
                found.append((number, target))
    return found


def check(path: Path, line: int, target: str) -> str | None:
    if EXTERNAL_RE.match(target) or target.startswith("#"):
        return None

    path_part, _, fragment = target.partition("#")
    path_part = path_part.split("?", 1)[0]
    if not path_part:
        return None

    base = ROOT if path_part.startswith("/") else path.parent
    resolved = (base / path_part.lstrip("/")).resolve()

    if not resolved.exists():
        return f"alvo inexistente: {target}"
    if fragment and resolved.is_file() and resolved.suffix == ".md":
        if fragment.lower() not in anchors_of(resolved):
            return f"âncora inexistente em {path_part}: #{fragment}"
    return None


def main() -> int:
    documents = sorted(
        candidate
        for candidate in ROOT.rglob("*.md")
        if ".git" not in candidate.relative_to(ROOT).parts
    )
    if not documents:
        sys.exit("nenhum arquivo .md encontrado.")

    broken = 0
    checked = 0
    for document in documents:
        name = document.relative_to(ROOT)
        for line, target in targets_in(document):
            if EXTERNAL_RE.match(target) or target.startswith("#"):
                continue
            checked += 1
            problem = check(document, line, target)
            if problem:
                broken += 1
                print(f"FALHOU {name}:{line}: {problem}")

    if broken:
        print(f"\n{broken} de {checked} links relativos estão quebrados.")
        return 1

    print(f"{checked} links relativos verificados em {len(documents)} arquivos .md; todos resolvem.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
