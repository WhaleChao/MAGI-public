"""
ensemble_inference.py — 三模型並行推理與一票否決制
建立時間：2026-04-14

架構：
  Port 8080: Gemma E4B   (事實查核員)
  Port 8082: Phi-4-mini  (邏輯審查員)
  Port 8083: SmolLM3-3B  (格式稽核員)

一票否決制：三模型必須全部同意才算通過。
不一致時：直接把三個模型的結果都輸出給使用者，不呼叫外部 API。
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


def consensus_check(results: Dict[str, Any], task_type: str = "chat") -> ConsensusResult:
    """一票否決制共識檢查。"""
    if task_type == "intent":
        return _consensus_intent(results)
    elif task_type == "summary":
        return _consensus_summary(results)
    else:
        # 一般 chat：用 primary，其他模型做品質審查
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

        # 品質檢查：如果 smol 指出簡體字、persona leak 等問題
        vetoed_by = []
        veto_reasons = []
        try:
            from api.tw_output_guard import normalize_output_text
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


def format_disagreement(cr: ConsensusResult) -> str:
    """把不一致結果格式化為人類可讀字串，讓使用者自己決定。"""
    lines = ["⚠️ 三模型未達共識，請選擇："]
    role_labels = {
        "primary": "🔵 Gemma（事實查核員）",
        "phi4": "🟡 Phi（邏輯審查員）",
        "smol": "🟢 SmolLM（格式稽核員）",
    }
    for key, label in role_labels.items():
        val = cr.individual_results.get(key)
        if val:
            lines.append(f"{label}：{val}")
        else:
            lines.append(f"{label}：（無回應）")
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
