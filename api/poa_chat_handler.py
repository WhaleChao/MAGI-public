# <MAGI_ROOT>/api/poa_chat_handler.py
# 委任狀/委託書/委任契約書/收據 三通道聊天式產生流程
# 仿照 legal_attest 的 handle_chat 做法，支援 TG / DC / LINE 觸發

import os
import re
import json
import sys
import uuid
import time
import logging
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[1]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_config_path

logger = logging.getLogger(__name__)

AGENT_DIR = Path(f"{_MAGI_ROOT}/.agent")
STATE_PATH = AGENT_DIR / "poa_chat_state.json"

_SKIP_WORDS = {"無", "沒有", "略", "跳過", "skip", "-"}

# ── 案件類型 & 角色對照 ──
CASE_TYPE_MAP = {
    "1": "民事", "民事": "民事", "民": "民事",
    "2": "刑事", "刑事": "刑事", "刑": "刑事",
    "3": "行政", "行政": "行政", "行": "行政",
}

ROLE_MAP_CRIMINAL = {
    "1": "辯護人", "辯護人": "辯護人", "辯護": "辯護人",
    "2": "告訴代理人", "告訴代理人": "告訴代理人", "告訴代理": "告訴代理人", "告代": "告訴代理人",
}


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 44, exc_info=True)
    return {}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_user(state: dict, user_id: str):
    if user_id in state:
        del state[user_id]
    _save_state(state)


def _load_config() -> dict:
    config = {
        "company_name": "偵理法律事務所",
        "default_lawyer": "喬政翔律師",
        "company_address_hl": "",
        "company_phone": "",
        "company_fax": "",
        "company_email": "",
        "bank_name": "",
        "bank_account_name": "",
        "bank_account_number": "",
    }
    try:
        cfg_path = str(get_config_path("config.json"))
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k in config:
                if cfg.get(k):
                    config[k] = cfg[k]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 80, exc_info=True)
    return config


# ─────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────

def _parse_case_type(msg: str) -> Optional[str]:
    m = msg.strip()
    for k, v in CASE_TYPE_MAP.items():
        if m == k or m == v:
            return v
    if "民" in m:
        return "民事"
    if "刑" in m:
        return "刑事"
    if "行政" in m:
        return "行政"
    return None


def _parse_role(msg: str, case_type: str) -> Optional[str]:
    m = msg.strip()
    if case_type in ("民事", "行政"):
        return "代理人"
    if case_type == "刑事":
        for k, v in ROLE_MAP_CRIMINAL.items():
            if m == k or m == v:
                return v
        if "辯護" in m:
            return "辯護人"
        if "告訴" in m or "告代" in m:
            return "告訴代理人"
    return None


def _try_extract_fields_from_init(message: str) -> dict:
    """嘗試從使用者的初始訊息中解析出已知欄位（智慧解析）"""
    extracted = {}
    # 案件類型
    if "民事" in message:
        extracted["case_type"] = "民事"
    elif "刑事" in message:
        extracted["case_type"] = "刑事"
    elif "行政" in message:
        extracted["case_type"] = "行政"

    # 角色
    if "辯護" in message:
        extracted["role"] = "辯護人"
    elif "告訴代理" in message or "告代" in message:
        extracted["role"] = "告訴代理人"

    # 案號
    case_no_full = re.search(
        r"(\d{2,3}年度[\u4e00-\u9fff]{1,4}字第\d+號?)",
        message
    )
    if case_no_full:
        cn = case_no_full.group(1)
        extracted["case_no"] = cn if cn.endswith("號") else cn + "號"
    else:
        case_no_short = re.search(
            r"(\d{2,3})\s*([\u4e00-\u9fff]{1,4})\s*(\d+)",
            message
        )
        if case_no_short:
            y, cat, num = case_no_short.groups()
            extracted["case_no"] = f"{y}年度{cat}字第{num}號"

    # 當事人姓名
    name_match = re.search(r"(?:幫|替|為)\s*(\S{2,5})\s*(?:做|製作|產生|草擬|開)", message)
    if not name_match:
        name_match = re.search(r"(\S{2,5})\s*(?:的|之)\s*(?:委任狀|委託書|委任契約|契約書|收據)", message)
    if name_match:
        extracted["client_name"] = name_match.group(1)

    # 法院
    court_match = re.search(r"([\u4e00-\u9fff]{2,8}(?:地方|高等|最高)?\s*(?:法院|地院|檢察署))", message)
    if court_match:
        extracted["court"] = court_match.group(1)

    # 金額
    amount_match = re.search(r"(\d[\d,]+)\s*元", message)
    if amount_match:
        extracted["amount"] = amount_match.group(1).replace(",", "")

    return extracted


# ─────────────────────────────────────────────
# Preview builders
# ─────────────────────────────────────────────

def _build_poa_preview(us: dict) -> str:
    lines = [
        "📋 **委任狀預覽**",
        f"• 案件類型：{us.get('case_type', '—')}",
        f"• 角色：{us.get('role', '—')}",
        f"• 當事人：{us.get('client_name', '—')}",
        f"• 案號：{us.get('case_no') or '（空白）'}",
        f"• 股別：{us.get('branch') or '（空白）'}",
        f"• 案由：{us.get('case_reason') or '（空白）'}",
        f"• 法院/檢察署：{us.get('court') or '（空白）'}",
    ]
    if us.get("address"):
        lines.append(f"• 地址：{us['address']}")
    return "\n".join(lines)


def _build_contract_preview(us: dict) -> str:
    lines = [
        "📋 **委任契約書預覽**",
        f"• 當事人（委任人）：{us.get('client_name', '—')}",
        f"• 案由/事件：{us.get('case_reason') or '（空白）'}",
        f"• 委任範圍：{us.get('scope') or '（空白）'}",
        f"• 委任費用：{us.get('amount') or '（空白）'} 元",
        f"• 身分證字號：{us.get('tax_id') or '（空白）'}",
        f"• 聯絡電話：{us.get('phone') or '（空白）'}",
        f"• 電子信箱：{us.get('email') or '（空白）'}",
        f"• 通訊地址：{us.get('address') or '（空白）'}",
    ]
    return "\n".join(lines)


def _build_receipt_preview(us: dict) -> str:
    lines = [
        "📋 **收據預覽**",
        f"• 當事人：{us.get('client_name', '—')}",
        f"• 案由/事件：{us.get('case_reason') or '（空白）'}",
        f"• 費用項目：{us.get('fee_type') or '法律服務費'}",
        f"• 金額：{us.get('amount') or '（空白）'} 元",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# DOCX generation & export
# ─────────────────────────────────────────────

def _generate_and_export(us: dict) -> str:
    """根據 doc_type 產生 DOCX 並回傳 |||FILE_PATH||| 格式"""
    doc_type = us.get("doc_type", "poa")
    try:
        config = _load_config()
        export_dir = f"{_MAGI_ROOT}/exports"
        os.makedirs(export_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]

        if doc_type == "receipt":
            from api.osc_document_generator import generate_receipt
            data = {
                "委任人/當事人": us.get("client_name", ""),
                "案由/事件": us.get("case_reason", ""),
                "金額": us.get("amount", ""),
            }
            fee_type = us.get("fee_type") or "法律服務費"
            doc = generate_receipt(data, fee_type, config)
            filename = f"receipt_{stamp}_{token}.docx"
            text_part = (
                f"✅ **收據已產生！**\n\n"
                f"• 當事人：{us.get('client_name', '—')}\n"
                f"• 費用項目：{fee_type}\n"
                f"• 金額：{us.get('amount') or '—'} 元\n\n"
                f"請下載後確認內容。"
            )
        elif doc_type == "contract":
            from api.osc_document_generator import generate_engagement_agreement
            data = {
                "委任人/當事人": us.get("client_name", ""),
                "案由/事件": us.get("case_reason", ""),
                "委任範圍": us.get("scope", ""),
                "委任費用(數字)": us.get("amount", ""),
                "身分證字號": us.get("tax_id", ""),
                "聯絡電話": us.get("phone", ""),
                "電子信箱": us.get("email", ""),
                "通訊地址": us.get("address", ""),
            }
            doc = generate_engagement_agreement(data, config)
            filename = f"contract_{stamp}_{token}.docx"
            text_part = (
                f"✅ **委任契約書已產生！**\n\n"
                f"• 當事人：{us.get('client_name', '—')}\n"
                f"• 案由：{us.get('case_reason') or '—'}\n"
                f"• 費用：{us.get('amount') or '—'} 元\n\n"
                f"請下載後確認內容，雙方簽名後使用。"
            )
        else:
            from api.osc_document_generator import generate_poa
            data = {
                "案號": us.get("case_no", ""),
                "股別": us.get("branch", ""),
                "委任人/當事人": us.get("client_name", ""),
                "案由/事件": us.get("case_reason", ""),
                "法院/檢察署": us.get("court", ""),
                "通訊地址": us.get("address", ""),
                "聯絡電話": us.get("phone", ""),
                "身分證字號": us.get("tax_id", ""),
            }
            case_type = us.get("case_type", "民事")
            role = us.get("role", "代理人")
            doc = generate_poa(data, case_type, role, config)
            filename = f"poa_{stamp}_{token}.docx"
            text_part = (
                f"✅ **委任狀已產生！**\n\n"
                f"• 類型：{case_type}委任狀（{role}）\n"
                f"• 當事人：{us.get('client_name', '—')}\n"
                f"• 案號：{us.get('case_no') or '—'}\n\n"
                f"請下載後確認內容，列印簽名後使用。"
            )

        docx_path = os.path.join(export_dir, filename)
        doc.save(docx_path)
        return f"{text_part}|||FILE_PATH|||{docx_path}"
    except Exception as e:
        label = {"contract": "委任契約書", "receipt": "收據"}.get(doc_type, "委任狀")
        logger.error(f"{label} generation failed: {e}", exc_info=True)
        return f"❌ 產生{label}時發生錯誤：{e}"


# ─────────────────────────────────────────────
# Main chat handler
# ─────────────────────────────────────────────

def handle_chat(user_id: str, message: str) -> str:
    """
    多步對話流程，依序收集必要欄位。
    如果使用者初始訊息已包含部分資訊，智慧跳過已知步驟。
    支援兩種 doc_type: "poa"（委任狀）及 "contract"（委任契約書）。
    """
    state = _load_state()
    us = state.get(user_id, {})
    doc_type = us.get("doc_type", "poa")

    # ── 初始化（委任狀）──
    if message == "init":
        us = {"step": "start", "doc_type": "poa"}
        state[user_id] = us
        _save_state(state)
        return (
            "好的，我來幫您製作委任狀。\n"
            "(隨時可以回覆「取消」退出本流程)\n\n"
            "請問案件類型？\n"
            "1️⃣ 民事\n"
            "2️⃣ 刑事\n"
            "3️⃣ 行政\n\n"
            "（直接輸入數字或文字即可）"
        )

    # ── 初始化（委任契約書）──
    if message == "init_contract":
        us = {"step": "ask_client", "doc_type": "contract"}
        state[user_id] = us
        _save_state(state)
        return (
            "好的，我來幫您製作委任契約書。\n"
            "(隨時可以回覆「取消」退出本流程)\n\n"
            "請問**委任人**（當事人）的姓名？"
        )

    # ── 初始化（收據）──
    if message == "init_receipt":
        us = {"step": "ask_client", "doc_type": "receipt"}
        state[user_id] = us
        _save_state(state)
        return (
            "好的，我來幫您開收據。\n"
            "(隨時可以回覆「取消」退出本流程)\n\n"
            "請問**當事人**的姓名？"
        )

    # ── 智慧初始化 ──
    if message == "smart_init":
        raw = us.pop("_raw_message", "")
        extracted = _try_extract_fields_from_init(raw)
        us.update(extracted)
        doc_type = us.get("doc_type", "poa")

        if doc_type == "poa":
            if us.get("case_type") in ("民事", "行政") and "role" not in us:
                us["role"] = "代理人"
            if not us.get("case_type"):
                us["step"] = "start"
                state[user_id] = us
                _save_state(state)
                return (
                    "好的，我來幫您製作委任狀。\n"
                    "(隨時可以回覆「取消」退出本流程)\n\n"
                    "請問案件類型？\n"
                    "1️⃣ 民事\n2️⃣ 刑事\n3️⃣ 行政\n\n"
                    "（直接輸入數字或文字即可）"
                )
        elif doc_type in ("contract", "receipt"):
            pass  # 直接走 _advance

        return _advance_to_next_missing(state, user_id, us)

    # ── 無 step → 異常重置 ──
    if not us.get("step"):
        _clear_user(state, user_id)
        return "流程狀態異常，已重置。請重新輸入指令。"

    step = us.get("step", "start")
    msg = message.strip()

    # ── Step: 案件類型（POA only）──
    if step == "start":
        ct = _parse_case_type(msg)
        if not ct:
            return "抱歉，我沒有理解。請輸入：\n1️⃣ 民事\n2️⃣ 刑事\n3️⃣ 行政"
        us["case_type"] = ct
        if ct == "刑事":
            us["step"] = "ask_role"
            state[user_id] = us
            _save_state(state)
            return (
                f"案件類型：{ct}。\n\n"
                "請問您的身份是？\n"
                "1️⃣ 辯護人（被告方）\n"
                "2️⃣ 告訴代理人（告訴人方）\n\n"
                "（直接輸入數字或文字即可）"
            )
        else:
            us["role"] = "代理人"
            return _advance_to_next_missing(state, user_id, us)

    # ── Step: 角色（刑事限定）──
    if step == "ask_role":
        role = _parse_role(msg, us.get("case_type", "刑事"))
        if not role:
            return "抱歉，我沒有理解。請輸入：\n1️⃣ 辯護人\n2️⃣ 告訴代理人"
        us["role"] = role
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 當事人姓名 ──
    if step == "ask_client":
        if len(msg) < 1:
            return "請輸入當事人姓名（至少一個字）："
        us["client_name"] = msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 案號（POA）──
    if step == "ask_case_no":
        us["case_no"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 股別（POA）──
    if step == "ask_branch":
        us["branch"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 案由 ──
    if step == "ask_reason":
        us["case_reason"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 法院/檢察署（POA）──
    if step == "ask_court":
        us["court"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 委任範圍（contract）──
    if step == "ask_scope":
        us["scope"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 委任費用（contract）──
    if step == "ask_amount":
        if msg in _SKIP_WORDS:
            us["amount"] = ""
        else:
            # 嘗試只留數字
            digits = re.sub(r"[^\d]", "", msg)
            us["amount"] = digits if digits else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 費用項目（receipt）──
    if step == "ask_fee_type":
        us["fee_type"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 身分證字號 ──
    if step == "ask_tax_id":
        us["tax_id"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 電話 ──
    if step == "ask_phone":
        us["phone"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 電子信箱（contract）──
    if step == "ask_email":
        us["email"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 地址 ──
    if step == "ask_address":
        us["address"] = "" if msg in _SKIP_WORDS else msg
        return _advance_to_next_missing(state, user_id, us)

    # ── Step: 確認 ──
    if step == "confirm":
        upper = msg.upper()
        if upper in ["Y", "YES", "好", "確認", "OK", "可以", "對", "是", "沒問題", "確定"]:
            us["step"] = "generating"
            state[user_id] = us
            _save_state(state)
            result = _generate_and_export(us)
            _clear_user(state, user_id)
            return result
        elif upper in ["N", "NO", "否", "不對", "重來", "重新"]:
            if doc_type in ("contract", "receipt"):
                us = {"step": "ask_client", "doc_type": doc_type}
                state[user_id] = us
                _save_state(state)
                return "好的，我們重新開始。\n\n請問**當事人**的姓名？"
            else:
                us = {"step": "start", "doc_type": "poa"}
                state[user_id] = us
                _save_state(state)
                return (
                    "好的，我們重新開始。\n\n"
                    "請問案件類型？\n"
                    "1️⃣ 民事\n2️⃣ 刑事\n3️⃣ 行政"
                )
        else:
            label = {"contract": "委任契約書", "receipt": "收據"}.get(doc_type, "委任狀")
            return f"請回覆「確認」產生{label}，或「重來」重新填寫。"

    # 不明步驟，重置
    _clear_user(state, user_id)
    return "流程狀態異常，已重置。請重新輸入指令。"


# ─────────────────────────────────────────────
# Field advancement logic
# ─────────────────────────────────────────────

def _advance_to_next_missing(state: dict, user_id: str, us: dict) -> str:
    """檢查還缺哪些必填欄位，跳到下一個需要問的步驟。"""
    doc_type = us.get("doc_type", "poa")

    if doc_type == "contract":
        return _advance_contract(state, user_id, us)
    elif doc_type == "receipt":
        return _advance_receipt(state, user_id, us)
    else:
        return _advance_poa(state, user_id, us)


def _advance_poa(state: dict, user_id: str, us: dict) -> str:
    checks = [
        ("case_type",   "start",       None),
        ("role",        "ask_role",     lambda: us.get("case_type") == "刑事"),
        ("client_name", "ask_client",   None),
        ("case_no",     "ask_case_no",  None),
        ("branch",      "ask_branch",   None),
        ("case_reason", "ask_reason",   None),
        ("court",       "ask_court",    None),
    ]
    prompts = {
        "start":       "請問案件類型？\n1️⃣ 民事\n2️⃣ 刑事\n3️⃣ 行政",
        "ask_role":    "請問您的身份是？\n1️⃣ 辯護人（被告方）\n2️⃣ 告訴代理人（告訴人方）",
        "ask_client":  "請問**當事人**（委任人）的姓名？",
        "ask_case_no": "請問**案號**？（例如：114年度訴字第123號，無則輸入「略」）",
        "ask_branch":  "請問**股別**？（無則輸入「略」）",
        "ask_reason":  "請問**案由**？（例如：竊盜、損害賠償等，無則輸入「略」）",
        "ask_court":   "請問繫屬的**法院或檢察署**名稱？（例如：臺灣花蓮地方法院，無則輸入「略」）",
    }

    for field, step_name, condition in checks:
        if condition is not None and not condition():
            continue
        if field not in us:
            us["step"] = step_name
            state[user_id] = us
            _save_state(state)
            return _known_prefix(us, field, "poa") + prompts[step_name]

    # 選填地址
    if "address" not in us and us.get("step") != "confirm":
        us["step"] = "ask_address"
        state[user_id] = us
        _save_state(state)
        return "選填：請問當事人的**通訊地址**？（無則輸入「略」）"

    # 確認
    us["step"] = "confirm"
    state[user_id] = us
    _save_state(state)
    preview = _build_poa_preview(us)
    return f"{preview}\n\n以上資訊是否正確？回覆「確認」產生委任狀，或「重來」重新填寫。"


def _advance_contract(state: dict, user_id: str, us: dict) -> str:
    checks = [
        ("client_name", "ask_client"),
        ("case_reason", "ask_reason"),
        ("scope",       "ask_scope"),
        ("amount",      "ask_amount"),
        ("tax_id",      "ask_tax_id"),
        ("phone",       "ask_phone"),
        ("email",       "ask_email"),
        ("address",     "ask_address"),
    ]
    prompts = {
        "ask_client":  "請問**委任人**（當事人）的姓名？",
        "ask_reason":  "請問**案由/事件**？（例如：損害賠償、離婚等）",
        "ask_scope":   "請問**委任範圍**？（例如：第一審訴訟代理，無則輸入「略」）",
        "ask_amount":  "請問**委任費用**？（請輸入數字，例如：50000，無則輸入「略」）",
        "ask_tax_id":  "請問委任人的**身分證字號**？（無則輸入「略」）",
        "ask_phone":   "請問委任人的**聯絡電話**？（無則輸入「略」）",
        "ask_email":   "請問委任人的**電子信箱**？（無則輸入「略」）",
        "ask_address": "請問委任人的**通訊地址**？（無則輸入「略」）",
    }

    for field, step_name in checks:
        if field not in us:
            us["step"] = step_name
            state[user_id] = us
            _save_state(state)
            return _known_prefix(us, field, "contract") + prompts[step_name]

    # 確認
    us["step"] = "confirm"
    state[user_id] = us
    _save_state(state)
    preview = _build_contract_preview(us)
    return f"{preview}\n\n以上資訊是否正確？回覆「確認」產生委任契約書，或「重來」重新填寫。"


def _advance_receipt(state: dict, user_id: str, us: dict) -> str:
    checks = [
        ("client_name", "ask_client"),
        ("case_reason", "ask_reason"),
        ("fee_type",    "ask_fee_type"),
        ("amount",      "ask_amount"),
    ]
    prompts = {
        "ask_client":   "請問**當事人**的姓名？",
        "ask_reason":   "請問**案由/事件**？（無則輸入「略」）",
        "ask_fee_type": "請問**費用項目**名稱？（例如：律師酬金、裁判費代墊，預設為「法律服務費」，略則用預設）",
        "ask_amount":   "請問**金額**？（請輸入數字，例如：50000）",
    }

    for field, step_name in checks:
        if field not in us:
            us["step"] = step_name
            state[user_id] = us
            _save_state(state)
            return _known_prefix(us, field, "receipt") + prompts[step_name]

    # 確認
    us["step"] = "confirm"
    state[user_id] = us
    _save_state(state)
    preview = _build_receipt_preview(us)
    return f"{preview}\n\n以上資訊是否正確？回覆「確認」產生收據，或「重來」重新填寫。"


def _known_prefix(us: dict, current_field: str, doc_type: str) -> str:
    """回報目前已知的欄位"""
    known = []
    if doc_type == "poa":
        if us.get("case_type") and current_field != "case_type":
            known.append(f"案件類型：{us['case_type']}")
        if us.get("role") and current_field != "role":
            known.append(f"角色：{us['role']}")
    if us.get("client_name") and current_field != "client_name":
        known.append(f"當事人：{us['client_name']}")
    if us.get("case_no") and current_field != "case_no":
        known.append(f"案號：{us['case_no']}")
    if us.get("court") and current_field != "court":
        known.append(f"法院：{us['court']}")
    if doc_type == "contract":
        if us.get("amount") and current_field != "amount":
            known.append(f"費用：{us['amount']}元")

    if known:
        return "已知資訊：" + "、".join(known) + "\n\n"
    return ""
