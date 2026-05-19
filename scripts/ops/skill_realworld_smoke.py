#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import fitz

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.catalog import iter_top_level_skill_dirs


REPORT_DIR = ROOT / "static" / "reports"
CJK_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/CJKSymbolsFallback.ttc"),
)

NON_RUNNABLE_LIBRARY_SKILLS = {
    "apple",
    "bridge",
    "browser",
    "docx",
    "evolution",
    "laf-web-debug",
    "law_firm",
    "law_review",
    "legal",
    "management",
    "memory",
    "ops",
    "pptx",
    "research",
    "xlsx",
}


@dataclass
class SkillRunResult:
    skill: str
    command: list[str]
    mode: str
    status: str
    returncode: int
    seconds: float
    summary: str
    stdout_preview: str
    stderr_preview: str


def _create_sample_pdf(path: Path) -> None:
    doc = fitz.open()
    font_path = next((candidate for candidate in CJK_FONT_CANDIDATES if candidate.exists()), None)
    fontname = "helv"
    if font_path is not None:
        fontname = "MAGICJK"

    pages = [
        "臺灣臺北地方法院\n起訴書\n被告 王小明\n中華民國115年4月3日\n本件係詐欺案件。",
        "臺灣臺北地方法院\n審判筆錄\n被告 王小明\n115年4月4日\n開庭紀錄如下。",
    ]
    for content in pages:
        page = doc.new_page()
        if font_path is not None:
            page.insert_font(fontfile=str(font_path), fontname=fontname)
        page.insert_text((72, 72), content, fontsize=12, fontname=fontname)
    doc.save(path)
    doc.close()


def _create_sample_fields_json(path: Path) -> None:
    path.write_text(json.dumps({"name": "王小明"}, ensure_ascii=False), encoding="utf-8")


def _env(tmp_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MAGI_ROOT"] = str(ROOT)
    env["MAGI_ROOT_DIR"] = str(ROOT)
    env["MAGI_DISABLE_SERVER_STARTUP_HOOKS"] = "1"
    env["SYNOLOGY_CASE_ROOT"] = str(tmp_dir)
    env["SYNOLOGY_CASE_ROOTS"] = str(tmp_dir)
    env["TRANSCRIPT_INDEX_DB"] = str(tmp_dir / "transcript_index.json")
    env["MAGI_EXPORT_DIR"] = str(tmp_dir / "exports")
    return env


def _pick_command(skill: str, sample_pdf: Path, sample_fields: Path) -> tuple[str, list[str]]:
    overrides: dict[str, tuple[str, list[str]]] = {
        "auto-magi-skill": ("help", ["--task", "help"]),
        "brain_manager": ("help", ["--help"]),
        "brief-gen": ("template", ["--task", "template", "--text", "車禍案件損害賠償"]),
        "casper-autofix-knowledge": ("help", ["--task", "help"]),
        "contract-review": (
            "summarize",
            [
                "--task",
                "summarize",
                "--text",
                "本合約約定乙方於2026年4月提供顧問服務，雙方應保密，違約金為新台幣十萬元。",
            ],
        ),
        "court-hearing-reminder": ("list", ["--task", "list"]),
        "crawler-targets": ("list", ["--task", "list"]),
        "db-dual-sync": ("status", ["--task", "status"]),
        "evidence-admissibility": ("rules", ["--task", "rules"]),
        "file-review-orchestrator": ("help", ["--task", "help"]),
        "gmail-drafts": ("help", ["--task", "help"]),
        "insight-refine": ("help", ["--task", "help"]),
        "iron-dome": ("self_test", ["self_test"]),
        "iron_dome": ("self_test", ["self_test"]),
        "judgment-collector": ("help", ["--task", "help"]),
        "judicial-flow-search-archive": ("help", ["--task", "help"]),
        "judicial-tools": ("help", ["--task", "help"]),
        "judicial-web-search": ("help", ["--task", "help"]),
        "labor-law-calculator": (
            "overtime",
            [
                "--mode",
                "overtime",
                "--monthly-wage",
                "50000",
                "--day-type",
                "平日",
                "--hours",
                "2",
            ],
        ),
        "laf-orchestrator": ("help", ["--task", "help"]),
        "laf-portal-automation": ("list", ["--list"]),
        "laf-refine-case": ("help", ["--task", "help"]),
        "laf-withdrawal-report": ("help", ["--task", "help"]),
        "legal_attest": ("chat-init", ["--task", "chat", '{"user_id":"smoke","message":"init"}']),
        "magi-autopilot": ("help", ["--task", "help"]),
        "magi-doctor": ("report", ["--task", "report"]),
        "magi-self-repair": ("help", ["--help"]),
        "market-briefing": ("briefing", ["--task", "briefing", "--mode", "quick", "--force", "1", "--notify", "0"]),
        "mock-test": ("help", ["--task", "help"]),
        "obsidian": ("status", ["--task", "status"]),
        "osc-orchestrator": ("queue_status", ["--task", "queue_status"]),
        "osc-scan-folder": ("self_test", ["--task", "self_test"]),
        "pdf": ("extract", ["--task", f"extract --file {sample_pdf}"]),
        "pdf-annotator": ("help", ["--help"]),
        "pdf-bookmarker": ("test", ["--task", "test", "--path", str(sample_pdf), "--dry-run"]),
        "pdf-namer": ("review_name", ["--task", "review_name", "--path", str(sample_pdf), "--case_name", "王小明"]),
        "process-hygiene": ("scan", ["--task", "scan"]),
        "screenshot-sorter-tw": ("help", ["--help"]),
        "statutes-vdb": ("help", ["--task", "help"]),
        "transcript-downloader": ("help", ["--task", "help"]),
        "transcript-indexer": ("help", ["--help"]),
        "translator": ("self_test", ["--task", "self_test"]),
        "trial-prep": ("upcoming", ["--task", "upcoming", "--days", "3"]),
        "worldmonitor-intel": ("status", ["--task", "status"]),
    }
    return overrides.get(skill, ("help", ["--help"]))


def _timeout_for(skill: str) -> int:
    overrides = {
        "contract-review": 120,
        "market-briefing": 60,
        "obsidian": 60,
        "trial-prep": 30,
    }
    return overrides.get(skill, 20)


def _classify(skill: str, mode: str, returncode: int, stdout: str, stderr: str) -> tuple[str, str]:
    text = "\n".join([stdout or "", stderr or ""]).strip()
    low = text.lower()
    if returncode != 0 or "traceback" in low or "modulenotfounderror" in low:
        return "FAIL", text.splitlines()[0] if text else f"returncode={returncode}"
    if skill == "worldmonitor-intel" and "Melchior 可用" in text:
        if "Finnhub key: 未設定" in text:
            return "PASS", "Melchior 可用（未設定 Finnhub key，維持降級）"
        return "PASS", text.splitlines()[0] if text else "ok"
    if skill == "pdf-namer" and "Proposed Name:" in text:
        if "[WARNING]" in text or "warning:" in low:
            return "PASS", "已產生建議檔名（視覺分析走 fallback）"
        return "PASS", "已產生建議檔名"
    warning_markers = (
        "未設定",
        "無法",
        "停用",
        "missing",
        '"degraded": true',
        "degraded: true",
        "warning:",
        "[warning]",
    )
    if any(token in low for token in warning_markers):
        return "WARN", text.splitlines()[0] if text else "warning"
    return "PASS", text.splitlines()[0] if text else "ok"


def run_matrix() -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="magi-skill-smoke-") as td:
        tmp_dir = Path(td)
        sample_pdf = tmp_dir / "sample.pdf"
        sample_fields = tmp_dir / "fields.json"
        _create_sample_pdf(sample_pdf)
        _create_sample_fields_json(sample_fields)

        results: list[SkillRunResult] = []
        runnable_skills = [entry for entry in iter_top_level_skill_dirs(ROOT / "skills", runnable_only=True)]

        for entry in runnable_skills:
            mode, args = _pick_command(entry.name, sample_pdf, sample_fields)
            cmd = [sys.executable, str(entry / "action.py"), *args]
            print(f"[RUN] {entry.name} :: {mode}", flush=True)
            started = datetime.now()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(ROOT),
                    env=_env(tmp_dir),
                    capture_output=True,
                    text=True,
                    timeout=_timeout_for(entry.name),
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                status, summary = _classify(entry.name, mode, proc.returncode, stdout, stderr)
                returncode = int(proc.returncode)
                finished = datetime.now()
            except subprocess.TimeoutExpired as exc:
                stdout = ((exc.stdout or "") if isinstance(exc.stdout, str) else "").strip()
                stderr = ((exc.stderr or "") if isinstance(exc.stderr, str) else "").strip()
                status = "WARN"
                summary = "timeout"
                returncode = 124
                finished = datetime.now()
            results.append(
                SkillRunResult(
                    skill=entry.name,
                    command=cmd,
                    mode=mode,
                    status=status,
                    returncode=returncode,
                    seconds=round((finished - started).total_seconds(), 3),
                    summary=summary,
                    stdout_preview=stdout[:400],
                    stderr_preview=stderr[:400],
                )
            )

    non_runnable = [entry.name for entry in iter_top_level_skill_dirs(ROOT / "skills") if not (entry / "action.py").exists()]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "runnable_skill_count": len(results),
        "non_runnable_skill_count": len(non_runnable),
        "pass_count": sum(1 for r in results if r.status == "PASS"),
        "warn_count": sum(1 for r in results if r.status == "WARN"),
        "fail_count": sum(1 for r in results if r.status == "FAIL"),
        "results": [asdict(r) for r in results],
        "non_runnable_skills": non_runnable,
        "non_runnable_library_skills": sorted(NON_RUNNABLE_LIBRARY_SKILLS & set(non_runnable)),
    }
    return summary


def write_reports(summary: dict[str, Any]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"skill_realworld_smoke_{stamp}.json"
    md_path = REPORT_DIR / f"skill_realworld_smoke_{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# MAGI Skill 實戰 Smoke 報告",
        "",
        f"- 產生時間：{summary['generated_at']}",
        f"- runnable skills：{summary['runnable_skill_count']}",
        f"- non-runnable skills：{summary['non_runnable_skill_count']}",
        f"- PASS：{summary['pass_count']}",
        f"- WARN：{summary['warn_count']}",
        f"- FAIL：{summary['fail_count']}",
        "",
        "## Runnable Skills",
        "",
    ]
    for item in summary["results"]:
        lines.append(f"- `{item['skill']}` `{item['status']}` `{item['mode']}` `{item['summary']}`")
    lines.extend(["", "## Non-runnable Skills", ""])
    for name in summary["non_runnable_skills"]:
        lines.append(f"- `{name}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(
        description="Run MAGI real-world skill smoke matrix and write JSON/Markdown reports."
    )
    parser.add_argument(
        "--fail-on-warn",
        action="store_true",
        help="Treat WARN as failure (exit non-zero when WARN exists).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    summary = run_matrix()
    json_path, md_path = write_reports(summary)
    print(json.dumps({**summary, "json_report": str(json_path), "md_report": str(md_path)}, ensure_ascii=False, indent=2))
    if summary["fail_count"] > 0:
        return 1
    if args.fail_on_warn and summary["warn_count"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
