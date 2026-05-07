#!/usr/bin/env python3
"""Benchmark Gemma 4 target models with optional MTP draft metadata.

This script intentionally works against any OpenAI-compatible local endpoint.
The current oMLX server may ignore or reject draft fields, so baseline runs are
still useful today; MTP-capable sidecars can reuse the same task files later.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

try:
    from api.model_config import MTP_BLOCK_SIZE, MTP_DRAFT_KIND, resolve_draft_model
    from api.routing.service_registry import get_service_url
except Exception:
    MTP_BLOCK_SIZE = 4
    MTP_DRAFT_KIND = "mtp"

    def resolve_draft_model(target_model: str = "") -> str:
        target = str(target_model or "").lower()
        return "gemma-4-26B-A4B-it-assistant-bf16" if "26b" in target or "a4b" in target else "gemma-4-E4B-it-assistant-bf16"

    def get_service_url(name: str, *, path: str = "") -> str:
        base = "http://127.0.0.1:8080"
        return base.rstrip("/") + ("/" + path.lstrip("/") if path else "")


DEFAULT_OUT_DIR = Path(".magi_benchmarks/gemma4_mtp")


@dataclass(frozen=True)
class BenchmarkTask:
    name: str
    category: str
    messages: list[dict[str, Any]]
    expect_json: bool = False
    max_tokens: int = 512
    temperature: float = 0.2


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_tasks(path: Path, *, limit: int = 0) -> list[BenchmarkTask]:
    tasks: list[BenchmarkTask] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSONL: {exc}") from exc
        messages = raw.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError(f"{path}:{lineno}: messages must be a non-empty list")
        tasks.append(
            BenchmarkTask(
                name=str(raw.get("name") or f"task_{lineno}"),
                category=str(raw.get("category") or "general"),
                messages=[m for m in messages if isinstance(m, dict)],
                expect_json=_as_bool(raw.get("expect_json")),
                max_tokens=int(raw.get("max_tokens") or 512),
                temperature=float(raw.get("temperature") if raw.get("temperature") is not None else 0.2),
            )
        )
        if limit and len(tasks) >= limit:
            break
    return tasks


def extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is not None:
            return str(content)
        text = choices[0].get("text")
        if text is not None:
            return str(text)
    return ""


def valid_json_text(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
    try:
        json.loads(stripped)
        return True
    except Exception:
        return False


def build_payload(
    task: BenchmarkTask,
    *,
    model: str,
    draft_model: str = "",
    draft_kind: str = MTP_DRAFT_KIND,
    draft_block_size: int = MTP_BLOCK_SIZE,
    send_draft_fields: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": task.messages,
        "temperature": task.temperature,
        "max_tokens": task.max_tokens,
    }
    if draft_model and send_draft_fields:
        payload.update(
            {
                "draft_model": draft_model,
                "draft_kind": draft_kind,
                "draft_block_size": int(draft_block_size),
            }
        )
    return payload


def chat_once(base_url: str, payload: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/chat/completions"
    started = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        elapsed = time.perf_counter() - started
        body = resp.json() if resp.text else {}
        if resp.status_code >= 400:
            return {"ok": False, "elapsed_sec": elapsed, "status_code": resp.status_code, "error": body or resp.text}
        content = extract_content(body)
        usage = body.get("usage") or {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        return {
            "ok": bool(content),
            "elapsed_sec": elapsed,
            "status_code": resp.status_code,
            "content": content,
            "usage": usage,
            "extra_metrics": body.get("magi_mlx") or {},
            "tokens_per_sec": (completion_tokens / elapsed) if completion_tokens and elapsed > 0 else 0.0,
        }
    except Exception as exc:
        return {"ok": False, "elapsed_sec": time.perf_counter() - started, "status_code": 0, "error": str(exc)}


def probe_runtime() -> dict[str, Any]:
    probe: dict[str, Any] = {"omlx_serve_has_draft_flag": False, "mlx_vlm_available": False}
    try:
        help_result = subprocess.run(["omlx", "serve", "--help"], check=False, capture_output=True, text=True, timeout=20)
        help_text = (help_result.stdout or "") + (help_result.stderr or "")
        probe["omlx_serve_has_draft_flag"] = "draft" in help_text.lower()
        probe["omlx_serve_help_rc"] = help_result.returncode
    except Exception as exc:
        probe["omlx_serve_error"] = str(exc)
    try:
        import importlib.util

        probe["mlx_vlm_available"] = importlib.util.find_spec("mlx_vlm") is not None
    except Exception as exc:
        probe["mlx_vlm_error"] = str(exc)
    return probe


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok_results = [r for r in results if r.get("ok")]
    elapsed = [float(r.get("elapsed_sec") or 0) for r in ok_results]
    tps = [float(r.get("tokens_per_sec") or 0) for r in ok_results if float(r.get("tokens_per_sec") or 0) > 0]
    json_tasks = [r for r in results if r.get("expect_json")]
    return {
        "total": len(results),
        "ok": len(ok_results),
        "failed": len(results) - len(ok_results),
        "median_elapsed_sec": statistics.median(elapsed) if elapsed else None,
        "median_tokens_per_sec": statistics.median(tps) if tps else None,
        "json_success": sum(1 for r in json_tasks if r.get("json_valid")),
        "json_total": len(json_tasks),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    tasks = load_tasks(Path(args.tasks), limit=int(args.limit or 0))
    draft_model = args.draft_model or (resolve_draft_model(args.model) if args.variant == "mtp" else "")
    results: list[dict[str, Any]] = []
    for task in tasks:
        payload = build_payload(
            task,
            model=args.model,
            draft_model=draft_model,
            draft_kind=args.draft_kind,
            draft_block_size=args.draft_block_size,
            send_draft_fields=not args.no_send_draft_fields,
        )
        if args.dry_run:
            result = {"ok": True, "elapsed_sec": 0.0, "content": "", "tokens_per_sec": 0.0}
        else:
            result = chat_once(args.base_url, payload, timeout=args.timeout)
        content = str(result.get("content") or "")
        row = {
            "name": task.name,
            "category": task.category,
            "expect_json": task.expect_json,
            "json_valid": valid_json_text(content) if task.expect_json and content else False,
            "request_payload_keys": sorted(payload.keys()),
            **result,
        }
        results.append(row)
        status = "ok" if row.get("ok") else "fail"
        print(f"{status:>4} {task.name:<28} {float(row.get('elapsed_sec') or 0):6.2f}s")

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "variant": args.variant,
        "model": args.model,
        "draft_model": draft_model,
        "base_url": args.base_url,
        "runtime_probe": probe_runtime() if args.probe_runtime else None,
        "summary": summarize(results),
        "results": results,
    }
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    default_base = get_service_url("omlx_inference").rstrip("/") + "/v1"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True, help="JSONL task file")
    parser.add_argument("--model", default=os.environ.get("MAGI_TEXT_PRIMARY_MODEL") or "gemma-4-e4b-it-4bit")
    parser.add_argument("--variant", choices=["baseline", "mtp"], default="baseline")
    parser.add_argument("--base-url", default=default_base, help="OpenAI-compatible /v1 base URL")
    parser.add_argument("--draft-model", default="")
    parser.add_argument("--draft-kind", default=MTP_DRAFT_KIND)
    parser.add_argument("--draft-block-size", type=int, default=MTP_BLOCK_SIZE)
    parser.add_argument("--no-send-draft-fields", action="store_true", help="Label as MTP without adding draft_* request fields")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-runtime", action="store_true")
    parser.add_argument("--output", default="", help="Output JSON path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report = run_benchmark(args)
    out_path = Path(args.output) if args.output else DEFAULT_OUT_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.variant}_{args.model}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"saved: {out_path}")
    return 0 if report["summary"]["failed"] == 0 or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
