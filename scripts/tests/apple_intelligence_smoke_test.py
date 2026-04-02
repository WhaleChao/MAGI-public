# -*- coding: utf-8 -*-
"""
Apple Intelligence / Apple on-device 協作冒煙測試
=================================================

目的（台灣用語）：
- 確認 CASPER/MAGI 的「PDF 判讀、改檔名、摘要、音訊轉文字」會嘗試呼叫 Apple 能力協作。

注意：
- 「摘要」與「音檔轉文字」目前以 Shortcuts 觸發 Apple Intelligence/系統能力為主，
  需要你先在「捷徑」App 建立捷徑：
  - MAGI 摘要
  - MAGI 音檔轉文字
  參考：<MAGI_ROOT>/skills/apple/SETUP_SHORTCUTS.md

輸出：
- 會把結果寫到 <MAGI_ROOT>/reports/apple_intelligence_smoke_test_*.txt
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parent.parent.parent)))
sys.path.insert(0, str(MAGI_ROOT))


def _run(cmd: list[str], timeout: int = 90) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": r.returncode == 0,
            "rc": r.returncode,
            "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "stdout": "", "stderr": f"timeout({timeout}s)"}
    except Exception as e:
        return {"ok": False, "rc": -2, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def _pick_default_files() -> dict:
    # PDF：優先用現成的測試檔
    pdf_candidates = [
        MAGI_ROOT / ".cache/osc_flow_case_status/tmp_pdftest_35007/test.pdf",
    ]
    img_candidates = [
        MAGI_ROOT / "test_image.png",
        MAGI_ROOT / "test_image.jpg",
    ]
    audio_candidates = [
        MAGI_ROOT / "test_audio.aiff",
        MAGI_ROOT / "test_audio.wav",
    ]

    def _first_existing(paths):
        for p in paths:
            if p.exists():
                return str(p)
        return ""

    return {
        "pdf": _first_existing(pdf_candidates),
        "image": _first_existing(img_candidates),
        "audio": _first_existing(audio_candidates),
    }


def main(argv: list[str]) -> int:
    defaults = _pick_default_files()
    pdf_path = (argv[1] if len(argv) > 1 else defaults["pdf"]).strip()
    image_path = (argv[2] if len(argv) > 2 else defaults["image"]).strip()
    audio_path = (argv[3] if len(argv) > 3 else defaults["audio"]).strip()

    from skills.apple import apple_intelligence as ai

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "inputs": {"pdf": pdf_path, "image": image_path, "audio": audio_path},
        "shortcuts": ai.shortcuts_status(),
        "tests": {},
    }

    # 1) PDF 判讀（Quartz）
    report["tests"]["pdf_quartz"] = ai.extract_pdf_text_quartz(pdf_path, max_pages=3)
    if report["tests"]["pdf_quartz"].get("success"):
        t = report["tests"]["pdf_quartz"].get("text") or ""
        report["tests"]["pdf_quartz"]["_preview"] = t[:160]
        report["tests"]["pdf_quartz"]["_len"] = len(t)

    # 2) OCR（Vision）
    report["tests"]["ocr_vision"] = ai.ocr_image_vision(image_path)
    if report["tests"]["ocr_vision"].get("success"):
        t = report["tests"]["ocr_vision"].get("text") or ""
        report["tests"]["ocr_vision"]["_preview"] = t[:160]
        report["tests"]["ocr_vision"]["_len"] = len(t)

    # 3) 摘要（Apple Intelligence via Shortcuts）
    # Writing Tools 在某些系統版本下對「選取範圍/文字長度」有下限，文字太短可能會報：
    # 「無法使用『書寫工具』，嘗試較長的所選範圍。」
    sample_text = (
        "以下是一段測試用長文字，請協助整理成 5 點重點摘要，並保留關鍵名詞：\n"
        "（1）本段文字用來驗證 Apple Intelligence 的寫作工具是否能被捷徑呼叫。\n"
        "（2）若摘要功能成功，應回傳一段可讀的條列重點，而不是跳出選單或要求手動輸入。\n"
        "（3）本測試不涉及任何送出或對外傳輸，只是本機端處理。\n"
        "（4）如果你看到要求選擇「生成摘要/製作重點摘要」之類的對話框，代表捷徑仍含互動步驟，需要改成固定流程。\n"
        "（5）最後，請輸出摘要結果為純文字。\n"
    )
    report["tests"]["summarize_shortcuts"] = ai.summarize_text_apple_intelligence(sample_text, timeout_sec=60)

    # 4) 音訊轉文字（Apple Intelligence/Shortcuts）
    report["tests"]["stt_shortcuts"] = ai.transcribe_audio(audio_path, engine="apple_intelligence")

    # 5) 改檔名鏈路（輕量測試）：只驗證 pdf-namer 的取字引擎會選到 apple_quartz
    # 避免跑完整 analyze 觸發 LLM/遠端模型造成測試卡住。
    pdf_namer = MAGI_ROOT / "skills/pdf-namer/action.py"
    if pdf_namer.exists() and pdf_path and os.path.exists(pdf_path):
        try:
            import importlib.util

            os.environ["MAGI_PDF_TEXT_ENGINE"] = "apple"
            spec = importlib.util.spec_from_file_location("magi_pdf_namer_action", str(pdf_namer))
            mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            text, engine = mod.extract_text(pdf_path)
            report["tests"]["pdf_namer_text_engine"] = {
                "ok": True,
                "engine": engine,
                "text_len": len(text or ""),
                "_preview": (text or "")[:160],
            }
        except Exception as e:
            report["tests"]["pdf_namer_text_engine"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    else:
        report["tests"]["pdf_namer_text_engine"] = {"ok": False, "error": "missing pdf-namer or pdf input"}

    # 6) Tools API（/summarize, /collab/transcribe）確認 API 也會嘗試 Apple
    import urllib.request

    def _post_json(url: str, payload: dict, timeout: int = 20) -> dict:
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return {"ok": True, "status": resp.status, "body": body[:4000]}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return {"ok": False, "status": int(getattr(e, "code", 0) or 0), "body": body[:4000], "error": f"HTTPError: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    report["tests"]["tools_api_summarize_engine_apple"] = _post_json(
        "http://127.0.0.1:5003/summarize",
        {"text": sample_text, "engine": "apple"},
        timeout=25,
    )
    report["tests"]["tools_api_transcribe"] = _post_json(
        "http://127.0.0.1:5003/collab/transcribe",
        {"audio_path": audio_path},
        timeout=25,
    )

    # 寫報告檔
    out_dir = MAGI_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"apple_intelligence_smoke_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    # 人類可讀格式（同時附 JSON）
    lines = []
    lines.append("Apple Intelligence / Apple on-device 協作冒煙測試報告")
    lines.append("=" * 60)
    lines.append(f"時間：{report['ts']}")
    lines.append(f"PDF：{pdf_path or '(未提供/不存在)'}")
    lines.append(f"圖片：{image_path or '(未提供/不存在)'}")
    lines.append(f"音檔：{audio_path or '(未提供/不存在)'}")
    lines.append("")
    lines.append("捷徑狀態（需要建立的兩個）：MAGI 摘要 / MAGI 音檔轉文字")
    sc = report.get("shortcuts", {}).get("shortcuts", {})
    lines.append(f"- MAGI 摘要：{'已安裝' if sc.get('summarize', {}).get('installed') else '未安裝'}")
    lines.append(f"- MAGI 音檔轉文字：{'已安裝' if sc.get('stt_file', {}).get('installed') else '未安裝'}")
    lines.append("")
    lines.append("測試結果摘要")
    pdf_ok = bool(report["tests"]["pdf_quartz"].get("success"))
    ocr_ok = bool(report["tests"]["ocr_vision"].get("success"))
    sum_ok = bool(report["tests"]["summarize_shortcuts"].get("success"))
    stt_ok = bool(report["tests"]["stt_shortcuts"].get("success"))
    lines.append(f"- PDF Quartz 取字：{'OK' if pdf_ok else 'FAIL'}")
    lines.append(f"- Vision OCR：{'OK' if ocr_ok else 'FAIL'}")
    lines.append(f"- Apple 摘要（捷徑）：{'OK' if sum_ok else 'FAIL/需建立捷徑'}")
    lines.append(f"- Apple 音檔轉文字（捷徑）：{'OK' if stt_ok else 'FAIL/需建立捷徑'}")
    pn = report["tests"].get("pdf_namer_text_engine", {})
    lines.append(f"- pdf-namer 取字引擎：{'OK' if pn.get('ok') else 'FAIL'} (engine: {pn.get('engine')})")
    lines.append("")
    lines.append("詳細 JSON（供除錯）")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(str(out_path))
    # return non-zero only if Quartz/Vision 都失敗（避免把捷徑未裝當致命）
    return 0 if (pdf_ok and ocr_ok) else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
