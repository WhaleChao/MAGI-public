from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class ContractValidationError(ValueError):
    """Raised with stable, compact validation messages for CI output."""


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_json(instance: Any, schema: dict[str, Any], *, label: str = "payload") -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    if not errors:
        return
    messages = []
    for error in errors[:12]:
        where = ".".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"{where}: {error.message}")
    if len(errors) > 12:
        messages.append(f"... and {len(errors) - 12} more")
    raise ContractValidationError(f"{label} failed schema validation: " + "; ".join(messages))


def validate_json_file(path: str | Path, schema_path: str | Path) -> Any:
    instance = load_json(path)
    validate_json(instance, load_json(schema_path), label=str(path))
    return instance
