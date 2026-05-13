#!/usr/bin/env python3
"""NVIDIA NIM 兜底整合冒煙測試（2026-04-19）

用途：
  - 驗證 NVIDIA NIM API key 有效、模型白名單正確、PII scrub 可逆
  - 不依賴 MAGI 長駐 runtime；可獨立執行
  - 不傳任何真實個資（全用合成測試資料）

執行：
  cd /Users/ai/Desktop/MAGI_v2
  NVIDIA_NIM_ENABLE=1 ./venv/bin/python3 scripts/ops/smoke_nvidia_nim.py

若要跳過真實 API 呼叫（只測設定）：
  NVIDIA_NIM_ENABLE=1 NIM_SMOKE_SKIP_LIVE=1 ./venv/bin/python3 scripts/ops/smoke_nvidia_nim.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAGI_ROOT))

# ─── helpers ────────────────────────────────────────────────────────────────

def _ok(label: str, detail: str = "") -> None:
    msg = f"  ✅ {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _fail(label: str, detail: str = "") -> None:
    msg = f"  ❌ {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)


def _skip(label: str, reason: str = "") -> None:
    msg = f"  ⏭️  {label}"
    if reason:
        msg += f" ({reason})"
    print(msg)


# ─── Test 1: 環境變數 / API key 設定 ────────────────────────────────────────

def test_env_config() -> bool:
    print("\n[1] 環境變數 / API key 設定")
    api_key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not api_key:
        _fail("NVIDIA_NIM_API_KEY", "未設定（請在 .env 或環境中設定）")
        return False
    if api_key.startswith("<<"):
        _fail("NVIDIA_NIM_API_KEY", "仍是佔位符，請換上真實 key")
        return False
    if not api_key.startswith("nvapi-"):
        _fail("NVIDIA_NIM_API_KEY", "格式不符（應以 nvapi- 開頭）")
        return False
    _ok("NVIDIA_NIM_API_KEY", f"{api_key[:12]}...（長度 {len(api_key)}）")

    enable = os.environ.get("NVIDIA_NIM_ENABLE", "0")
    if enable not in ("1", "true", "yes", "on"):
        _fail("NVIDIA_NIM_ENABLE", f"={enable}（需為 1 才能實際觸發）")
        return False
    _ok("NVIDIA_NIM_ENABLE", f"={enable}")
    return True


# ─── Test 2: 模型白名單 / 黑名單 ────────────────────────────────────────────

def test_model_allowlist() -> bool:
    print("\n[2] 模型白名單 / 黑名單")
    try:
        from providers.nvidia_nim import NvidiaNimProvider
    except ImportError as e:
        _fail("import NvidiaNimProvider", str(e))
        return False

    allowed_cases = [
        "meta/llama-3.3-70b-instruct",
        "mistralai/mistral-large-2-instruct",
        "google/gemma-3-27b-it",
        "microsoft/phi-4-multimodal-instruct",
    ]
    blocked_cases = [
        "deepseek/deepseek-r1",
        "qwen/qwen-72b",
        "01-ai/yi-large",
        "baichuan-inc/baichuan2-13b",
        "zhipuai/glm-4",
        "moonshot-v1-128k",
        "internlm/internlm2-chat-20b",
    ]

    ok = True
    for m in allowed_cases:
        if NvidiaNimProvider.is_model_allowed(m):
            _ok(f"允許: {m}")
        else:
            _fail(f"應允許但被擋: {m}")
            ok = False

    for m in blocked_cases:
        if not NvidiaNimProvider.is_model_allowed(m):
            _ok(f"封鎖: {m}")
        else:
            _fail(f"應封鎖但被允許: {m}")
            ok = False

    return ok


# ─── Test 3: PII scrubber 可逆性 ────────────────────────────────────────────

def test_pii_scrubber() -> bool:
    print("\n[3] PII Scrubber 可逆性")
    try:
        from skills.engine.pii_scrubber import PIIScrubber
    except ImportError as e:
        _fail("import PIIScrubber", str(e))
        return False

    scrubber = PIIScrubber(known_names=["王大明"])
    original = "當事人王大明 身分證 A123456789，聯絡電話 0912-345-678，案號 114年度原訴字第000024號"
    result = scrubber.scrub(original)

    if "A123456789" in result.scrubbed_text:
        _fail("身分證未遮蔽")
        return False
    if "0912" in result.scrubbed_text:
        _fail("手機號未遮蔽")
        return False
    if "王大明" in result.scrubbed_text:
        _fail("姓名未遮蔽")
        return False
    _ok("scrub 遮蔽正確", f"counts={result.counts}")

    # 模擬 LLM 回覆含佔位符
    fake_llm_reply = result.scrubbed_text.replace(
        "[ID-001]", "[ID-001] 的資料已核對完成"
    )
    restored = result.restore(fake_llm_reply)
    if "A123456789" not in restored:
        _fail("restore 未還原身分證")
        return False
    _ok("restore 還原正確")
    return True


# ─── Test 4: nim_heavy run_nim_chat 設定層（不打 API）───────────────────────

def test_nim_config_layer() -> bool:
    print("\n[4] nim_heavy 設定層（budget / circuit breaker / model 選擇）")
    try:
        from skills.bridge.nim_heavy import _pick_model, _env_bool, _daily_budget, _cb_can_call
    except ImportError as e:
        _fail("import nim_heavy helpers", str(e))
        return False

    model_fast = _pick_model("general", heavy=False)
    model_heavy = _pick_model("general", heavy=True)
    _ok(f"fast model: {model_fast}")
    _ok(f"heavy model: {model_heavy}")

    budget = _daily_budget()
    _ok(f"daily budget: {budget}")

    can_call, reason = _cb_can_call()
    if can_call:
        _ok("circuit breaker: open")
    else:
        _skip("circuit breaker: cooldown active", reason)

    return True


# ─── Test 5: live API call（可跳過）─────────────────────────────────────────

def test_live_api_call() -> bool:
    skip_live = os.environ.get("NIM_SMOKE_SKIP_LIVE", "0").strip().lower()
    if skip_live in ("1", "true", "yes"):
        print("\n[5] Live API call")
        _skip("跳過（NIM_SMOKE_SKIP_LIVE=1）")
        return True

    print("\n[5] Live API call（合成無個資 prompt）")
    try:
        from skills.bridge.nim_heavy import run_nim_chat
    except ImportError as e:
        _fail("import run_nim_chat", str(e))
        return False

    t0 = time.monotonic()
    result = run_nim_chat(
        prompt="請用繁體中文回答：1 + 1 = ?（只需回答數字即可）",
        timeout_sec=60,
        task_type="general",
        require_pii_scrub=False,
        heavy=False,
    )
    elapsed = time.monotonic() - t0

    if not result.get("success"):
        err = result.get("error", "unknown")
        _fail("API call 失敗", err)
        return False

    response = result.get("response", "")
    _ok(f"API call 成功，耗時 {elapsed:.1f}s", f"model={result.get('model')}")
    _ok(f"回覆: {response[:80]!r}")
    return True


# ─── Test 6: usage log 寫入 ──────────────────────────────────────────────────

def test_usage_log() -> bool:
    print("\n[6] Usage log")
    try:
        from skills.bridge.nim_heavy import get_usage_report
    except ImportError as e:
        _fail("import get_usage_report", str(e))
        return False

    report = get_usage_report(days=1)
    _ok(f"usage report: total={report.get('total')}, ok={report.get('ok')}, fail={report.get('fail')}")
    _ok(f"daily count today: {report.get('daily_count_today')}/{report.get('daily_budget')}")
    return True


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 60)
    print("NVIDIA NIM 兜底整合 Smoke Test（2026-04-19）")
    print("=" * 60)

    # Load .env
    env_path = MAGI_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

    results = []
    results.append(("env_config", test_env_config()))
    results.append(("model_allowlist", test_model_allowlist()))
    results.append(("pii_scrubber", test_pii_scrubber()))
    results.append(("nim_config_layer", test_nim_config_layer()))
    results.append(("live_api_call", test_live_api_call()))
    results.append(("usage_log", test_usage_log()))

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"結果：{passed}/{total} 通過")

    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")

    all_pass = all(ok for _, ok in results)
    if all_pass:
        print("\n🎉 All tests passed — NVIDIA NIM 兜底整合就緒")
    else:
        print("\n⚠️  有測試失敗，請依上方錯誤訊息排查")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
