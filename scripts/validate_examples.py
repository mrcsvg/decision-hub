#!/usr/bin/env python3
"""Valida spec/examples/*.json contra o contrato de ingestão.

JSON Schema 2020-12 com checagem de formato ativa: `date`, `email` e `uri`
são verificados de fato, não apenas anotados. Sem os extras de formato
instalados (`pip install 'jsonschema[format]'`) a validação passaria vazia,
então o script aborta se algum formato usado pelo contrato não tiver
verificador registrado.

Uso: python scripts/validate_examples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("jsonschema não instalado. Rode: pip install 'jsonschema[format]'")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "spec" / "experiment-record-v0.schema.json"
EXAMPLES_DIR = ROOT / "spec" / "examples"
REQUIRED_FORMATS = ("date", "email", "uri")


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def describe(error) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "(raiz)"
    return f"    {location}: {error.message}"


def main() -> int:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)

    format_checker = Draft202012Validator.FORMAT_CHECKER
    missing = [name for name in REQUIRED_FORMATS if name not in format_checker.checkers]
    if missing:
        sys.exit(
            "checagem de formato inativa para: "
            + ", ".join(missing)
            + ". Rode: pip install 'jsonschema[format]'"
        )

    validator = Draft202012Validator(schema, format_checker=format_checker)

    examples = sorted(EXAMPLES_DIR.glob("*.json"))
    if not examples:
        sys.exit(f"nenhum exemplo encontrado em {EXAMPLES_DIR.relative_to(ROOT)}/")

    failed = 0
    for example in examples:
        name = example.relative_to(ROOT)
        errors = sorted(validator.iter_errors(load_json(example)), key=lambda e: list(e.absolute_path))
        if errors:
            failed += 1
            print(f"FALHOU {name}")
            for error in errors:
                print(describe(error))
        else:
            print(f"ok     {name}")

    if failed:
        print(f"\n{failed} de {len(examples)} exemplos violam o contrato.")
        return 1

    print(f"\n{len(examples)} exemplos válidos contra {SCHEMA_PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
