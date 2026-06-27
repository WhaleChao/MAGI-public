"""Core helpers for reusing prior OSC pleading DOCX files.

The web route layer is intentionally kept out of this module.  Callers pass a
source DOCX plus source/target case dictionaries; this module copies the file,
replaces case-specific header/footer/table/body text, and saves a new DOCX
without touching the source.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DOCX_SUFFIX = ".docx"
_WORD_TEMP_PREFIX = "~$"
_OWN_PLEADING_FOLDER_MARKER = "我方歷次書狀"

_PLEADING_MARKERS = (
    "書狀",
    "起訴狀",
    "答辯狀",
    "準備狀",
    "聲請狀",
    "陳報狀",
    "抗告狀",
    "上訴狀",
    "辯論意旨",
    "刑事附帶民事",
)


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "court_case_no": (
        "court_case_no",
        "court_case_number",
        "court_case",
        "court_no",
        "court_no_text",
        "judicial_case_no",
        "judicial_case_number",
        "case_no",
        "case_no_text",
        "case.court_case_no",
        "case.court_case_number",
        "court.case_no",
        "court.case_number",
        "法院案號",
    ),
    "internal_case_no": (
        "case_number",
        "internal_case_no",
        "internal_case_number",
        "osc_case_no",
        "osc_case_number",
        "magi_case_no",
        "magi_case_number",
        "paperclip_case_no",
        "paperclip_case_number",
        "case.internal_case_no",
        "case.case_number",
        "內部案號",
    ),
    "division": (
        "court_division",
        "division",
        "court_branch",
        "branch",
        "assigned_division",
        "case_division",
        "case.court_division",
        "court.division",
        "股別",
    ),
    "client": (
        "client",
        "client_name",
        "party_name",
        "principal",
        "case.client_name",
        "parties.client",
        "parties.client_name",
        "當事人",
        "委任人",
    ),
    "opponent": (
        "opponent",
        "opponent_name",
        "counterparty",
        "counterparty_name",
        "opposing_party",
        "opposing_party_name",
        "case.opponent_name",
        "parties.opponent",
        "parties.opponent_name",
        "對造",
        "相對人",
    ),
    "plaintiff": (
        "plaintiff",
        "plaintiff_name",
        "claimant",
        "claimant_name",
        "parties.plaintiff",
        "parties.plaintiff_name",
        "原告",
        "聲請人",
    ),
    "defendant": (
        "defendant",
        "defendant_name",
        "respondent",
        "respondent_name",
        "parties.defendant",
        "parties.defendant_name",
        "被告",
        "相對人",
    ),
    "institution": (
        "court_name",
        "court",
        "institution",
        "institution_name",
        "agency",
        "agency_name",
        "organ",
        "organ_name",
        "prosecutor_office",
        "prosecution_office",
        "prosecutors_office",
        "district_prosecutors_office",
        "case.court_name",
        "court.name",
        "法院",
        "地檢署",
        "機關",
    ),
    "case_reason": (
        "case_reason",
        "reason",
        "case_cause",
        "cause",
        "matter",
        "subject",
        "case.reason",
        "case.case_reason",
        "案由",
    ),
}


@dataclass(frozen=True)
class ReplacementRule:
    field: str
    source: str
    target: str


def index_pleading_docx(
    root_dir: str | os.PathLike[str],
    *,
    recursive: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return reusable DOCX pleading candidates under ``root_dir``.

    The route layer can use this for the "all pleadings" index.  Results are
    conservative: Word temporary files are skipped, only ``.docx`` files are
    returned, and only files under ``我方歷次書狀`` are indexed.  This prevents
    similarly named Word files such as 委任狀 from polluting the reuse list.
    """

    root = Path(root_dir).expanduser()
    if not root.exists() or not root.is_dir():
        return []

    pattern = "**/*" if recursive else "*"
    candidates: list[dict[str, Any]] = []
    for path in root.glob(pattern):
        if not path.is_file():
            continue
        if path.suffix.lower() != DOCX_SUFFIX or path.name.startswith(_WORD_TEMP_PREFIX):
            continue
        marker_text = str(path.relative_to(root))
        if _OWN_PLEADING_FOLDER_MARKER not in marker_text:
            continue
        if _PLEADING_MARKERS and not any(marker in marker_text for marker in _PLEADING_MARKERS):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        candidates.append(
            {
                "path": str(path),
                "file_name": path.name,
                "stem": path.stem,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": "pleading",
            }
        )

    candidates.sort(key=lambda item: (float(item.get("mtime") or 0), item.get("file_name") or ""), reverse=True)
    if limit is not None and limit >= 0:
        return candidates[:limit]
    return candidates


def build_pleading_index(
    root_dir: str | os.PathLike[str],
    *,
    recursive: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Compatibility alias for route code that wants an explicit index name."""

    return index_pleading_docx(root_dir, recursive=recursive, limit=limit)


def reuse_docx_document(
    source_path: str | os.PathLike[str],
    source_case: Mapping[str, Any] | None,
    target_case: Mapping[str, Any] | None,
    *,
    overrides: Mapping[str, Any] | None = None,
    suggested_filename: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a new DOCX by replacing source-case data with target-case data.

    ``overrides`` may contain target field overrides using the same aliases as
    ``target_case``.  It may also contain ``{"replacements": {"old": "new"}}``
    for explicit literal replacements, plus optional ``suggested_filename`` and
    ``output_dir`` values for route payload convenience.
    """

    source = Path(source_path).expanduser()
    _validate_docx_source(source)

    effective_overrides = dict(overrides or {})
    if suggested_filename is None:
        suggested_filename = _clean_scalar(effective_overrides.get("suggested_filename"))
    if output_dir is None and effective_overrides.get("output_dir"):
        output_dir = effective_overrides.get("output_dir")

    out_dir = Path(output_dir).expanduser() if output_dir else source.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = _unique_output_path(out_dir, suggested_filename or _default_filename(source, target_case))

    rules = _build_replacement_rules(source_case or {}, target_case or {}, effective_overrides)

    try:
        from docx import Document  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional install state
        raise RuntimeError("python-docx is required to reuse DOCX files") from exc

    doc = Document(str(source))
    counts = _replace_in_document(doc, rules)
    doc.save(str(out_path))

    replacements = [
        {
            "field": rule.field,
            "source": rule.source,
            "target": rule.target,
            "count": counts.get((rule.field, rule.source, rule.target), 0),
        }
        for rule in rules
    ]

    return {
        "output_path": str(out_path),
        "file_name": out_path.name,
        "replacements": replacements,
        "replacement_count": sum(item["count"] for item in replacements),
        "source_path": str(source),
    }


def reuse_document(
    source_path: str | os.PathLike[str],
    source_case: Mapping[str, Any] | None,
    target_case: Mapping[str, Any] | None,
    *,
    overrides: Mapping[str, Any] | None = None,
    suggested_filename: str | None = None,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for callers that do not want DOCX in the name."""

    return reuse_docx_document(
        source_path,
        source_case,
        target_case,
        overrides=overrides,
        suggested_filename=suggested_filename,
        output_dir=output_dir,
    )


def _validate_docx_source(source: Path) -> None:
    if source.suffix.lower() != DOCX_SUFFIX:
        raise ValueError("Only .docx source files are supported")
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    if source.name.startswith(_WORD_TEMP_PREFIX):
        raise ValueError("Word temporary .docx files are not supported")


def _default_filename(source: Path, target_case: Mapping[str, Any] | None) -> str:
    case_no = _first_value(target_case or {}, _FIELD_ALIASES["internal_case_no"])
    court_no = _first_value(target_case or {}, _FIELD_ALIASES["court_case_no"])
    suffix = case_no or court_no
    if suffix:
        return f"{source.stem}-{_sanitize_filename(suffix)}{DOCX_SUFFIX}"
    return f"{source.stem}-重製{DOCX_SUFFIX}"


def _unique_output_path(output_dir: Path, suggested_filename: str) -> Path:
    name = _sanitize_filename(suggested_filename)
    path = Path(name)
    if path.name != name:
        name = path.name
    if not name:
        name = f"reused-document{DOCX_SUFFIX}"
    candidate = output_dir / name
    if candidate.suffix:
        if candidate.suffix.lower() != DOCX_SUFFIX:
            raise ValueError("Only .docx output files are supported")
    else:
        candidate = candidate.with_suffix(DOCX_SUFFIX)

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 10000):
        numbered = output_dir / f"{stem} ({index}){suffix}"
        if not numbered.exists():
            return numbered
    raise FileExistsError(f"Could not find a free output filename for {candidate.name}")


def _sanitize_filename(name: Any) -> str:
    text = _clean_scalar(name)
    if not text:
        return ""
    text = text.replace("\\", "/").split("/")[-1]
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = re.sub(r"[:*?\"<>|]", "_", text)
    return text.strip().strip(".")


def _build_replacement_rules(
    source_case: Mapping[str, Any],
    target_case: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> list[ReplacementRule]:
    effective_target = _merged_target_case(target_case, overrides)
    rules: list[ReplacementRule] = []
    seen: set[tuple[str, str, str]] = set()

    for field, aliases in _FIELD_ALIASES.items():
        source_values = _values_from_case(source_case, aliases)
        target_values = _values_from_case(effective_target, aliases)
        for source_value, target_value in _pair_values(source_values, target_values):
            if not _usable_source_text(source_value, field) or not target_value or source_value == target_value:
                continue
            key = (field, source_value, target_value)
            if key in seen:
                continue
            seen.add(key)
            rules.append(ReplacementRule(field=field, source=source_value, target=target_value))

    explicit = overrides.get("replacements") if isinstance(overrides, Mapping) else None
    if isinstance(explicit, Mapping):
        for old, new in explicit.items():
            source_value = _clean_scalar(old)
            target_value = _clean_scalar(new)
            if not _usable_source_text(source_value, "explicit") or not target_value or source_value == target_value:
                continue
            key = ("explicit", source_value, target_value)
            if key in seen:
                continue
            seen.add(key)
            rules.append(ReplacementRule(field="explicit", source=source_value, target=target_value))

    rules.sort(key=lambda rule: len(rule.source), reverse=True)
    return rules


def _merged_target_case(target_case: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(target_case or {})
    ignored = {"replacements", "suggested_filename", "output_dir"}
    for key, value in (overrides or {}).items():
        if key in ignored:
            continue
        if _clean_scalar(value) or isinstance(value, (list, tuple, set, dict)):
            merged[key] = value
    return merged


def _pair_values(source_values: list[str], target_values: list[str]) -> Iterable[tuple[str, str]]:
    if not source_values or not target_values:
        return ()
    if len(source_values) == len(target_values):
        return zip(source_values, target_values)
    if len(target_values) == 1:
        return ((source_value, target_values[0]) for source_value in source_values)
    return zip(source_values, target_values)


def _values_from_case(case: Mapping[str, Any], aliases: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        for raw in _lookup_alias_values(case, alias):
            for value in _flatten_values(raw):
                text = _clean_scalar(value)
                if text and text not in seen:
                    seen.add(text)
                    values.append(text)
    return values


def _first_value(case: Mapping[str, Any], aliases: tuple[str, ...]) -> str:
    values = _values_from_case(case, aliases)
    return values[0] if values else ""


def _lookup_alias_values(case: Mapping[str, Any], alias: str) -> list[Any]:
    direct: list[Any] = []
    if alias in case:
        direct.append(case[alias])

    lower_alias = alias.lower()
    for key, value in case.items():
        if isinstance(key, str) and key.lower() == lower_alias and key != alias:
            direct.append(value)

    if "." not in alias:
        return direct

    cur: Any = case
    for part in alias.split("."):
        if not isinstance(cur, Mapping):
            return direct
        if part in cur:
            cur = cur[part]
            continue
        lower_part = part.lower()
        match = None
        for key, value in cur.items():
            if isinstance(key, str) and key.lower() == lower_part:
                match = value
                break
        if match is None:
            return direct
        cur = match
    direct.append(cur)
    return direct


def _flatten_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        flattened: list[Any] = []
        for key in ("name", "value", "text", "label", "case_no", "case_number"):
            if key in value:
                flattened.extend(_flatten_values(value[key]))
        return flattened
    if isinstance(value, (list, tuple, set)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_values(item))
        return flattened
    return (value,)


def _clean_scalar(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ").strip()
    return re.sub(r"\s+", " ", text)


def _usable_source_text(value: str, field: str) -> bool:
    if not value:
        return False
    if field == "division":
        return len(value) >= 1 and (len(value) >= 2 or value.endswith("股"))
    if field == "explicit":
        return len(value) >= 1
    return len(value) >= 2


def _replace_in_document(doc: Any, rules: list[ReplacementRule]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    if not rules:
        return counts

    seen_containers: set[int] = set()
    for container in _document_containers(doc):
        marker = id(getattr(container, "_element", container))
        if marker in seen_containers:
            continue
        seen_containers.add(marker)
        for paragraph in _iter_paragraphs(container):
            paragraph_counts = _replace_in_paragraph(paragraph, rules)
            for key, count in paragraph_counts.items():
                counts[key] = counts.get(key, 0) + count
    return counts


def _document_containers(doc: Any) -> Iterable[Any]:
    yield doc
    for section in getattr(doc, "sections", []):
        for attr in (
            "header",
            "first_page_header",
            "even_page_header",
            "footer",
            "first_page_footer",
            "even_page_footer",
        ):
            try:
                yield getattr(section, attr)
            except Exception:
                continue


def _iter_paragraphs(container: Any) -> Iterable[Any]:
    for paragraph in getattr(container, "paragraphs", []):
        yield paragraph

    seen_cells: set[int] = set()
    for table in getattr(container, "tables", []):
        yield from _iter_table_paragraphs(table, seen_cells)


def _iter_table_paragraphs(table: Any, seen_cells: set[int]) -> Iterable[Any]:
    for row in getattr(table, "rows", []):
        for cell in getattr(row, "cells", []):
            marker = id(getattr(cell, "_tc", cell))
            if marker in seen_cells:
                continue
            seen_cells.add(marker)
            for paragraph in getattr(cell, "paragraphs", []):
                yield paragraph
            for nested in getattr(cell, "tables", []):
                yield from _iter_table_paragraphs(nested, seen_cells)


def _replace_in_paragraph(paragraph: Any, rules: list[ReplacementRule]) -> dict[tuple[str, str, str], int]:
    text_nodes = _paragraph_text_nodes(paragraph)
    if not text_nodes:
        return {}

    original = "".join(node.text or "" for node in text_nodes)
    if not original:
        return {}

    replaced = original
    counts: dict[tuple[str, str, str], int] = {}
    for rule in rules:
        count = replaced.count(rule.source)
        if count <= 0:
            continue
        replaced = replaced.replace(rule.source, rule.target)
        counts[(rule.field, rule.source, rule.target)] = counts.get((rule.field, rule.source, rule.target), 0) + count

    if replaced == original:
        return {}

    text_nodes[0].text = replaced
    for node in text_nodes[1:]:
        node.text = ""
    return counts


def _paragraph_text_nodes(paragraph: Any) -> list[Any]:
    element = getattr(paragraph, "_p", None)
    if element is None:
        return []
    try:
        return list(element.xpath(".//w:t"))
    except Exception:
        return []
