#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live gate for the Gemma 4 12B oMLX overlay candidate.

This gate intentionally targets the isolated overlay endpoint (default
127.0.0.1:18080), not production 8080.  It verifies the model can serve
normal MAGI work before it may replace the daytime E4B profile.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import importlib.util
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "gemma-4-12B-it-4bit"


@dataclass
class GateCase:
    name: str
    ok: bool
    elapsed_sec: float
    detail: str = ""
    expected: str = ""
    actual: str = ""


@dataclass
class GateReport:
    ok: bool
    generated_at: str
    server_url: str
    model: str
    cases: list[GateCase] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _request_json(url: str, payload: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _chat(server_url: str, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> tuple[dict[str, Any], float]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": kwargs.pop("temperature", 0.0),
        "max_tokens": kwargs.pop("max_tokens", 256),
    }
    payload.update(kwargs)
    started = time.time()
    obj = _request_json(f"{server_url.rstrip('/')}/v1/chat/completions", payload, timeout=240)
    return obj, time.time() - started


def _message(obj: dict[str, Any]) -> dict[str, Any]:
    return obj["choices"][0]["message"]


def _content(obj: dict[str, Any]) -> str:
    value = _message(obj).get("content")
    return value if isinstance(value, str) else ""


def _tool_name(obj: dict[str, Any]) -> str:
    calls = _message(obj).get("tool_calls") or []
    if not calls:
        return ""
    first = calls[0] or {}
    return str(((first.get("function") or {}).get("name")) or "")


def _add_case(report: GateReport, case: GateCase) -> None:
    report.cases.append(case)
    if not case.ok:
        report.failures.append(f"{case.name}: {case.detail or case.actual}")


def _run_case(
    report: GateReport,
    name: str,
    fn: Callable[[], tuple[bool, str, str, float]],
    expected: str,
) -> None:
    started = time.time()
    try:
        ok, actual, detail, elapsed = fn()
    except Exception as exc:
        ok = False
        actual = f"{type(exc).__name__}: {exc}"
        detail = actual
        elapsed = time.time() - started
    _add_case(
        report,
        GateCase(
            name=name,
            ok=ok,
            elapsed_sec=round(elapsed, 3),
            detail=detail,
            expected=expected,
            actual=actual[:1200],
        ),
    )


def _tools() -> list[dict[str, Any]]:
    def tool(name: str, description: str, prop: str = "query") -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {prop: {"type": "string"}},
                    "required": [prop],
                },
            },
        }

    return [
        tool("calendar_lookup", "查詢 Google 日曆或 OSC 行事曆", "date_text"),
        tool("weather_lookup", "查詢天氣", "location"),
        tool("laf_case_search", "查詢法律扶助案件、開辦、報結或官網附件", "query"),
        tool("file_review_lookup", "查詢閱卷、繳費單或卷宗下載狀態", "query"),
        tool("transcript_lookup", "查詢筆錄、庭期或筆錄待辦", "query"),
        tool("legal_db_search", "查詢台灣法院裁判與實務見解", "query"),
    ]


def run_gate(server_url: str, model: str, *, stress_count: int = 6, verify_overlay: bool = True) -> GateReport:
    report = GateReport(
        ok=False,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        server_url=server_url,
        model=model,
    )

    if verify_overlay:
        def verify() -> tuple[bool, str, str, float]:
            started = time.time()
            proc = subprocess.run(
                [sys.executable, str(MAGI_ROOT / "scripts" / "ops" / "prepare_omlx_gemma4_unified_runtime.py"), "--verify-only"],
                cwd=str(MAGI_ROOT),
                text=True,
                capture_output=True,
                timeout=120,
            )
            actual = (proc.stdout + proc.stderr).strip()
            return proc.returncode == 0 and '"detected_type": "vlm"' in actual, actual, actual, time.time() - started

        _run_case(report, "overlay_verify", verify, "Gemma4 unified overlay imports and detects 12B as VLM")

    def models() -> tuple[bool, str, str, float]:
        started = time.time()
        obj = _request_json(f"{server_url.rstrip('/')}/v1/models", timeout=20)
        ids = [str(item.get("id") or "") for item in obj.get("data", []) if isinstance(item, dict)]
        actual = ", ".join(ids)
        return model in ids, actual, actual, time.time() - started

    _run_case(report, "models_endpoint", models, f"/v1/models includes {model}")

    def zh_short() -> tuple[bool, str, str, float]:
        obj, elapsed = _chat(
            server_url,
            model,
            [{"role": "user", "content": "請用繁體中文一句話說明你已可正常運作。"}],
            max_tokens=80,
        )
        text = _content(obj)
        ok = "正常" in text and "已经" not in text
        return ok, text, text, elapsed

    _run_case(report, "zh_tw_short_answer", zh_short, "Traditional Chinese short answer")

    def legal_summary() -> tuple[bool, str, str, float]:
        obj, elapsed = _chat(
            server_url,
            model,
            [
                {"role": "system", "content": "你是台灣律師助理，回答必須使用繁體中文，不得自行新增日期。"},
                {"role": "user", "content": "請摘要：法院命被告於民國115年6月11日前提出答辯狀，逾期可能影響訴訟進行。"},
            ],
            max_tokens=180,
        )
        text = _content(obj)
        ok = all(token in text for token in ("115", "6月11", "答辯狀")) and "6月12" not in text
        return ok, text, text, elapsed

    _run_case(report, "legal_summary_fact_retention", legal_summary, "Keep deadline and document type")

    def heavy_translation() -> tuple[bool, str, str, float]:
        prompt = (
            "請用繁體中文翻譯下列英文法律研究句子，輸出三欄 Markdown 表格：原文、譯文、術語。"
            "每個法律或研究專有名詞在譯文後保留原文括號。"
            "術語表：Judicial interpreters=司法通譯；agency=能動性；responsibility=責任；jurors=國民法官；criminal trials=刑事審判。"
            "原文：Judicial interpreters influence jurors' impressions of agency and responsibility in criminal trials."
        )
        obj, elapsed = _chat(server_url, model, [{"role": "user", "content": prompt}], max_tokens=260)
        text = _content(obj)
        required = ["judicial interpreter", "agency", "responsibility"]
        ok = (
            "|" in text
            and all(term in text.lower() for term in required)
            and "司法通譯" in text
            and "能動性" in text
            and "司法解釋者" not in text
        )
        return ok, text, text, elapsed

    _run_case(report, "heavy_translation_term_preservation", heavy_translation, "Bilingual table preserves key terms")

    def deterministic_transcript_extractor() -> tuple[bool, str, str, float]:
        started = time.time()
        path = MAGI_ROOT / "skills" / "transcript-todo-extractor" / "action.py"
        spec = importlib.util.spec_from_file_location("_magi_transcript_todo_gate", path)
        if not spec or not spec.loader:
            raise RuntimeError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        items = mod.extract_candidates_from_pages(
            [(1, "法官諭本案候核辦，請辯護人於115年8月19日前提出聲請函詢事項。")],
            pdf_path=Path("/tmp/20260720 訊問筆錄.pdf"),
            transcript_date="2026-07-20",
            case_number="2025-0001",
            client_name="測試當事人",
        )
        actual = json.dumps([asdict(x) for x in items], ensure_ascii=False, indent=2)
        ok = (
            len(items) == 1
            and items[0].type == "提出"
            and items[0].date == "2026-08-19"
            and items[0].rule == "absolute_action_deadline"
            and "聲請函詢事項" in items[0].description
        )
        return ok, actual, actual, time.time() - started

    _run_case(report, "transcript_extractor_actionability", deterministic_transcript_extractor, "正式抽取器：候核辦本身不成為待辦；明確動作與期限才建立")

    def transcript_todo_reasoning() -> tuple[bool, str, str, float]:
        prompt = (
            "你是 OSC 筆錄待辦建立器。請只輸出 JSON。規則：『候核辦』只是程序狀態，本身不得建立日曆待辦；"
            "只有筆錄明確記載應做事項及期限時，才建立該具體待辦。"
            "開庭時有待辦但沒指定時間，應在下次庭前7個工作日設提醒。"
            "筆錄日期：2026-07-20。筆錄：法官諭本案候核辦，請辯護人於115年8月19日前提出聲請函詢事項。請判斷待辦。"
        )
        obj, elapsed = _chat(server_url, model, [{"role": "user", "content": prompt}], max_tokens=220)
        text = _content(obj)
        ok = "2026-08-19" in text and "提出" in text and "聲請函詢事項" in text and "庭期前" not in text
        return ok, text, text, elapsed

    _run_case(report, "transcript_todo_structured_rule", transcript_todo_reasoning, "候核辦 actionability rule understood")

    tool_cases = [
        ("tool_calendar_not_weather", "請查詢明天下午三點的行程。", "calendar_lookup"),
        ("tool_weather", "請查詢花蓮明天上午的天氣。", "weather_lookup"),
        ("tool_laf", "請查法扶王惠薰 1150529-E-005 的官網附件與開辦狀態。", "laf_case_search"),
        ("tool_file_review", "請查林建豐 115年度原交易字第21號繳費單是否已可下載。", "file_review_lookup"),
        ("tool_transcript", "請查劉信義最近筆錄是否有庭期或待辦。", "transcript_lookup"),
        ("tool_legal_db", "請查最高法院提到通譯的判決並分類。", "legal_db_search"),
    ]

    for name, prompt, expected_tool in tool_cases:
        def make_case(prompt: str = prompt, expected_tool: str = expected_tool) -> tuple[bool, str, str, float]:
            obj, elapsed = _chat(
                server_url,
                model,
                [{"role": "user", "content": prompt}],
                tools=_tools(),
                tool_choice="auto",
                max_tokens=160,
            )
            actual = _tool_name(obj)
            return actual == expected_tool, actual or json.dumps(_message(obj), ensure_ascii=False), actual, elapsed

        _run_case(report, name, make_case, expected_tool)

    def no_tool_summary() -> tuple[bool, str, str, float]:
        obj, elapsed = _chat(
            server_url,
            model,
            [{"role": "user", "content": "請摘要這句話：被告應於115年6月11日前提出答辯狀。"}],
            tools=_tools(),
            tool_choice="auto",
            max_tokens=120,
        )
        actual_tool = _tool_name(obj)
        text = _content(obj)
        ok = not actual_tool and "答辯狀" in text
        return ok, actual_tool or text, actual_tool or text, elapsed

    _run_case(report, "tool_no_false_positive_for_summary", no_tool_summary, "No tool call for pure summary")

    def long_context_deadline() -> tuple[bool, str, str, float]:
        facts = "\n".join(
            f"{i}. 測試事實 {i}：本段不是答案。" for i in range(1, 18)
        )
        facts += "\n18. 真正期限：民國115年6月11日前提出答辯狀。\n19. 不要輸出其他日期。"
        obj, elapsed = _chat(
            server_url,
            model,
            [{"role": "user", "content": f"請只輸出真正期限與事項。\n{facts}"}],
            max_tokens=120,
        )
        text = _content(obj)
        ok = "115" in text and "6月11" in text and "答辯狀" in text and "6月12" not in text
        return ok, text, text, elapsed

    _run_case(report, "long_context_fact_pick", long_context_deadline, "Pick correct deadline from longer prompt")

    def sequential_stress() -> tuple[bool, str, str, float]:
        started = time.time()
        latencies: list[float] = []
        for i in range(stress_count):
            obj, elapsed = _chat(
                server_url,
                model,
                [{"role": "user", "content": f"第{i + 1}次健康檢查：請只回答 OK。"}],
                max_tokens=16,
            )
            latencies.append(round(elapsed, 3))
            if "OK" not in _content(obj).upper():
                return False, json.dumps({"failed_at": i + 1, "latencies": latencies}, ensure_ascii=False), "bad response", time.time() - started
        actual = json.dumps({"count": stress_count, "latencies": latencies}, ensure_ascii=False)
        return True, actual, actual, time.time() - started

    _run_case(report, "sequential_stress", sequential_stress, f"{stress_count} sequential OK responses")

    report.ok = not report.failures
    return report


def write_report(report: GateReport, json_out: Path) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    json_out.write_text(payload + "\n", encoding="utf-8")
    txt = json_out.with_suffix(".txt")
    lines = [
        f"Gemma 4 12B overlay live gate: {'PASS' if report.ok else 'FAIL'}",
        f"model={report.model} server={report.server_url} generated={report.generated_at}",
        "",
    ]
    for case in report.cases:
        mark = "PASS" if case.ok else "FAIL"
        lines.append(f"- {mark} {case.name} ({case.elapsed_sec}s): {case.actual[:240]}")
    if report.failures:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in report.failures)
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Gemma 4 12B oMLX overlay live gate.")
    parser.add_argument("--server-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--stress-count", type=int, default=6)
    parser.add_argument("--json-out", default=str(MAGI_ROOT / ".runtime" / "gemma4_12b_overlay_live_gate_latest.json"))
    parser.add_argument("--skip-overlay-verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run_gate(
        args.server_url,
        args.model,
        stress_count=args.stress_count,
        verify_overlay=not args.skip_overlay_verify,
    )
    json_out = Path(args.json_out)
    write_report(report, json_out)
    payload = json.dumps(asdict(report), ensure_ascii=False, indent=2)
    print(payload if args.json else json_out.with_suffix(".txt").read_text(encoding="utf-8"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
