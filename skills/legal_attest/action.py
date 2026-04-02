import os
import re
import json
import logging
import uuid
import sys
import argparse
from pathlib import Path

# Provide access to MAGI paths (env-configurable, no hardcoded user path)
_MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", os.path.expanduser("~/Desktop/MAGI"))
sys.path.insert(0, os.path.abspath(_MAGI_ROOT))
from skills.legal_attest.generator import core

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("MAGI_EXPORT_BASE_URL", "").rstrip("/")

AGENT_DIR = Path(_MAGI_ROOT) / ".agent"
STATE_PATH = AGENT_DIR / "legal_attest_state.json"

def _load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as _e:
            logger.warning("legal_attest state load failed: %s", _e)
    return {}

def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def handle_chat(user_id: str, message: str) -> str:
    state = _load_state()
    user_state = state.get(user_id, {"step": "init"})
    
    if message == "init":
        user_state = {"step": "ask_sender_name"}
        state[user_id] = user_state
        _save_state(state)
        return "好的，我們來草擬存證信函。\\n(隨時可以回覆「取消」退出本流程)\\n\\n首先，請告訴我**寄件人**的姓名或公司名稱："
        
    step = user_state.get("step")
    
    if step == "ask_sender_name":
        user_state["sender_name"] = message.strip()
        user_state["step"] = "ask_sender_addr"
        state[user_id] = user_state
        _save_state(state)
        return f"寄件人：「{user_state['sender_name']}」。\\n接下來，請告訴我**寄件人**的地址？"
        
    elif step == "ask_sender_addr":
        user_state["sender_addr"] = message.strip()
        user_state["step"] = "ask_receiver_name"
        state[user_id] = user_state
        _save_state(state)
        return "收到。\\n請問**收件人**的姓名或公司名稱？"
        
    elif step == "ask_receiver_name":
        user_state["receiver_name"] = message.strip()
        user_state["step"] = "ask_receiver_addr"
        state[user_id] = user_state
        _save_state(state)
        return f"收件人：「{user_state['receiver_name']}」。\\n請問**收件人**的地址？"
        
    elif step == "ask_receiver_addr":
        user_state["receiver_addr"] = message.strip()
        user_state["step"] = "ask_content"
        state[user_id] = user_state
        _save_state(state)
        return "好的，雙方資訊已收集完畢。\\n最後，請貼上存證信函的**詳細內文**（如果字數較多，可以分段送出，完成後回覆「OK」）："
        
    elif step == "ask_content":
        msg_upper = message.strip().upper()
        if msg_upper in ["OK", "好了", "完成", "結束"]:
            if not user_state.get("content"):
                return "您還沒有提供任何內文喔！請貼上內文後再回覆「OK」。"
            
            user_state["step"] = "generating"
            state[user_id] = user_state
            _save_state(state)
            
            # Generate the PDF
            try:
                export_dir = os.environ.get("MAGI_EXPORT_DIR",
                                            os.path.join(_MAGI_ROOT, "exports"))
                os.makedirs(export_dir, exist_ok=True)
                pdf_filename = f"legal_attest_{uuid.uuid4().hex[:8]}.pdf"
                output_path = os.path.join(export_dir, pdf_filename)
                
                sender_name_list = [[user_state["sender_name"]]]
                sender_addr_list = [user_state["sender_addr"]]
                receiver_name_list = [[user_state["receiver_name"]]]
                receiver_addr_list = [user_state["receiver_addr"]]
                
                core.generate_text_and_letter(
                    sender_name_list, sender_addr_list,
                    receiver_name_list, receiver_addr_list,
                    [], [],  # No CC for simple chat flow
                    user_state["content"]
                )
                core.merge_text_and_letter(output_path)
                core.clean_temp_files()
                
                # Clear state
                del state[user_id]
                _save_state(state)
                
                dl_link = f"{_BASE_URL}/exports/{pdf_filename}" if _BASE_URL else output_path
                return f"✅ 存證信函已產生完畢！\\n下載路徑：{dl_link}\\n\\n請列印後帶去郵局寄出喔！"
            except Exception as e:
                logger.error(f"Failed to generate legal attest: {e}")
                del state[user_id]
                _save_state(state)
                return f"❌ 產生 PDF 時發生錯誤：{e}"
        else:
            # Append content
            prev_content = user_state.get("content", "")
            user_state["content"] = prev_content + "\\n" + message.strip() if prev_content else message.strip()
            state[user_id] = user_state
            _save_state(state)
            return "已記錄。（如果還有後續內容請繼續傳送；若已完成請回覆「OK」）"
    
    del state[user_id]
    _save_state(state)
    return "✅ 存證信函狀態已重置。"


def main():
    parser = argparse.ArgumentParser(description="MAGI legal_attest — 存證信函產生器")
    parser.add_argument("--task", required=True)
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()

    task = args.task.strip()

    if task == "help":
        print(json.dumps({
            "skill": "legal_attest",
            "description": "存證信函產生器（互動式對話驅動）",
            "tasks": [
                {"name": "help", "description": "顯示本說明"},
                {"name": "status", "description": "顯示目前對話狀態"},
                {"name": "chat", "description": "互動式對話", "params": {"user_id": "str", "message": "str"}},
            ],
        }, ensure_ascii=False, indent=2))
    elif task == "status":
        state = _load_state()
        active = len(state)
        print(json.dumps({
            "ok": True,
            "active_sessions": active,
            "sessions": {uid: {"step": s.get("step")} for uid, s in state.items()},
        }, ensure_ascii=False, indent=2))
    elif task == "chat":
        raw = " ".join(args.rest) if args.rest else "{}"
        try:
            params = json.loads(raw)
        except json.JSONDecodeError:
            params = {"user_id": "cli", "message": raw}
        uid = params.get("user_id", "cli")
        msg = params.get("message", "init")
        reply = handle_chat(uid, msg)
        print(json.dumps({"ok": True, "reply": reply}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": False, "error": f"Unknown task: {task}"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
