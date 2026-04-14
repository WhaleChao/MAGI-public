"""
ensemble_inference.py — 三模型兩階段審查推理
建立時間：2026-04-14
更新：2026-04-14（兩階段審查員模式）

架構：
  Port 8080: Gemma E4B   (生產者 + 事實查核員)
  Port 8082: Phi-4-mini  (邏輯審查員 — 審查 E4B 的輸出)
  Port 8083: SmolLM3-3B  (格式稽核員 — 審查 E4B 的輸出)

chat/legal 模式（兩階段）：
  Phase 1: E4B 生成答案
  Phase 2: Phi-4 和 SmolLM3 並行審查 E4B 答案，各自回 OK 或 VETO: <原因>
  → 任一審查員否決即觸發 unanimous=False

intent 模式（三票）：
  三模型各自分類，完全一致才算共識

summary 模式：
  E4B 主輸出，三模型結果存 individual_results

否決失效條件：審查員自身失敗（逾時/錯誤）= 棄權，不算否決
"""
from __future__ import annotations

import os
import json
import logging
import concurrent.futures
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# ── Port 設定 ──
OMLX_E4B_PORT = int(os.environ.get("MAGI_OMLX_PORT", "8080"))
OMLX_PHI4_PORT = int(os.environ.get("MAGI_OMLX_PHI4_PORT", "8082"))
OMLX_SMOL_PORT = int(os.environ.get("MAGI_OMLX_SMOL_PORT", "8083"))

OMLX_E4B_BASE = os.environ.get("MAGI_OMLX_BASE", f"http://127.0.0.1:{OMLX_E4B_PORT}")
OMLX_PHI4_BASE = f"http://127.0.0.1:{OMLX_PHI4_PORT}"
OMLX_SMOL_BASE = f"http://127.0.0.1:{OMLX_SMOL_PORT}"

# ── 三模型性格定義 ──
ENSEMBLE_ROLES = {
    "primary": {
        "name": "Gemma (事實查核員)",
        "port": OMLX_E4B_PORT,
        "base": OMLX_E4B_BASE,
        "system_prefix": (
            "你是「事實查核員」，專注於事實正確性與完整性。\n"
            "職責：\n"
            "1. 確認人名、日期、金額、法條編號、案號正確引用\n"
            "2. 確認沒有遺漏關鍵事實\n"
            "3. 確認沒有添加原文沒有的資訊（禁止幻覺）\n"
            "4. 用繁體中文，遵循台灣法律用語\n"
        ),
    },
    "phi4": {
        "name": "Phi (邏輯審查員)",
        "port": OMLX_PHI4_PORT,
        "base": OMLX_PHI4_BASE,
        "system_prefix": (
            "你是「邏輯審查員」，專注於推理邏輯與結構一致性。\n"
            "職責：\n"
            "1. 確認因果關係正確（A 導致 B 的推論是否成立）\n"
            "2. 確認結論與前提一致（沒有自相矛盾）\n"
            "3. 確認分類與歸屬合理（案由、罪名、法條適用）\n"
            "4. 發現邏輯漏洞時明確指出\n"
        ),
    },
    "smol": {
        "name": "SmolLM (格式稽核員)",
        "port": OMLX_SMOL_PORT,
        "base": OMLX_SMOL_BASE,
        "system_prefix": (
            "你是「格式稽核員」，專注於輸出品質與格式規範。\n"
            "職責：\n"
            "1. 確認輸出為繁體中文（不可混入簡體字）\n"
            "2. 確認格式符合要求（條列式、結構化、無廢話）\n"
            "3. 確認沒有洩漏內部標籤（[使用者陳述]、身為 CASPER）\n"
            "4. 確認沒有 persona 跑題\n"
        ),
    },
}

DEFAULT_ENSEMBLE_TIMEOUT = int(os.environ.get("MAGI_ENSEMBLE_TIMEOUT_SEC", "60"))
ENSEMBLE_MODE = os.environ.get("MAGI_ENSEMBLE_MODE", "auto")


class ConsensusResult:
    """三模型共識結果。"""

    def __init__(
        self,
        unanimous: bool,
        result: Optional[str],
        vetoed_by: Optional[List[str]] = None,
        veto_reasons: Optional[List[str]] = None,
        individual_results: Optional[Dict[str, Any]] = None,
        task_type: str = "chat",
    ):
        self.unanimous = unanimous
        self.result = result
        self.vetoed_by = vetoed_by or []
        self.veto_reasons = veto_reasons or []
        self.individual_results = individual_results or {}
        self.task_type = task_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unanimous": self.unanimous,
            "result": self.result,
            "vetoed_by": self.vetoed_by,
            "veto_reasons": self.veto_reasons,
            "individual_results": self.individual_results,
            "task_type": self.task_type,
        }


def _call_omlx_chat(
    base_url: str,
    model_hint: str,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: int = DEFAULT_ENSEMBLE_TIMEOUT,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """呼叫單一 oMLX 實例。"""
    try:
        import requests  # type: ignore

        # 取得實際 model id
        try:
            models_resp = requests.get(f"{base_url}/v1/models", timeout=5)
            models_data = models_resp.json()
            model_id = models_data["data"][0]["id"] if models_data.get("data") else model_hint
        except Exception:
            model_id = model_hint

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return {"success": True, "text": text, "model": model_id}
    except Exception as e:
        return {"success": False, "error": str(e), "text": ""}


def ensemble_chat(
    prompt: str,
    system: str = "",
    mode: str = "verify",
    timeout_sec: int = DEFAULT_ENSEMBLE_TIMEOUT,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """並行送給三個 port，回傳各模型結果 dict。

    mode:
      "fast"   — 只送 primary (E4B)，不走三模型
      "verify" — 三模型並行
    """
    if mode == "fast":
        role = ENSEMBLE_ROLES["primary"]
        sys_prompt = (role["system_prefix"] + "\n" + system).strip() if system else role["system_prefix"]
        result = _call_omlx_chat(role["base"], "gemma-4-e4b-it-4bit", sys_prompt, prompt, timeout_sec, max_tokens)
        return {"primary": result, "phi4": None, "smol": None, "mode": "fast"}

    results: Dict[str, Any] = {}
    roles_list = [("primary", ENSEMBLE_ROLES["primary"]), ("phi4", ENSEMBLE_ROLES["phi4"]), ("smol", ENSEMBLE_ROLES["smol"])]

    def _call_role(key_role):
        key, role = key_role
        sys_prompt = (role["system_prefix"] + "\n" + system).strip() if system else role["system_prefix"]
        return key, _call_omlx_chat(role["base"], role["name"], sys_prompt, prompt, timeout_sec, max_tokens)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_call_role, item): item[0] for item in roles_list}
        for future in concurrent.futures.as_completed(futures, timeout=timeout_sec + 5):
            try:
                key, result = future.result()
                results[key] = result
            except Exception as e:
                key = futures[future]
                results[key] = {"success": False, "error": str(e), "text": ""}

    results["mode"] = "verify"
    return results


def _normalize_intent_label(text: str) -> str:
    """正規化意圖分類 label。"""
    t = text.strip().upper()
    # 常見 label 正規化
    for kw in ("CMD", "COMMAND", "命令", "指令"):
        if kw in t:
            return "CMD"
    for kw in ("QUERY", "查詢", "問題", "QUESTION"):
        if kw in t:
            return "QUERY"
    for kw in ("CHAT", "聊天", "閒聊"):
        if kw in t:
            return "CHAT"
    for kw in ("RECALL", "記憶", "回憶"):
        if kw in t:
            return "RECALL"
    # 取第一個 word
    import re
    first = re.split(r"[\s,.\n:：]", text.strip())[0].upper()
    return first or "UNKNOWN"


def _consensus_intent(results: Dict[str, Any]) -> ConsensusResult:
    """三 label 完全一致才算共識。"""
    labels = {}
    for key in ("primary", "phi4", "smol"):
        r = results.get(key)
        if r and r.get("success") and r.get("text"):
            labels[key] = _normalize_intent_label(r["text"])
        else:
            labels[key] = None

    valid_labels = [v for v in labels.values() if v is not None]
    if not valid_labels:
        return ConsensusResult(
            unanimous=False, result=None,
            individual_results=labels, task_type="intent"
        )

    unique = set(valid_labels)
    if len(unique) == 1 and len(valid_labels) == 3:
        return ConsensusResult(
            unanimous=True, result=valid_labels[0],
            individual_results=labels, task_type="intent"
        )

    # 不一致：回傳所有結果
    return ConsensusResult(
        unanimous=False, result=None,
        individual_results=labels, task_type="intent"
    )


def _consensus_summary(results: Dict[str, Any]) -> ConsensusResult:
    """交集要點為主輸出。不一致要點保留在 individual_results。"""
    texts = {}
    for key in ("primary", "phi4", "smol"):
        r = results.get(key)
        if r and r.get("success") and r.get("text"):
            texts[key] = r["text"]

    if not texts:
        return ConsensusResult(unanimous=False, result=None, individual_results=texts, task_type="summary")

    # 有任何一個模型成功即可輸出（summary 不強求三模型一致）
    # 用 primary 作為主輸出，其他模型結果存 individual_results
    primary_text = texts.get("primary", "")
    all_same = len(set(texts.values())) == 1

    return ConsensusResult(
        unanimous=all_same,
        result=primary_text or next(iter(texts.values()), None),
        individual_results=texts,
        task_type="summary"
    )


# ── 審查員 prompt ──
_PHI4_REVIEWER_SYSTEM = (
    "你是邏輯審查員。任務：審查下面的法律回答有無邏輯錯誤或法條引用錯誤。\n"
    "規則：\n"
    "- 沒有問題：只回覆 OK，不要加任何其他字\n"
    "- 有問題：只回覆 VETO: 後接一句具體錯誤描述（例如：VETO: 第184條第一項前段要件漏掉違法性）\n"
    "禁止重複問題或重新回答問題，禁止回覆超過一行。"
)

_SMOL_REVIEWER_SYSTEM = (
    "你是格式稽核員。任務：審查下面的回答有無簡體字或內部標籤（如[使用者陳述]）洩漏。\n"
    "規則：\n"
    "- 沒有問題：只回覆 OK，不要加任何其他字\n"
    "- 有問題：只回覆 VETO: 後接一句具體描述（例如：VETO: 第3行出現簡體字「损害」）\n"
    "禁止重複問題或重新回答問題，禁止回覆超過一行。"
)


def _parse_reviewer_verdict(text: str):
    # type: (str) -> tuple
    """解析審查員回應。回傳 (vetoed: bool, reason: str)。
    設計原則：審查員回應模糊時預設不否決，避免誤殺。
    """
    if not text:
        return False, ""
    # 只取第一行（防止模型多說廢話）
    first_line = text.strip().split("\n")[0].strip()
    upper = first_line.upper()

    # 明確 OK 優先
    ok_kws = ["OK", "通過", "正確", "沒問題", "無問題", "NO ISSUE", "PASS"]
    for kw in ok_kws:
        if upper.startswith(kw):
            return False, ""

    # 明確 VETO
    if upper.startswith("VETO"):
        parts = first_line.split(":", 1)
        reason = parts[1].strip() if len(parts) > 1 else ""
        # 過濾模板佔位符（審查員沒有真正填入原因）
        placeholder_kws = ["<一句話", "<具體", "<請說明", "<reason", "<problem"]
        if not reason or any(p in reason for p in placeholder_kws):
            return False, ""  # 模板未填 → 視為棄權
        return True, reason

    # 其他否決關鍵字（短句才算，避免誤判）
    veto_kws = ["否決", "有誤", "法條錯誤", "邏輯矛盾", "簡體字出現", "標籤洩漏"]
    for kw in veto_kws:
        if kw in first_line and len(first_line) < 80:
            return True, first_line[:80]

    # 無法判斷 → 棄權（不否決）
    return False, ""


def _ensemble_review(
    original_prompt,   # type: str
    primary_answer,    # type: str
    timeout_sec=30,    # type: int
):
    # type: (...) -> Dict[str, Any]
    """Phase 2：Phi-4 和 SmolLM3 並行審查 E4B 的答案。"""
    review_prompt = (
        "【原始問題】\n{q}\n\n"
        "【待審查的回答】\n{a}"
    ).format(q=original_prompt[:400], a=primary_answer[:1200])

    results = {}  # type: Dict[str, Any]

    def _review(role_key, base, sys_prompt):
        return role_key, _call_omlx_chat(
            base, role_key, sys_prompt, review_prompt,
            timeout_sec=timeout_sec, max_tokens=60
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fs = {
            executor.submit(_review, "phi4", OMLX_PHI4_BASE, _PHI4_REVIEWER_SYSTEM): "phi4",
            executor.submit(_review, "smol", OMLX_SMOL_BASE, _SMOL_REVIEWER_SYSTEM): "smol",
        }
        for future in concurrent.futures.as_completed(fs, timeout=timeout_sec + 5):
            try:
                key, result = future.result()
                results[key] = result
            except Exception as e:
                key = fs[future]
                results[key] = {"success": False, "error": str(e), "text": ""}

    return results


def _build_review_consensus(original_prompt, primary_answer, review_results, task_type="chat"):
    # type: (str, str, Dict[str, Any], str) -> ConsensusResult
    """Phase 2 審查結果 → ConsensusResult。"""
    vetoed_by = []
    veto_reasons = []

    role_labels = {
        "phi4": "Phi-4 邏輯審查員",
        "smol": "SmolLM3 格式稽核員",
    }
    review_verdicts = {}  # type: Dict[str, str]

    for reviewer_key in ("phi4", "smol"):
        r = review_results.get(reviewer_key, {})
        verdict_text = r.get("text", "") if r else ""
        review_verdicts[reviewer_key + "_verdict"] = verdict_text
        if not r or not r.get("success"):
            # 審查員失敗 → 棄權
            review_verdicts[reviewer_key + "_verdict"] = "(審查員無回應，棄權)"
            continue
        vetoed, reason = _parse_reviewer_verdict(verdict_text)
        if vetoed:
            vetoed_by.append(reviewer_key)
            veto_reasons.append("{}: {}".format(role_labels[reviewer_key], reason))

    # output_guard 保留
    final_answer = primary_answer
    try:
        from api.tw_output_guard import normalize_output_text  # type: ignore
        cleaned = normalize_output_text(primary_answer)
        if cleaned != primary_answer and "抱歉" in cleaned:
            vetoed_by.append("output_guard")
            veto_reasons.append("內部標籤洩漏或 persona 跑題")
            final_answer = cleaned
    except Exception:
        pass

    return ConsensusResult(
        unanimous=len(vetoed_by) == 0,
        result=final_answer,
        vetoed_by=vetoed_by,
        veto_reasons=veto_reasons,
        individual_results=dict(
            primary=primary_answer,
            **review_verdicts
        ),
        task_type=task_type,
    )


def consensus_check(results: Dict[str, Any], task_type: str = "chat") -> ConsensusResult:
    """一票否決制共識檢查。

    intent  → 三票完全一致
    summary → E4B 主輸出，others 存 individual_results
    chat    → 向後相容：只跑 output_guard（不觸發審查員）
              若要審查員否決，請改用 ensemble_chat_verified()
    """
    if task_type == "intent":
        return _consensus_intent(results)
    elif task_type == "summary":
        return _consensus_summary(results)
    else:
        primary = results.get("primary", {})
        phi4 = results.get("phi4", {})
        smol = results.get("smol", {})

        primary_text = primary.get("text", "") if primary else ""
        if not primary_text:
            return ConsensusResult(
                unanimous=False, result=None,
                individual_results={k: v.get("text", "") for k, v in results.items() if isinstance(v, dict)},
                task_type=task_type
            )

        vetoed_by = []
        veto_reasons = []
        try:
            from api.tw_output_guard import normalize_output_text  # type: ignore
            cleaned = normalize_output_text(primary_text)
            if cleaned != primary_text and "抱歉" in cleaned:
                vetoed_by.append("output_guard")
                veto_reasons.append("內部標籤洩漏或 persona 跑題")
                primary_text = cleaned
        except Exception:
            pass

        return ConsensusResult(
            unanimous=len(vetoed_by) == 0,
            result=primary_text,
            vetoed_by=vetoed_by,
            veto_reasons=veto_reasons,
            individual_results={
                "primary": primary.get("text", "") if primary else "",
                "phi4": phi4.get("text", "") if phi4 else "",
                "smol": smol.get("text", "") if smol else "",
            },
            task_type=task_type
        )


def ensemble_chat_verified(
    prompt: str,
    system: str = "",
    timeout_sec: int = DEFAULT_ENSEMBLE_TIMEOUT,
    max_tokens: int = 1024,
    task_type: str = "chat",
) -> ConsensusResult:
    """兩階段審查模式（正式入口）。

    Phase 1: E4B 生成答案（max_tokens）
    Phase 2: Phi-4 + SmolLM3 並行審查（max_tokens=60，只回 OK/VETO）
    任一審查員否決 → unanimous=False，veto_reasons 說明原因

    適用：法律問答、文件摘要、任何需要品質把關的回覆
    不適用：意圖分類（用 ensemble_classify_intent）、純閒聊
    """
    # Phase 1
    role = ENSEMBLE_ROLES["primary"]
    sys_prompt = (role["system_prefix"] + "\n" + system).strip() if system else role["system_prefix"]
    primary_result = _call_omlx_chat(
        role["base"], role["name"], sys_prompt, prompt,
        timeout_sec=timeout_sec, max_tokens=max_tokens
    )
    primary_answer = primary_result.get("text", "")

    if not primary_answer:
        return ConsensusResult(
            unanimous=False, result=None,
            individual_results={"primary_error": primary_result.get("error", "no output")},
            task_type=task_type
        )

    # Phase 2：審查員用一半的 timeout（避免慢審查拖垮整體）
    review_timeout = max(15, timeout_sec // 2)
    review_results = _ensemble_review(prompt, primary_answer, timeout_sec=review_timeout)

    return _build_review_consensus(prompt, primary_answer, review_results, task_type=task_type)


def format_disagreement(cr: ConsensusResult) -> str:
    """把審查/不一致結果格式化為人類可讀字串。"""
    # 兩階段審查模式（individual_results 含 *_verdict）
    has_review = any(
        k.endswith("_verdict") for k in cr.individual_results
    )
    if has_review:
        lines = ["⚠️ 審查員提出異議："]
        for reviewer, label in (("phi4", "🟡 Phi-4 邏輯審查"), ("smol", "🟢 SmolLM3 格式稽核")):
            verdict = cr.individual_results.get(reviewer + "_verdict", "")
            vetoed = reviewer in cr.vetoed_by
            mark = "❌" if vetoed else "✅"
            lines.append("{} {}：{}".format(mark, label, verdict or "(無回應)"))
        if cr.result:
            lines.append("\nE4B 原答案：\n{}".format(cr.result[:500]))
        if cr.veto_reasons:
            lines.append("\n否決原因：" + "；".join(cr.veto_reasons))
        return "\n".join(lines)

    # 意圖分類 / 三向不一致
    lines = ["⚠️ 三模型未達共識，請選擇："]
    role_labels = {
        "primary": "🔵 Gemma（事實查核員）",
        "phi4": "🟡 Phi（邏輯審查員）",
        "smol": "🟢 SmolLM（格式稽核員）",
    }
    for key, label in role_labels.items():
        val = cr.individual_results.get(key)
        if val:
            lines.append("{}：{}".format(label, val))
        else:
            lines.append("{}：（無回應）".format(label))
    return "\n".join(lines)


def should_use_ensemble(feature: str) -> str:
    """決定是否啟用三模型驗證。

    Returns:
        "fast"   — 只用 primary
        "verify" — 三模型並行
        "off"    — 完全略過 ensemble
    """
    if ENSEMBLE_MODE == "off":
        return "off"
    if ENSEMBLE_MODE == "always":
        return "verify"
    # auto 模式
    if feature in ("intent", "classify"):
        return "fast"  # 意圖分類只需 primary（速度優先）
    if feature in ("summary", "verify", "legal"):
        return "verify"
    return "fast"


def ensemble_classify_intent(message: str, timeout_sec: int = 30) -> ConsensusResult:
    """三模型意圖分類（主要入口）。

    只用 primary 做快速分類（速度優先），verify 模式才走三模型。
    """
    prompt = (
        f"請分類以下訊息的意圖，只回答一個 label（CMD / QUERY / CHAT / RECALL），不要解釋：\n\n{message}"
    )
    mode = should_use_ensemble("intent")
    if mode == "off" or mode == "fast":
        results = ensemble_chat(prompt, mode="fast", timeout_sec=timeout_sec, max_tokens=20)
        primary = results.get("primary", {})
        if primary and primary.get("success"):
            label = _normalize_intent_label(primary.get("text", ""))
            return ConsensusResult(
                unanimous=True, result=label,
                individual_results={"primary": label},
                task_type="intent"
            )
        return ConsensusResult(unanimous=False, result=None, individual_results={}, task_type="intent")

    results = ensemble_chat(prompt, mode="verify", timeout_sec=timeout_sec, max_tokens=20)
    return _consensus_intent(results)


def ensemble_verify_summary(text: str, summary: str, timeout_sec: int = 60) -> ConsensusResult:
    """三模型驗證摘要品質（主要入口）。"""
    prompt = (
        f"原文：\n{text[:2000]}\n\n"
        f"摘要：\n{summary}\n\n"
        f"請評估這份摘要的品質。若有問題請簡短說明，若無問題請回答「通過」。"
    )
    results = ensemble_chat(prompt, mode="verify", timeout_sec=timeout_sec, max_tokens=200)
    return _consensus_summary(results)
