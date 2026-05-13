# -*- coding: utf-8 -*-
"""
contacts_bridge.py
==================
Apple 通訊錄（Contacts.framework）整合模組。

透過 AppleScript 查詢 Apple 通訊錄，自動比對案件當事人、對造律師。
用於 LINE 通知時自動帶入正確的稱謂。

整合點：
- 案件管理：當事人自動比對通訊錄
- LINE 通知：自動帶入聯絡資訊
- 對造律師查詢：快速找到聯絡方式
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger("ContactsBridge")


def _run_osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    """執行 AppleScript。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "osascript not found"


def _escape(text: str) -> str:
    """轉義 AppleScript 字串。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# 查詢聯絡人
# ---------------------------------------------------------------------------

def search_contact(name: str) -> Optional[dict]:
    """
    在 Apple 通訊錄中搜尋聯絡人。

    Args:
        name: 姓名（部分匹配）

    Returns:
        {"name": str, "phone": str, "email": str, "organization": str}
        或 None
    """
    name_esc = _escape(name)
    script = f'''
    tell application "Contacts"
        set matchedPeople to (every person whose name contains "{name_esc}")
        if (count of matchedPeople) > 0 then
            set p to item 1 of matchedPeople
            set pName to name of p
            set pOrg to organization of p
            set pPhone to ""
            set pEmail to ""
            try
                set pPhone to value of first phone of p
            end try
            try
                set pEmail to value of first email of p
            end try
            return pName & "||" & pPhone & "||" & pEmail & "||" & pOrg
        end if
        return ""
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok and output:
        parts = output.split("||")
        if len(parts) >= 4:
            return {
                "name": parts[0],
                "phone": parts[1],
                "email": parts[2],
                "organization": parts[3] if parts[3] != "missing value" else "",
            }
    return None


def search_contacts(name: str, limit: int = 5) -> list[dict]:
    """
    搜尋多個匹配的聯絡人。

    Args:
        name: 姓名（部分匹配）
        limit: 最大回傳筆數

    Returns:
        [{"name": str, "phone": str, "email": str, "organization": str}, ...]
    """
    name_esc = _escape(name)
    script = f'''
    tell application "Contacts"
        set matchedPeople to (every person whose name contains "{name_esc}")
        set maxCount to {limit}
        set resultList to ""
        repeat with i from 1 to (minimum of {{count of matchedPeople, maxCount}})
            set p to item i of matchedPeople
            set pName to name of p
            set pOrg to organization of p
            set pPhone to ""
            set pEmail to ""
            try
                set pPhone to value of first phone of p
            end try
            try
                set pEmail to value of first email of p
            end try
            set resultList to resultList & pName & "||" & pPhone & "||" & pEmail & "||" & pOrg & "\\n"
        end repeat
        return resultList
    end tell
    '''
    ok, output = _run_osascript(script, timeout=15)
    if not ok or not output:
        return []

    contacts = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("||")
        if len(parts) >= 4:
            contacts.append({
                "name": parts[0],
                "phone": parts[1],
                "email": parts[2],
                "organization": parts[3] if parts[3] != "missing value" else "",
            })
    return contacts


def get_contact_count() -> int:
    """取得通訊錄聯絡人總數。"""
    script = '''
    tell application "Contacts"
        return (count of every person) as text
    end tell
    '''
    ok, output = _run_osascript(script)
    if ok:
        try:
            return int(output)
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# 律師查詢便捷函式
# ---------------------------------------------------------------------------

def search_lawyer(name: str) -> Optional[dict]:
    """
    搜尋律師聯絡資訊。

    優先匹配「X律師」或組織含「事務所」的聯絡人。
    """
    # 先搜「X律師」
    result = search_contact(f"{name}律師")
    if result:
        return result

    # 再搜原名
    results = search_contacts(name, limit=5)
    for r in results:
        org = r.get("organization", "")
        if "事務所" in org or "律師" in org:
            return r

    # 回傳第一個匹配
    return results[0] if results else None


def format_contact_info(contact: dict) -> str:
    """格式化聯絡人資訊為顯示字串。"""
    parts = [contact.get("name", "")]
    if contact.get("organization"):
        parts.append(f"（{contact['organization']}）")
    if contact.get("phone"):
        parts.append(f"📞 {contact['phone']}")
    if contact.get("email"):
        parts.append(f"✉️ {contact['email']}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        count = get_contact_count()
        print(f"Apple Contacts: {count} contacts")
        print("\nUsage: python contacts_bridge.py <name>")
    else:
        name = sys.argv[1]
        print(f"Searching for: {name}")
        results = search_contacts(name)
        if results:
            for r in results:
                print(f"  {format_contact_info(r)}")
        else:
            print("  No contacts found.")
