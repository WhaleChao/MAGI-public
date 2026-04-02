#!/usr/bin/env python3
"""
判決摘要功能測試
================
用桌面「判決」資料夾的所有 PDF 測試摘要 pipeline。
驗證：1) 能成功擷取文字 2) 摘要不降級 3) 回應品質正常

Usage:
    cd ~/Desktop/MAGI && python3 scripts/ops/test_summarize_judgments.py
"""
import json
import os
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parent.parent.parent
os.chdir(MAGI_ROOT)
sys.path.insert(0, str(MAGI_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(MAGI_ROOT / ".env")
except ImportError:
    pass

JUDGMENT_DIR = Path.home() / "Desktop" / "判決"

print("╔══════════════════════════════════════════════════╗")
print("║  判決摘要功能測試                                ║")
print("╚══════════════════════════════════════════════════╝")
print(f"  判決資料夾: {JUDGMENT_DIR}")
print(f"  MAGI_ROOT:  {MAGI_ROOT}")
print()

# ── 1. 確認判決資料夾 ──
pdfs = sorted(JUDGMENT_DIR.glob("*.pdf"))
if not pdfs:
    print("❌ 判決資料夾沒有 PDF 檔案")
    sys.exit(1)
print(f"  找到 {len(pdfs)} 個 PDF")
print()

# ── 2. Import 摘要模組 ──
print("── 載入摘要模組 ──")
try:
    from skills.bridge.balthasar_bridge import summarize_text
    print("  ✅ balthasar_bridge.summarize_text 載入成功")
except Exception as e:
    print(f"  ❌ 無法載入摘要模組: {e}")
    sys.exit(1)

# ── 3. Import PDF 擷取 ──
try:
    import fitz  # PyMuPDF
    PDF_ENGINE = "pymupdf"
    print("  ✅ PyMuPDF 載入成功")
except ImportError:
    try:
        from pypdf import PdfReader
        PDF_ENGINE = "pypdf"
        print("  ✅ pypdf 載入成功 (fallback)")
    except ImportError:
        print("  ❌ 沒有 PDF 擷取套件")
        sys.exit(1)

def extract_text(pdf_path, max_pages=5):
    """擷取 PDF 文字"""
    if PDF_ENGINE == "pymupdf":
        doc = fitz.open(str(pdf_path))
        text = ""
        for page in doc[:max_pages]:
            text += page.get_text()
        doc.close()
        return text
    else:
        reader = PdfReader(str(pdf_path))
        return "".join(page.extract_text() or "" for page in reader.pages[:max_pages])

# ── 4. 逐檔測試摘要 ──
print()
print("── 開始摘要測試 ──")
results = []

for pdf in pdfs:
    name = pdf.name
    print(f"\n  📄 {name}")

    # 4.1 擷取文字
    t0 = time.time()
    try:
        text = extract_text(pdf)
        extract_ms = int((time.time() - t0) * 1000)
    except Exception as e:
        print(f"     ❌ 文字擷取失敗: {e}")
        results.append({"file": name, "ok": False, "error": f"extract: {e}"})
        continue

    if len(text.strip()) < 50:
        print(f"     ⚠️  文字太少 ({len(text)} chars)，可能是掃描 PDF")
        results.append({"file": name, "ok": True, "skipped": True, "reason": "too_short"})
        continue

    print(f"     文字擷取: {len(text)} chars ({extract_ms}ms)")

    # 4.2 送摘要（取前 6000 字避免太長）
    input_text = text[:6000]
    t1 = time.time()
    try:
        result = summarize_text(input_text, timeout_sec=120, summary_length="medium")
        summary_ms = int((time.time() - t1) * 1000)
    except Exception as e:
        print(f"     ❌ 摘要失敗: {e}")
        results.append({"file": name, "ok": False, "error": f"summarize: {e}"})
        continue

    # 4.3 分析結果
    success = result.get("success", False)
    provider = result.get("provider", "unknown")
    model = result.get("model", "unknown")
    summary = result.get("text", "")
    degraded = result.get("degraded", False)
    error = result.get("error", "")

    # 判斷是否降級
    is_degraded = degraded or provider in ("remote_melchior", "remote_balthasar_fallback")

    if success and not is_degraded:
        status = "✅"
    elif success and is_degraded:
        status = "⚠️ 降級"
    else:
        status = "❌"

    print(f"     {status} 摘要完成 ({summary_ms}ms)")
    print(f"        Provider: {provider} | Model: {model}")
    if is_degraded:
        print(f"        ⚠️ 降級! degraded={degraded}")
    if summary:
        # 顯示前 100 字
        preview = summary.replace("\n", " ")[:100]
        print(f"        摘要: {preview}...")
    if error:
        print(f"        Error: {error[:100]}")

    results.append({
        "file": name,
        "ok": success,
        "degraded": is_degraded,
        "provider": provider,
        "model": model,
        "input_chars": len(input_text),
        "summary_chars": len(summary),
        "duration_ms": summary_ms,
        "error": error,
    })

# ── 5. 總結 ──
print()
print("═" * 55)
total = len(results)
ok = sum(1 for r in results if r.get("ok") and not r.get("degraded"))
degraded = sum(1 for r in results if r.get("ok") and r.get("degraded"))
failed = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
skipped = sum(1 for r in results if r.get("skipped"))

print(f"  Total: {total} | OK: {ok} | 降級: {degraded} | 失敗: {failed} | 跳過: {skipped}")

if degraded > 0:
    print(f"\n  ⚠️ {degraded} 個檔案觸發降級:")
    for r in results:
        if r.get("degraded"):
            print(f"     - {r['file']}: {r['provider']}/{r['model']}")

if failed > 0:
    print(f"\n  ❌ {failed} 個檔案摘要失敗:")
    for r in results:
        if not r.get("ok") and not r.get("skipped"):
            print(f"     - {r['file']}: {r.get('error','')[:80]}")

if ok == total - skipped and degraded == 0:
    print(f"\n  🎉 全部成功！無降級！")

print("═" * 55)

# Save report
report_path = MAGI_ROOT / "static" / "summarize_test_latest.json"
with open(report_path, "w", encoding="utf-8") as f:
    json.dump({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
               "total": total, "ok": ok, "degraded": degraded,
               "failed": failed, "skipped": skipped, "results": results},
              f, ensure_ascii=False, indent=2)
print(f"\n  報告: {report_path}")
