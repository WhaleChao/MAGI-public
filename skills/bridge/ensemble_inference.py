"""
ensemble_inference.py — 三模型兩階段審查推理
建立時間：2026-04-14
更新：2026-04-14（兩階段審查員模式 + 規則稽核器）

架構：
  Port 8080: Gemma E4B   (生產者 + 事實查核員)
  Port 8082: Phi-4-mini  (邏輯審查員 — 審查 E4B 的輸出)
  Port 8083: SmolLM3-3B  (標籤洩漏稽核員 — 只查 internal label leak / persona 跑題)
  [規則]    rule_checker  (格式稽核 — 繁簡辨別、禁用字串，不用 LLM)

格式稽核設計原則：
  繁簡辨別不用 LLM（小模型 Unicode 辨別不可靠）
  改用 _check_simplified_chinese() 規則判斷，無需外部套件
  SmolLM3 只負責：internal label leak + persona 跑題（字串比對型任務，LLM 穩定）

模型選用原則（2026-04-14）：
  不考慮中國模型（Qwen/DeepSeek/GLM/Yi 等）
  原因：受中國審查機制，法律敏感內容（人權案件、政治敏感案由）會拒答
  適用模型：Gemma 系列、Phi 系列、Mistral 系列、Llama 系列（西方開源）

chat/legal 模式（兩階段）：
  Phase 1: E4B 生成答案
  Phase 2a: Phi-4 邏輯審查 + SmolLM3 標籤洩漏稽核（並行 LLM）
  Phase 2b: rule_checker 繁簡辨別（同步規則，不佔 timeout）
  → 任一否決即 unanimous=False

intent 模式（三票）：
  三模型各自分類，完全一致才算共識

summary 模式：
  E4B 主輸出，三模型結果存 individual_results

否決失效條件：LLM 審查員失敗（逾時/錯誤）= 棄權；規則稽核永不失敗
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

# ── Soul 載入 ──
def _load_soul(name):
    # type: (str) -> str
    """從 docs/soul/SOUL_<NAME>.md 載入靈魂定義。找不到時回傳空字串。"""
    try:
        magi_root = os.environ.get(
            "MAGI_ROOT",
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        soul_path = os.path.join(magi_root, "docs", "soul", "SOUL_{}.md".format(name.upper()))
        with open(soul_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


# 啟動時載入（module level，只讀一次）
_SOUL_CASPER = _load_soul("CASPER")
_SOUL_MELCHIOR = _load_soul("MELCHIOR")
_SOUL_BALTHASAR = _load_soul("BALTHASAR")

# ── 三模型身份對照 ──
# E4B=Casper（仲裁者/生產者），Phi-4=Melchior（科學家/邏輯審查），SmolLM3=Balthasar（實用主義者/格式稽核）
SOUL_NAME_MAP = {
    "primary": "Casper",
    "phi4":    "Melchior",
    "smol":    "Balthasar",
    "rule_sc": "規則稽核",
    "output_guard": "輸出防衛",
}

# ── 三模型性格定義（soul 已注入） ──
ENSEMBLE_ROLES = {
    "primary": {
        "name": "Casper (MAGI-01)",
        "port": OMLX_E4B_PORT,
        "base": OMLX_E4B_BASE,
        "soul": _SOUL_CASPER,
    },
    "phi4": {
        "name": "Melchior (MAGI-02)",
        "port": OMLX_PHI4_PORT,
        "base": OMLX_PHI4_BASE,
        "soul": _SOUL_MELCHIOR,
    },
    "smol": {
        "name": "Balthasar (MAGI-03)",
        "port": OMLX_SMOL_PORT,
        "base": OMLX_SMOL_BASE,
        "soul": _SOUL_BALTHASAR,
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
        try:
            from skills.engine.error_classifier import classify_error
            ce = classify_error(e, provider="omlx")
            return {
                "success": False,
                "error": str(e),
                "text": "",
                "error_classified": ce.reason,
                "retryable": ce.retryable,
                "should_compress": ce.should_compress,
                "should_fallback": ce.should_fallback,
            }
        except Exception:
            return {"success": False, "error": str(e), "text": ""}


def _call_omlx_chat_multiturn(
    base_url,       # type: str
    model_hint,     # type: str
    messages,       # type: List[Dict[str, str]]
    timeout_sec=DEFAULT_ENSEMBLE_TIMEOUT,  # type: int
    max_tokens=512, # type: int
):
    # type: (...) -> Dict[str, Any]
    """多輪對話版 oMLX 呼叫。供 ReAct 引擎使用。"""
    try:
        import requests  # type: ignore

        try:
            models_resp = requests.get("{}/v1/models".format(base_url), timeout=5)
            models_data = models_resp.json()
            model_id = models_data["data"][0]["id"] if models_data.get("data") else model_hint
        except Exception:
            model_id = model_hint

        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        resp = requests.post(
            "{}/v1/chat/completions".format(base_url),
            json=payload,
            timeout=timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return {"success": True, "text": text, "model": model_id}
    except Exception as e:
        try:
            from skills.engine.error_classifier import classify_error
            ce = classify_error(e, provider="omlx")
            return {
                "success": False,
                "error": str(e),
                "text": "",
                "error_classified": ce.reason,
                "retryable": ce.retryable,
                "should_compress": ce.should_compress,
                "should_fallback": ce.should_fallback,
            }
        except Exception:
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
def _build_reviewer_system(soul_text, role_instruction):
    # type: (str, str) -> str
    """將 soul 與審查員指令合併成 system prompt。soul 提供身份，role_instruction 提供任務。"""
    if soul_text:
        # 只取 soul 前段（Prime Directives + Behavior）避免 token 浪費
        soul_head = soul_text[:800].strip()
        return "{}\n\n---\n{}".format(soul_head, role_instruction)
    return role_instruction


_PHI4_REVIEWER_SYSTEM = _build_reviewer_system(
    _SOUL_MELCHIOR,
    (
        "現在任務：以 Melchior 的身份，審查下面這份法律回答有無邏輯錯誤或法條引用錯誤。\n"
        "規則：\n"
        "- 沒有問題：只回覆 OK，不要加任何其他字\n"
        "- 有問題：只回覆 VETO: 後接一句具體錯誤描述（例如：VETO: 第184條第一項前段要件漏掉違法性）\n"
        "禁止重複問題或重新回答問題，禁止回覆超過一行。"
    )
)

_SMOL_REVIEWER_SYSTEM = _build_reviewer_system(
    _SOUL_BALTHASAR,
    (
        "現在任務：以 Balthasar 的身份，審查下面這份回答是否出現以下任何一種問題：\n"
        "  (A) 洩漏內部標籤，例如 [使用者陳述]、[檢索線索]、[衍生推論]、[已驗證事實]\n"
        "  (B) Persona 跑題，例如出現「身為 CASPER」「我是 AI 助理」「身為語言模型」\n"
        "規則：\n"
        "- 沒有問題：只回覆 OK，不要加任何其他字\n"
        "- 有問題：只回覆 VETO: 後接一句描述（例如：VETO: 出現[使用者陳述]標籤）\n"
        "禁止重複問題內容，禁止回覆超過一行，禁止做任何翻譯或繁簡判斷。"
    )
)

# ── 規則式繁簡稽核器（不用 LLM） ──
# 收錄法律文書中最常出現的簡體字（僅含與繁體碼位不同的字）
# 原則：寧少勿多，確保不誤判。常見繁體字不在此列。
_SC_LEGAL_CHARS = frozenset(
    # 確認為簡體專用字（繁體寫法不同 Unicode code point）
    # 格式：簡體字（← 對應繁體）
    "损"  # 損
    "权"  # 權
    "责"  # 責
    "证"  # 證
    "诉"  # 訴
    "处"  # 處
    "规"  # 規
    "进"  # 進
    "对"  # 對
    "认"  # 認
    "时"  # 時
    "问"  # 問
    "说"  # 說
    "来"  # 來
    "过"  # 過
    "义"  # 義
    "务"  # 務
    "类"  # 類
    "协"  # 協
    "签"  # 簽
    "举"  # 舉
    "书"  # 書
    "审"  # 審
    "长"  # 長
    "会"  # 會
    "还"  # 還
    "为"  # 為
    "从"  # 從
    "发"  # 發
    "开"  # 開
    "关"  # 關
    "应"  # 應
    "现"  # 現
    "给"  # 給
    "让"  # 讓
    "边"  # 邊
    "单"  # 單
    "实"  # 實
    "续"  # 續
    "区"  # 區
    "动"  # 動
    "结"  # 結
    "请"  # 請
    "们"  # 們
    "违"  # 違
    "约"  # 約
    "赔"  # 賠
    "据"  # 據
    "议"  # 議
    "订"  # 訂
    "讨"  # 討
    "论"  # 論
    "题"  # 題
)
# 刻意排除 行、理、定 等繁簡共用字（code point 相同，加入會誤判繁體）


def _check_simplified_chinese(text):
    # type: (str) -> tuple
    """規則式繁簡偵測。回傳 (has_simplified: bool, found_chars: list)。
    只檢查 _SC_LEGAL_CHARS 中已知簡體字，不做完整轉換。
    """
    found = [c for c in text if c in _SC_LEGAL_CHARS]
    # 去重保序
    seen = set()
    unique_found = []
    for c in found:
        if c not in seen:
            seen.add(c)
            unique_found.append(c)
    return bool(unique_found), unique_found


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
        try:
            for future in concurrent.futures.as_completed(fs, timeout=timeout_sec + 5):
                try:
                    key, result = future.result()
                    results[key] = result
                except Exception as e:
                    key = fs[future]
                    results[key] = {"success": False, "error": str(e), "text": ""}
        except concurrent.futures.TimeoutError:
            # BUG-40: 審查員全部超時 → 棄權而非讓整個請求 500
            import logging as _log
            _log.getLogger(__name__).warning("Phase 2 reviewers timed out (timeout=%ds), proceeding with partial results", timeout_sec)
            for future, key in fs.items():
                if key not in results:
                    results[key] = {"success": False, "error": "reviewer_timeout", "text": ""}

    return results


def _build_review_consensus(original_prompt, primary_answer, review_results, task_type="chat"):
    # type: (str, str, Dict[str, Any], str) -> ConsensusResult
    """Phase 2 審查結果 → ConsensusResult。"""
    vetoed_by = []
    veto_reasons = []

    role_labels = {
        "phi4": "Melchior",
        "smol": "Balthasar",
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

    # 規則式繁簡稽核（不用 LLM，永不失敗）
    has_sc, sc_chars = _check_simplified_chinese(primary_answer)
    if has_sc:
        vetoed_by.append("rule_sc")
        veto_reasons.append("簡體字偵測（規則）：{}".format("、".join(sc_chars[:8])))
    review_verdicts["rule_sc"] = "VETO: {}".format(sc_chars[:8]) if has_sc else "OK"

    # 規則式事實溯源稽核（rule_fact_grounding）
    # 答案中引用的法條號碼，必須在 prompt（含記憶/網路 context）中有依據。
    # 若 LLM 憑訓練知識自行生成「第184條」等引用，此規則將觸發否決。
    try:
        from api.hallucination_guard import check_fact_grounding as _check_fg
        _fg_grounded, _fg_ungrounded = _check_fg(primary_answer, [original_prompt])
        if not _fg_grounded:
            vetoed_by.append("rule_fact_grounding")
            veto_reasons.append(
                "法條引用未有依據（溯源檢查）：{}".format("、".join(_fg_ungrounded[:5]))
            )
            _log.getLogger(__name__).warning(
                "[rule_fact_grounding] VETO — ungrounded refs: %s",
                _fg_ungrounded,
            )
        review_verdicts["rule_fact_grounding"] = (
            "VETO: {}".format(_fg_ungrounded[:5]) if not _fg_grounded else "OK"
        )
    except Exception as _fg_err:
        _log.getLogger(__name__).debug(
            "[rule_fact_grounding] skipped: %s", _fg_err
        )
        review_verdicts["rule_fact_grounding"] = "(skipped)"

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
    prompt,          # type: str
    system="",       # type: str
    timeout_sec=DEFAULT_ENSEMBLE_TIMEOUT,  # type: int
    max_tokens=1024, # type: int
    task_type="chat",# type: str
):
    # type: (...) -> ConsensusResult
    """兩階段審查模式（正式入口）。

    Phase 1: Casper (E4B) 生成答案（注入 SOUL_CASPER）
    Phase 2: Melchior (Phi-4) + Balthasar (SmolLM3) 並行審查（max_tokens=60，只回 OK/VETO）
    任一審查員否決 → unanimous=False，veto_reasons 說明原因

    回應格式由 format_magi_response() 決定：
      unanimous=True  → 以「MAGI」之名輸出（三哲人共識）
      unanimous=False → 標明是哪位哲人的意見或哪位哲人提出異議

    適用：法律問答、文件摘要、任何需要品質把關的回覆
    不適用：意圖分類（用 ensemble_classify_intent）、純閒聊
    """
    role = ENSEMBLE_ROLES["primary"]

    # Casper soul + 呼叫方傳入的 system 指令合併
    soul = role.get("soul", "")
    if soul and system:
        sys_prompt = "{}\n\n---\n{}".format(soul, system)
    elif soul:
        sys_prompt = soul
    else:
        sys_prompt = system or "你是 MAGI 法律助理，請用繁體中文回答。"

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


# ── Feature flag ──
# 預設值改為 "1"（2026-04-20）：ReAct 工具呼叫正式啟用。
# 若要停用（如測試或緊急回滾），設定 MAGI_ENSEMBLE_TOOLS=0。
_ENSEMBLE_TOOLS_ENABLED = os.environ.get("MAGI_ENSEMBLE_TOOLS", "1").strip().lower() in {"1", "true", "yes", "on"}


def ensemble_chat_with_tools(
    prompt,          # type: str
    system="",       # type: str
    timeout_sec=DEFAULT_ENSEMBLE_TIMEOUT,  # type: int
    max_tokens=1024, # type: int
    task_type="chat",# type: str
    context="",      # type: str
):
    # type: (...) -> ConsensusResult
    """工具增強版 ensemble 入口（Casper ReAct + Melchior/Balthasar 審查）。

    Phase 1: Casper (E4B) 用 ReAct 引擎推理，可呼叫工具
    Phase 2: Melchior + Balthasar 並行審查最終答案
    Phase 3: 規則稽核 + 共識判定

    需要 MAGI_ENSEMBLE_TOOLS=1 才會真正啟用 ReAct。
    若 flag=0 或 ReAct 失敗，自動 fallback 到 ensemble_chat_verified()。
    """
    if not _ENSEMBLE_TOOLS_ENABLED:
        return ensemble_chat_verified(
            prompt=prompt, system=system, timeout_sec=timeout_sec,
            max_tokens=max_tokens, task_type=task_type,
        )

    # Phase 1: ReAct on E4B
    react_answer = ""
    react_trace = {}  # type: Dict[str, Any]
    tools_used = []  # type: List[str]

    try:
        from skills.engine.react_engine import ReActEngine
        casper_soul = ENSEMBLE_ROLES["primary"].get("soul", "")
        # soul + 呼叫方 system 合併
        full_soul = "{}\n\n---\n{}".format(casper_soul, system).strip() if system and casper_soul else (casper_soul or system or "")

        react_timeout = max(30, timeout_sec - 20)  # 留 20s 給 Phase 2

        engine = ReActEngine.for_omlx(
            user_query=prompt,
            max_steps=5,
            total_timeout=react_timeout,
            soul_text=full_soul,
        )
        result = engine.run(prompt, context=context)

        react_trace = {
            "steps": result.get("steps", 0),
            "tools_used": result.get("tools_used", []),
            "elapsed_sec": result.get("elapsed_sec", 0),
            "partial": result.get("partial", False),
        }
        tools_used = result.get("tools_used", [])

        if result.get("success") and result.get("answer"):
            react_answer = result["answer"]
            logger.info("ReAct 成功：%d 步，工具=%s，耗時=%.1fs",
                        result["steps"], tools_used, result["elapsed_sec"])
    except Exception as e:
        logger.warning("ReAct 引擎失敗，fallback 到無工具模式：%s", e)
        react_trace = {"error": str(e)}

    # Fallback: ReAct 失敗 → 走舊的 _call_omlx_chat 無工具模式
    if not react_answer:
        logger.info("ReAct 無答案，fallback 到 ensemble_chat_verified")
        return ensemble_chat_verified(
            prompt=prompt, system=system, timeout_sec=timeout_sec,
            max_tokens=max_tokens, task_type=task_type,
        )

    # Phase 2: Melchior + Balthasar 審查
    review_timeout = max(15, timeout_sec - int(react_trace.get("elapsed_sec", 0)) - 5)
    review_results = _ensemble_review(prompt, react_answer, timeout_sec=review_timeout)

    # Phase 3: 共識判定
    cr = _build_review_consensus(prompt, react_answer, review_results, task_type=task_type)

    # 追加 ReAct trace 到 individual_results 供 debug
    cr.individual_results["react_trace"] = react_trace
    cr.individual_results["tools_used"] = tools_used

    return cr


def format_magi_response(cr):
    # type: (ConsensusResult) -> str
    """將 ConsensusResult 格式化為對外回應文字。

    unanimous=True  → 「MAGI：<answer>」（三哲人共識，不揭露個別身份）
    unanimous=False → 輸出 Casper 的答案 + 標明哪位哲人提出異議及原因
    result=None     → 回報系統故障
    """
    if cr.result is None:
        err = ""
        if cr.individual_results:
            err = cr.individual_results.get("primary_error", "")
        return "MAGI 系統故障，無法生成回應。{}".format("（{}）".format(err) if err else "")

    if cr.unanimous:
        text = cr.result
        # 共識 + 有使用工具 → 附上來源標註
        tools_used = cr.individual_results.get("tools_used", [])
        if tools_used:
            # 去重保序
            seen = set()
            unique = []
            for t in tools_used:
                if t not in seen:
                    seen.add(t)
                    unique.append(t)
            text += "\n\n（參考資料來源：{}）".format("、".join(unique))
        return text

    # 有異議：輸出答案 + 附上哪位哲人有意見
    lines = [cr.result, ""]
    lines.append("─── 三哲人意見分歧 ───")
    # veto_reasons 與 vetoed_by 一一對應（同索引）
    for i, veto_key in enumerate(cr.vetoed_by):
        name = SOUL_NAME_MAP.get(veto_key, veto_key)
        if i < len(cr.veto_reasons):
            reason_raw = cr.veto_reasons[i]
            # 去掉 reason 前面的 "Name: " 前綴（避免重複顯示名字）
            colon_idx = reason_raw.find(": ")
            reason = reason_raw[colon_idx + 2:].strip() if colon_idx >= 0 else reason_raw
        else:
            reason = "（未說明原因）"
        lines.append("【{}】異議：{}".format(name, reason))
    return "\n".join(lines)


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
