"""Public-release placeholder for removed legal-research integrations."""

from __future__ import annotations

from api.legal_workflow import append_workflow_footer, detect_legal_workflow, workflow_prompt_block


_PUBLIC_DISABLED = (
    "This public MAGI release does not include legal-research collection, "
    "case-law lookup, or opinion-library integrations. Configure your own "
    "legal source adapter before enabling live legal research."
)


def _public_workflow_notice(message: str) -> str:
    workflow = detect_legal_workflow(text=message, mode="legal")
    guidance = workflow_prompt_block(workflow)
    base = f"{_PUBLIC_DISABLED}\n\n{guidance}".strip()
    return append_workflow_footer(base, workflow, tool_used=False)


def extract_judgment_collect_payload(message: str) -> tuple[dict | None, str]:
    return None, _public_workflow_notice(message)


def format_judgment_collect_result(payload: dict) -> str:
    return _public_workflow_notice("")


def run_judgment_collector_command(orch, message: str, notify: bool = False) -> str:
    return _public_workflow_notice(message)


def run_judgment_trend_command(orch, message: str) -> str:
    return _public_workflow_notice(message)
