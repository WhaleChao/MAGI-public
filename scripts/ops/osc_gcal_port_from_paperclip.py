#!/usr/bin/env python3
"""把 PaperClip 桌面版的 token.pickle + credentials.json 轉成 MAGI 格式。

用途：使用者已在 PaperClip 內完成 Google 授權，不想再走一次 OAuth 同意流程。
此腳本把：
  /Applications/Paperclip.app/Contents/MacOS/token.pickle
  /Applications/Paperclip.app/Contents/MacOS/credentials.json

轉成：
  ~/.magi/google/token.json            (Credentials.to_json() 格式)
  + 寫 settings.gcal_client_id / gcal_client_secret 到 MAGI DB

使用：
    /Users/ai/Desktop/MAGI_v2/venv/bin/python3 \
        /Users/ai/Desktop/MAGI_v2/scripts/ops/osc_gcal_port_from_paperclip.py
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from pathlib import Path

PAPERCLIP_TOKEN = Path("/Applications/Paperclip.app/Contents/MacOS/token.pickle")
PAPERCLIP_CREDS = Path("/Applications/Paperclip.app/Contents/MacOS/credentials.json")

MAGI_TOKEN = Path.home() / ".magi" / "google" / "token.json"


def main() -> int:
    sys.path.insert(0, "/Users/ai/Desktop/MAGI_v2")

    if not PAPERCLIP_TOKEN.exists():
        print(f"❌ PaperClip token.pickle 不存在: {PAPERCLIP_TOKEN}")
        return 1
    if not PAPERCLIP_CREDS.exists():
        print(f"❌ PaperClip credentials.json 不存在: {PAPERCLIP_CREDS}")
        return 1

    # 1. 解 pickle 取 Credentials
    print(f"→ 讀取 {PAPERCLIP_TOKEN}")
    try:
        with open(PAPERCLIP_TOKEN, "rb") as f:
            creds = pickle.load(f)
    except Exception as e:
        print(f"❌ pickle.load 失敗: {type(e).__name__}: {e}")
        print("   可能 google-auth 版本不相容。")
        return 2

    if not hasattr(creds, "to_json"):
        print(f"❌ pickle 內物件不是 Credentials: {type(creds)}")
        return 3

    # 2. 寫 MAGI 格式
    MAGI_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    MAGI_TOKEN.write_text(creds.to_json())
    MAGI_TOKEN.chmod(0o600)
    print(f"✅ 寫入 {MAGI_TOKEN}")
    print(f"   client_id: {getattr(creds, 'client_id', '?')[:30]}...")
    print(f"   token expired: {getattr(creds, 'expired', '?')}")
    print(f"   has refresh_token: {bool(getattr(creds, 'refresh_token', None))}")

    # 3. 讀 credentials.json 取 client_id + secret，寫 MAGI settings 表
    print(f"→ 讀取 {PAPERCLIP_CREDS}")
    creds_data = json.loads(PAPERCLIP_CREDS.read_text())
    inst = creds_data.get("installed") or creds_data.get("web") or {}
    client_id = inst.get("client_id", "")
    client_secret = inst.get("client_secret", "")

    if not client_id or not client_secret:
        print(f"⚠️  credentials.json 內無 client_id/secret，跳過 DB 寫入")
        return 0

    try:
        from api.osc.utils import _osc_exec
        for key, val, desc in [
            ("gcal_client_id", client_id, "Google OAuth client ID（從 PaperClip 移植）"),
            ("gcal_client_secret", client_secret, "Google OAuth client secret"),
            ("gcal_calendar_id", "primary", "目標行事曆 ID"),
        ]:
            _osc_exec(
                """INSERT INTO settings (`key`, value, description)
                   VALUES (%s,%s,%s)
                   ON DUPLICATE KEY UPDATE value=VALUES(value)""",
                (key, val, desc),
                fetch="none",
            )
        print(f"✅ 寫入 settings.gcal_client_id / gcal_client_secret / gcal_calendar_id")
    except Exception as e:
        print(f"⚠️  DB 寫入失敗（可手動填）: {type(e).__name__}: {e}")
        print(f"    Client ID: {client_id}")
        print(f"    Client Secret: {client_secret}")
        return 4

    print()
    print("✅ 移植完成。下一步：")
    print("   1. 開瀏覽器到 OSC admin tab")
    print("   2. 在「📅 Google Calendar 同步」section 確認三個欄位已自動填入")
    print("   3. 直接點「⏫ 立即同步」（不必再走「🔗 連線授權」）")
    print()
    print("   注意：PaperClip 用 OAuth 'installed' 類型，MAGI 用 'web' 類型。")
    print("   token.pickle 內的 refresh_token 跨類型可用，但若 token 失效需重新授權，")
    print("   此時要在 Google Cloud Console 把 OAuth client 改 'Web application' 類型，")
    print("   並加 redirect_uri http://127.0.0.1:5002/api/osc/gcal/auth/callback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
