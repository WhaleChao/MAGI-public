"""Client id helpers shared by OSC, web routes, and LAF automation."""
from __future__ import annotations

import re
from typing import Any, Iterable

_CLIENT_ID_RE = re.compile(r"^C(\d{4,6})$")


def is_canonical_client_id(value: Any) -> bool:
    return bool(_CLIENT_ID_RE.fullmatch(str(value or "").strip()))


def next_client_id_from_existing(values: Iterable[Any]) -> str:
    """Return the next original OSC client id (C0001, C0002, ...).

    Older bugs created UUID/webc ids and random hex-like ids such as
    C775F05FA or C7023687.  Those must not influence the OSC sequence.
    """
    max_seq = 0
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("id")
        match = _CLIENT_ID_RE.fullmatch(str(value or "").strip())
        if not match:
            continue
        max_seq = max(max_seq, int(match.group(1)))
    return f"C{max_seq + 1:04d}"


def generate_next_client_id() -> str:
    """Read the live OSC DB and generate the next canonical client id."""
    from api.osc.utils import _osc_exec

    rows, _ = _osc_exec("SELECT id FROM clients WHERE id LIKE 'C%'", fetch="all")
    return next_client_id_from_existing(rows or [])
