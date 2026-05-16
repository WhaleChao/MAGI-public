#!/usr/bin/env python3
"""MAGI skill wrapper for Supreme Court interpreter empirical classification."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parent
MAGI_ROOT = SKILL_DIR.parents[1]
CLASSIFIER_PATH = MAGI_ROOT / "scripts" / "classify_supreme_interpreter_mentions.py"
DEFAULT_OUT_DIR = MAGI_ROOT / ".runtime" / "interpreter_empirical_classifier"


def _load_classifier():
    spec = importlib.util.spec_from_file_location("magi_supreme_interpreter_classifier", CLASSIFIER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load classifier: {CLASSIFIER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("success") else 1


def _parse_task(task_text: str) -> dict[str, str]:
    text = (task_text or "").strip()
    if not text or text in {"help", "--help", "-h"}:
        return {"task": "help"}
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items() if v is not None}

    out: dict[str, str] = {"task": text.split()[0]}
    for key, value in re.findall(r"(\w+)=((?:\"[^\"]+\"|'[^']+'|\S+))", text):
        out[key] = value.strip("\"'")
    return out


def _txt_count(input_dir: Path) -> int:
    return sum(1 for _ in input_dir.glob("*.txt")) if input_dir.exists() else 0


def _pdf_count(input_dir: Path) -> int:
    pdf_dir = input_dir.parent / "PDF"
    return sum(1 for _ in pdf_dir.glob("*.pdf")) if pdf_dir.exists() else 0


def status() -> dict[str, Any]:
    classifier = _load_classifier()
    input_dir = classifier.resolve_input_dir("")
    return {
        "success": True,
        "task": "status",
        "input_dir": str(input_dir),
        "txt_count": _txt_count(input_dir),
        "pdf_count": _pdf_count(input_dir),
        "outputs": {
            "csv": str(input_dir / "最高法院_通譯_分類表.csv"),
            "xlsx": str(input_dir / "最高法院_通譯_分類表.xlsx"),
            "md": str(input_dir / "最高法院_通譯_分類表.md"),
        },
    }


def classify(input_dir: str = "", output_prefix: str = "") -> dict[str, Any]:
    classifier = _load_classifier()
    source = classifier.resolve_input_dir(input_dir)
    if not source.exists():
        return {"success": False, "error": "input_dir_not_found", "input_dir": str(source)}

    txt_files = sorted(source.glob("*.txt"))
    if not txt_files:
        return {"success": False, "error": "no_txt_files", "input_dir": str(source)}

    if output_prefix:
        prefix = Path(output_prefix).expanduser()
    else:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = DEFAULT_OUT_DIR / f"最高法院_通譯_分類表_{stamp}"
    prefix.parent.mkdir(parents=True, exist_ok=True)

    rows = [classifier.classify_file(path) for path in txt_files]
    rows = classifier.dedupe_cases(rows)
    rows.sort(key=lambda row: (row.date, row.court_no), reverse=True)

    csv_path = prefix.with_suffix(".csv")
    md_path = prefix.with_suffix(".md")
    xlsx_path = prefix.with_suffix(".xlsx")
    classifier.write_csv(rows, csv_path)
    classifier.write_markdown(rows, md_path)
    classifier.write_xlsx(rows, xlsx_path)

    marker_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    for row in rows:
        marker_counts[row.interpreter_marker] = marker_counts.get(row.interpreter_marker, 0) + 1
        category_counts[row.primary_category] = category_counts.get(row.primary_category, 0) + 1

    return {
        "success": True,
        "task": "classify",
        "input_dir": str(source),
        "txt_count": len(txt_files),
        "classified": len(rows),
        "marker_counts": marker_counts,
        "category_counts": category_counts,
        "outputs": {
            "csv": str(csv_path),
            "xlsx": str(xlsx_path),
            "md": str(md_path),
        },
    }


def self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="magi_interpreter_classifier_") as tmp:
        base = Path(tmp)
        (base / "0001_sample.txt").write_text(
            "裁判字號：最高法院 114 年度台抗字第 1 號刑事裁定\n"
            "裁判日期：民國 114 年 1 月 1 日\n"
            "裁判案由：再審\n\n主文\n抗告駁回。\n理由\n"
            "原判決所憑之證言、鑑定或通譯已證明其為虛偽者，得聲請再審。\n",
            encoding="utf-8",
        )
        (base / "0002_sample.txt").write_text(
            "裁判字號：最高法院 114 年度台上字第 2 號刑事判決\n"
            "裁判日期：民國 114 年 1 月 2 日\n"
            "裁判案由：違反毒品危害防制條例\n\n主文\n上訴駁回。\n理由\n"
            "上訴人主張警詢時未經通譯傳譯，且通譯並未如實翻譯，譯文與真意不符。\n",
            encoding="utf-8",
        )
        result = classify(str(base), str(base / "out"))
        if not result.get("success"):
            return result
        csv_text = Path(result["outputs"]["csv"]).read_text(encoding="utf-8-sig")
        ok = "僅條文引用" in csv_text and "實質通譯爭點" in csv_text and "【通譯】" in csv_text
        return {
            "success": ok,
            "task": "self_test",
            "detail": "classifier generated csv/xlsx/md and marked legal-template vs substantive interpreter issue",
            "outputs": result.get("outputs"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="最高法院通譯判決實證研究分類 skill")
    parser.add_argument("--task", default="help")
    args, extra = parser.parse_known_args()
    task_text = args.task if args.task != "help" or not extra else " ".join(extra)
    parsed = _parse_task(task_text)
    task = parsed.get("task", "help")

    try:
        if task in {"help", "說明"}:
            return _emit({
                "success": True,
                "tasks": ["status", "self_test", "classify"],
                "examples": [
                    "classify",
                    "classify input_dir=/path/to/TXT output_prefix=/path/to/out",
                    "{\"task\":\"classify\",\"input_dir\":\"/path/to/TXT\"}",
                ],
            })
        if task == "status":
            return _emit(status())
        if task == "self_test":
            return _emit(self_test())
        if task in {"classify", "分類", "判決實證研究分類"}:
            return _emit(classify(parsed.get("input_dir", ""), parsed.get("output_prefix", "")))
        return _emit({"success": False, "error": f"unknown_task:{task}", "supported": ["status", "self_test", "classify"]})
    except Exception as exc:
        return _emit({"success": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    raise SystemExit(main())
