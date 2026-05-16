#!/usr/bin/env python3
"""MAGI skill wrapper for Supreme Court interpreter empirical classification."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parent
MAGI_ROOT = SKILL_DIR.parents[1]
CLASSIFIER_PATH = MAGI_ROOT / "scripts" / "classify_supreme_interpreter_mentions.py"
JUDICIAL_SEARCH_PATH = MAGI_ROOT / "skills" / "judicial-web-search" / "action.py"
DEFAULT_OUT_DIR = MAGI_ROOT / ".runtime" / "interpreter_empirical_classifier"


def _load_classifier():
    spec = importlib.util.spec_from_file_location("magi_supreme_interpreter_classifier", CLASSIFIER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load classifier: {CLASSIFIER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_judicial_search():
    spec = importlib.util.spec_from_file_location("magi_judicial_web_search_for_interpreter", JUDICIAL_SEARCH_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load judicial web search skill: {JUDICIAL_SEARCH_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("success") else 1


def _parse_task(task_text: str) -> dict[str, Any]:
    text = (task_text or "").strip()
    if not text or text in {"help", "--help", "-h"}:
        return {"task": "help"}
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): v for k, v in data.items() if v is not None}

    out: dict[str, Any] = {"task": text.split()[0]}
    for key, value in re.findall(r"(\w+)=((?:\"[^\"]+\"|'[^']+'|\S+))", text):
        out[key] = value.strip("\"'")
    return out


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是"}


def _to_int(value: Any, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        out = int(value)
    except Exception:
        out = default
    out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _to_float(value: Any, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        out = float(value)
    except Exception:
        out = default
    out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    return [part.strip() for part in re.split(r"[,，、|/]+", raw) if part.strip()]


def _safe_name(value: str, fallback: str = "judgment", limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = text.strip(" ._")
    return (text or fallback)[:limit]


def _derive_query(keyword: str, courts: list[str]) -> tuple[str, list[str]]:
    raw = re.sub(r"[＆&＋+]+", " ", (keyword or "").strip())
    out_courts = list(courts)
    if "最高法院" in raw and "最高法院" not in out_courts:
        out_courts.append("最高法院")
    search_keywords = raw
    for court in out_courts:
        if court:
            search_keywords = search_keywords.replace(court, " ")
    search_keywords = re.sub(r"\s+", " ", search_keywords).strip()
    return search_keywords, out_courts


def _resolve_fetch_dirs(output_dir: str, keyword: str) -> tuple[Path, Path]:
    if output_dir:
        base = Path(output_dir).expanduser()
    else:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = DEFAULT_OUT_DIR / f"fetch_{_safe_name(keyword, 'keyword', limit=48)}_{stamp}"
    txt_dir = base if base.name.upper() == "TXT" else base / "TXT"
    txt_dir.mkdir(parents=True, exist_ok=True)
    return base if base.name.upper() != "TXT" else base.parent, txt_dir


def _read_full_search_results(search_result: dict[str, Any]) -> list[dict[str, Any]]:
    results = list(search_result.get("results") or [])
    path = str(search_result.get("results_path") or "").strip()
    if path:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            full = payload.get("results")
            if isinstance(full, list) and len(full) >= len(results):
                results = full
        except Exception:
            pass
    return [item for item in results if isinstance(item, dict)]


def _clean_fetched_text(text: str, title: str = "") -> str:
    body = (text or "").replace("\ufeff", "").replace("\u3000", " ")
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.strip()
    if title and "裁判字號：" not in body[:500]:
        body = f"裁判字號：{title}\n\n{body}"
    return body


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


def fetch_keyword(
    keyword: str = "",
    court: Any = "",
    output_dir: str = "",
    max_results: Any = 100,
    timeout_sec: Any = 60,
    delay_sec: Any = 0.2,
    headless: Any = True,
    max_chars: Any = 500000,
    force: Any = False,
    jws_module: Any | None = None,
) -> dict[str, Any]:
    raw_keyword = (keyword or "").strip()
    courts = _split_values(court)
    search_keywords, courts = _derive_query(raw_keyword, courts)
    if not search_keywords and not courts:
        return {"success": False, "error": "missing_keyword", "task": "fetch"}

    max_n = _to_int(max_results, 100, minimum=1, maximum=5000)
    timeout = _to_int(timeout_sec, 60, minimum=10, maximum=600)
    delay = _to_float(delay_sec, 0.2, minimum=0.0, maximum=10.0)
    max_text_chars = _to_int(max_chars, 500000, minimum=1000, maximum=5000000)
    do_force = _to_bool(force, False)
    do_headless = _to_bool(headless, True)
    base_dir, txt_dir = _resolve_fetch_dirs(output_dir, raw_keyword or search_keywords)

    jws = jws_module or _load_judicial_search()
    if hasattr(jws, "_search_http_impl"):
        search_result = jws._search_http_impl(
            keywords=search_keywords,
            max_results=max_n,
            timeout_sec=timeout,
            courts=courts or None,
        )
    else:
        search_result = jws._search_impl(
            keywords=search_keywords,
            max_results=max_n,
            headless=do_headless,
            timeout_sec=timeout,
            courts=courts or None,
        )
    if not search_result.get("success") and "playwright" in str(search_result.get("error", "")).lower() and hasattr(jws, "_search_http_impl"):
        search_result = jws._search_http_impl(
            keywords=search_keywords,
            max_results=max_n,
            timeout_sec=timeout,
            courts=courts or None,
        )
    if not search_result.get("success"):
        return {
            "success": False,
            "task": "fetch",
            "error": search_result.get("error", "search_failed"),
            "query": {"keyword": raw_keyword, "search_keywords": search_keywords, "courts": courts},
            "search": search_result,
        }

    results = _read_full_search_results(search_result)[:max_n]
    seen_urls: set[str] = set()
    fetched: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for idx, item in enumerate(results, start=1):
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or f"result_{idx}").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        filename = f"{idx:04d}_{_safe_name(title, f'result_{idx}')}.txt"
        dest = txt_dir / filename
        if dest.exists() and not do_force:
            fetched.append({
                "index": idx,
                "title": title,
                "url": url,
                "file": str(dest),
                "status": "exists",
            })
            continue
        fetch_result = jws._fetch_text_impl(url, headless=do_headless, timeout_sec=timeout, max_chars=max_text_chars)
        if not fetch_result.get("success"):
            failed.append({
                "index": idx,
                "title": title,
                "url": url,
                "error": fetch_result.get("error", "fetch_failed"),
            })
            continue
        text_path = Path(str(fetch_result.get("text_path") or "")).expanduser()
        try:
            text = text_path.read_text(encoding="utf-8", errors="replace")
            text = _clean_fetched_text(text, title=title)
            dest.write_text(text, encoding="utf-8")
            fetched.append({
                "index": idx,
                "title": title,
                "url": url,
                "file": str(dest),
                "text_chars": len(text),
                "engine": fetch_result.get("engine", ""),
                "status": "fetched",
            })
        except Exception as exc:
            failed.append({
                "index": idx,
                "title": title,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
            })
        if delay and idx < len(results):
            time.sleep(delay)

    report = {
        "success": bool(fetched),
        "task": "fetch",
        "query": {"keyword": raw_keyword, "search_keywords": search_keywords, "courts": courts},
        "output_dir": str(base_dir),
        "txt_dir": str(txt_dir),
        "search_total_count": search_result.get("total_count"),
        "search_count": search_result.get("count"),
        "results_seen": len(results),
        "fetched_count": len(fetched),
        "failed_count": len(failed),
        "fetched": fetched,
        "failed": failed,
        "search": {
            "engine": search_result.get("engine"),
            "pages_scanned": search_result.get("pages_scanned"),
            "results_path": search_result.get("results_path"),
            "incomplete": search_result.get("incomplete"),
        },
    }
    (base_dir / "fetch_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def fetch_and_classify(**kwargs: Any) -> dict[str, Any]:
    if not str(kwargs.get("keyword") or "").strip() and not kwargs.get("court"):
        prefix_raw = str(kwargs.get("output_prefix") or "").strip()
        return classify("", prefix_raw)
    fetch_keys = {
        "keyword", "court", "output_dir", "max_results", "timeout_sec",
        "delay_sec", "headless", "max_chars", "force", "jws_module",
    }
    fetch_result = fetch_keyword(**{k: v for k, v in kwargs.items() if k in fetch_keys})
    if not fetch_result.get("success"):
        return fetch_result
    base_dir = Path(str(fetch_result.get("output_dir") or DEFAULT_OUT_DIR)).expanduser()
    txt_dir = Path(str(fetch_result.get("txt_dir") or base_dir / "TXT")).expanduser()
    prefix_raw = str(kwargs.get("output_prefix") or "").strip()
    prefix = prefix_raw or str(base_dir / "最高法院_通譯_分類表")
    class_result = classify(str(txt_dir), prefix)
    return {
        "success": bool(class_result.get("success")),
        "task": "fetch_and_classify",
        "fetch": fetch_result,
        "classification": class_result,
        "outputs": class_result.get("outputs", {}),
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
                "tasks": ["status", "self_test", "fetch", "classify", "fetch_and_classify"],
                "examples": [
                    "fetch keyword=\"最高法院 通譯\" max_results=50",
                    "fetch_and_classify keyword=\"最高法院 通譯\" max_results=50 output_dir=/tmp/interpreter",
                    "classify",
                    "classify input_dir=/path/to/TXT output_prefix=/path/to/out",
                    "{\"task\":\"fetch_and_classify\",\"keyword\":\"最高法院 通譯\",\"max_results\":50}",
                ],
            })
        if task == "status":
            return _emit(status())
        if task == "self_test":
            return _emit(self_test())
        if task in {"fetch", "抓取", "搜尋抓取"}:
            return _emit(fetch_keyword(
                keyword=str(parsed.get("keyword") or parsed.get("keywords") or parsed.get("query") or ""),
                court=parsed.get("court") or parsed.get("courts") or "",
                output_dir=str(parsed.get("output_dir") or ""),
                max_results=parsed.get("max_results", 100),
                timeout_sec=parsed.get("timeout_sec", 60),
                delay_sec=parsed.get("delay_sec", 0.2),
                headless=parsed.get("headless", True),
                max_chars=parsed.get("max_chars", 500000),
                force=parsed.get("force", False),
            ))
        if task in {"fetch_and_classify", "抓取並分類", "搜尋並分類", "上網抓取並分類"}:
            return _emit(fetch_and_classify(
                keyword=str(parsed.get("keyword") or parsed.get("keywords") or parsed.get("query") or ""),
                court=parsed.get("court") or parsed.get("courts") or "",
                output_dir=str(parsed.get("output_dir") or ""),
                output_prefix=str(parsed.get("output_prefix") or ""),
                max_results=parsed.get("max_results", 100),
                timeout_sec=parsed.get("timeout_sec", 60),
                delay_sec=parsed.get("delay_sec", 0.2),
                headless=parsed.get("headless", True),
                max_chars=parsed.get("max_chars", 500000),
                force=parsed.get("force", False),
            ))
        if task in {"classify", "分類", "判決實證研究分類"}:
            return _emit(classify(parsed.get("input_dir", ""), parsed.get("output_prefix", "")))
        return _emit({"success": False, "error": f"unknown_task:{task}", "supported": ["status", "self_test", "fetch", "classify", "fetch_and_classify"]})
    except Exception as exc:
        return _emit({"success": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    raise SystemExit(main())
