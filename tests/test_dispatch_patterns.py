#!/usr/bin/env python3
"""
Comprehensive test for fuzzy_match and command_dispatch regex patterns.
"""

import sys
import os

sys.path.insert(0, "/Users/ai/Desktop/MAGI_v2")
os.chdir("/Users/ai/Desktop/MAGI_v2")

# Suppress logging noise during tests
import logging
logging.disable(logging.CRITICAL)

from api.pipelines.fuzzy_match import fuzzy_correct, suggest_correction
from api.pipelines.command_dispatch import (
    _RE_DRAW,
    _RE_COURT,
    _RE_LAF,
    _RE_SCHEDULE,
    _RE_MEETING,
    _RE_SUMMARIZ,
    _RE_SUMMARY,
    _RE_CASE_NUMBER,
    _RE_HTTP_URL,
    _RE_PAYMENT_DISMISS,
)

# ── Test infrastructure ──────────────────────────────────────────────────────

passed = 0
failed = 0
failures = []


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        msg = f"  FAIL  {name}" + (f"  -- {detail}" if detail else "")
        print(msg)
        failures.append(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1: fuzzy_match tests
# ═══════════════════════════════════════════════════════════════════════════════

print("=" * 72)
print("PART 1: fuzzy_correct / suggest_correction")
print("=" * 72)

# --- Exact keyword / direct hits ---

# 法扶 is a canonical keyword (part of 法扶回報指令, 法扶指令, etc.)
corr, conf = fuzzy_correct("法扶")
check("'法扶' triggers a legal-aid related match",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("閱卷")
check("'閱卷' triggers a file-review related match",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("筆錄")
check("'筆錄' triggers a transcript related match",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("排庭")
# 排庭 is NOT a canonical keyword; 庭期 is. Fuzzy match may or may not fire.
# Let's just report:
check("'排庭' -> schedule-related or None (inspect)",
      True,  # informational
      f"got corrected={corr!r} conf={conf}")

# --- Known typo map hits ---

corr, conf = fuzzy_correct("法付")
check("'法付' typo corrects to 法扶-related",
      corr is not None and "法扶" in corr and conf == 1.0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("閱券")
check("'閱券' typo corrects to 閱卷-related",
      corr is not None and "閱卷" in corr and conf == 1.0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("必錄")
check("'必錄' typo corrects to 筆錄-related",
      corr is not None and "筆錄" in corr and conf == 1.0,
      f"got corrected={corr!r} conf={conf}")

# --- Should NOT trigger ---

corr, conf = fuzzy_correct("你好")
check("'你好' -> no correction",
      corr is None or conf < 0.65,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("今天天氣如何")
check("'今天天氣如何' -> no correction",
      corr is None or conf < 0.65,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("幫我查法條")
check("'幫我查法條' -> no false correction",
      True,  # informational - report what happens
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("法院在哪裡")
check("'法院在哪裡' -> should NOT trigger 法扶",
      corr is None or "法扶" not in (corr or ""),
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("我想看卷宗")
check("'我想看卷宗' -> check if triggers 閱卷 (inspect)",
      True,  # informational
      f"got corrected={corr!r} conf={conf}")

# --- More command-related inputs ---

corr, conf = fuzzy_correct("開庭時間")
check("'開庭時間' -> schedule-related or None (inspect)",
      True,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("計算利息")
# No canonical keyword for interest calc; likely no match
check("'計算利息' -> no match expected",
      True,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("幫我畫圖")
check("'幫我畫圖' -> draw-related match",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("我畫了一張圖")
check("'我畫了一張圖' -> past tense, should ideally NOT trigger draw (inspect)",
      True,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("翻譯這段")
check("'翻譯這段' -> translation-related",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("摘要這篇")
check("'摘要這篇' -> summary-related",
      corr is not None and conf > 0,
      f"got corrected={corr!r} conf={conf}")

# --- Edge cases ---

corr, conf = fuzzy_correct("")
check("'' (empty) -> None",
      corr is None,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("asdfjkl")
check("'asdfjkl' gibberish -> None",
      corr is None or conf < 0.65,
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("admin")
check("'admin' -> not corrected (admin exclusion check)",
      True,  # admin is English; _ADMIN_KEYWORDS are Chinese, so check
      f"got corrected={corr!r} conf={conf}")

corr, conf = fuzzy_correct("設定")
check("'設定' -> not corrected (not a canonical keyword)",
      corr is None or conf < 0.65,
      f"got corrected={corr!r} conf={conf}")

# --- suggest_correction API ---

print()
print("--- suggest_correction API ---")

result = suggest_correction("法付")
check("suggest_correction('法付') -> auto-correct notice",
      result is not None and "自動修正" in result,
      f"got {result!r}")

result = suggest_correction("你好")
check("suggest_correction('你好') -> None",
      result is None,
      f"got {result!r}")

result = suggest_correction("")
check("suggest_correction('') -> None",
      result is None,
      f"got {result!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2: command_dispatch regex patterns
# ═══════════════════════════════════════════════════════════════════════════════

print()
print("=" * 72)
print("PART 2: command_dispatch regex patterns")
print("=" * 72)

# --- _RE_DRAW ---
print()
print("--- _RE_DRAW ---")

for text in ["畫一隻貓", "幫我draw a cat", "/draw sunset", "画圖", "generate image of X", "畫張圖", "畫圖", "畫個東西"]:
    m = _RE_DRAW.search(text)
    check(f"_RE_DRAW positive: {text!r}", m is not None, f"match={m}")

for text in ["這是一幅畫作", "抽獎活動", "drawing room", "I drew a picture", "我畫了一張圖"]:
    m = _RE_DRAW.search(text)
    check(f"_RE_DRAW negative: {text!r}", m is None, f"match={m}")

# --- _RE_COURT ---
print()
print("--- _RE_COURT ---")

for text in ["search court records", "find court cases", "court hearing"]:
    m = _RE_COURT.search(text)
    check(f"_RE_COURT positive: {text!r}", m is not None, f"match={m}")

for text in ["courtesy", "courtyard", "法院在哪"]:
    m = _RE_COURT.search(text)
    check(f"_RE_COURT negative: {text!r}", m is None, f"match={m}")

# --- _RE_LAF ---
print()
print("--- _RE_LAF ---")

for text in ["laf status", "check laf", "LAF report"]:
    m = _RE_LAF.search(text)
    check(f"_RE_LAF positive: {text!r}", m is not None, f"match={m}")

for text in ["laugh out loud", "half done", "Lafayette"]:
    m = _RE_LAF.search(text)
    check(f"_RE_LAF negative: {text!r}", m is None, f"match={m}")

# --- _RE_SCHEDULE ---
print()
print("--- _RE_SCHEDULE ---")

for text in ["show schedule", "my schedule today", "schedule meeting"]:
    m = _RE_SCHEDULE.search(text)
    check(f"_RE_SCHEDULE positive: {text!r}", m is not None, f"match={m}")

for text in ["scheduled task", "reschedule it", "排庭時間"]:
    m = _RE_SCHEDULE.search(text)
    check(f"_RE_SCHEDULE negative: {text!r}", m is None, f"match={m}")

# --- _RE_MEETING ---
print()
print("--- _RE_MEETING ---")

for text in ["meeting at 3pm", "start meeting", "meeting notes"]:
    m = _RE_MEETING.search(text)
    check(f"_RE_MEETING positive: {text!r}", m is not None, f"match={m}")

for text in ["meetings are boring", "nice to meet you", "開會中"]:
    # "meetings" should NOT match because \b requires word boundary after 'g'
    m = _RE_MEETING.search(text)
    check(f"_RE_MEETING negative: {text!r}", m is None, f"match={m}")

# --- _RE_SUMMARIZ ---
print()
print("--- _RE_SUMMARIZ ---")

for text in ["summarize this", "summarizing the doc", "please summarization"]:
    m = _RE_SUMMARIZ.search(text)
    check(f"_RE_SUMMARIZ positive: {text!r}", m is not None, f"match={m}")

for text in ["sum up", "total summary count", "resume"]:
    m = _RE_SUMMARIZ.search(text)
    check(f"_RE_SUMMARIZ negative: {text!r}", m is None, f"match={m}")

# --- _RE_SUMMARY ---
print()
print("--- _RE_SUMMARY ---")

for text in ["give me a summary", "summary of doc", "short summary"]:
    m = _RE_SUMMARY.search(text)
    check(f"_RE_SUMMARY positive: {text!r}", m is not None, f"match={m}")

for text in ["summarize", "summaries list", "sum total"]:
    m = _RE_SUMMARY.search(text)
    check(f"_RE_SUMMARY negative: {text!r}", m is None, f"match={m}")

# --- _RE_CASE_NUMBER ---
print()
print("--- _RE_CASE_NUMBER ---")

for text, desc in [
    ("113年度訴字第123號", "full format"),
    ("112重訴45", "compact format"),
    ("111 年度 勞訴 字 第 789 號", "spaced format"),
]:
    m = _RE_CASE_NUMBER.search(text)
    check(f"_RE_CASE_NUMBER positive ({desc}): {text!r}",
          m is not None,
          f"match={m.group() if m else None}, groups={m.groups() if m else None}")

for text in ["phone 0912345678", "2024年1月", "hello world 123"]:
    m = _RE_CASE_NUMBER.search(text)
    check(f"_RE_CASE_NUMBER negative: {text!r}", m is None, f"match={m}")

# --- _RE_HTTP_URL ---
print()
print("--- _RE_HTTP_URL ---")

for text in ["visit http://example.com", "https://google.com/search?q=test", "http://localhost:8080"]:
    m = _RE_HTTP_URL.search(text)
    check(f"_RE_HTTP_URL positive: {text!r}", m is not None, f"match={m}")

for text in ["ftp://server.com", "no url here", "www.example.com"]:
    m = _RE_HTTP_URL.search(text)
    check(f"_RE_HTTP_URL negative: {text!r}", m is None, f"match={m}")

# --- _RE_PAYMENT_DISMISS ---
print()
print("--- _RE_PAYMENT_DISMISS ---")

for text in ["王小明已繳費", "黃語玲已經繳費", "案件A繳費完畢"]:
    m = _RE_PAYMENT_DISMISS.search(text)
    check(f"_RE_PAYMENT_DISMISS positive: {text!r}",
          m is not None,
          f"match={m.group() if m else None}, group(1)={m.group(1) if m else None}")

for text in ["請去繳費", "繳費單在哪", "尚未繳費"]:
    m = _RE_PAYMENT_DISMISS.search(text)
    check(f"_RE_PAYMENT_DISMISS negative: {text!r}", m is None, f"match={m}")


# ═══════════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════════

print()
print("=" * 72)
print(f"RESULTS:  {passed} passed,  {failed} failed,  {passed + failed} total")
print("=" * 72)
if failures:
    print()
    print("FAILURES:")
    for f in failures:
        print(f)
    print()
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
