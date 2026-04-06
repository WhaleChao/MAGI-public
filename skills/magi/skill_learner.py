# -*- coding: utf-8 -*-
"""
skill_learner.py — 自我改進技能學習系統
=========================================
靈感來源：Hermes Agent (NousResearch/hermes-agent)

核心概念：MAGI 在執行任務後，自動沉澱可重用的「技能檔案」(SKILL.md)。
下次遇到類似任務時，系統會自動載入相關技能，提升處理品質。

技能生命週期：
1. 任務執行 → 背景審查 → 判斷是否值得沉澱
2. 產生 SKILL.md（YAML frontmatter + Markdown 內容）
3. 存入技能庫 (.agent/skills/<category>/<name>/SKILL.md)
4. 未來任務啟動時，搜尋相關技能注入 system prompt
5. 執行中發現技能過時 → 即時 patch 更新

與 MAGI 既有機制的整合：
- night_talk kaizen → 可觸發技能審查
- Orchestrator skill dispatch → 技能索引可輔助路由
- mem_bridge recall → 技能可作為高信任度 context
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SkillLearner")

_MAGI_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = Path(os.environ.get(
    "MAGI_SKILLS_DIR",
    str(_MAGI_ROOT / ".agent" / "skills"),
))
SKILLS_INDEX_PATH = SKILLS_DIR / ".skills_index.json"
SKILL_REVIEW_LOG = _MAGI_ROOT / ".agent" / "skill_review_log.jsonl"

# ── 技能分類（法律 AI 專用）──────────────────────────────────────

LEGAL_CATEGORIES = {
    "litigation": "訴訟流程 — 起訴、答辯、調解、判決等程序性知識",
    "evidence": "證據處理 — 證據分類、舉證責任、閱卷技巧",
    "document": "書狀撰寫 — 書狀格式、引用規範、論述結構",
    "research": "法律研究 — 判決檢索、法條查詢、學說引用",
    "case_mgmt": "案件管理 — 期日管理、案件分類、進度追蹤",
    "client": "委託人溝通 — 說明技巧、風險告知、期望管理",
    "system": "系統操作 — MAGI 操作技巧、常見問題排除",
    "workflow": "工作流程 — 跨步驟協作、自動化排程、品質把關",
}

# ── 技能審查提示詞 ────────────────────────────────────────────────

SKILL_REVIEW_PROMPT = """請審查以下任務執行紀錄，判斷是否值得沉澱為可重用技能。

評估標準：
1. 這個任務是否涉及非顯而易見的做法？（需要嘗試、除錯、或經驗判斷）
2. 這個做法是否可能在未來重複使用？
3. 是否有特定的陷阱或注意事項值得記錄？

如果值得沉澱，請用以下 YAML+Markdown 格式輸出：

```yaml
name: skill-name-here
description: 一句話描述這個技能做什麼
category: {categories}
version: 1.0.0
platforms: [all]
tags: [tag1, tag2]
```

# 技能標題

## 適用時機
- 什麼情況下應該使用這個技能

## 不適用時機
- 什麼情況下不應該使用

## 步驟
1. 具體步驟...
2. ...

## 陷阱與注意事項
- 已知的坑...

## 驗證方式
- 如何確認成功...

---

如果不值得沉澱，請只回覆：「不需要沉澱。」

任務紀錄：
{task_record}"""

SKILL_PATCH_PROMPT = """以下技能在實際使用中發現需要更新。
請根據新的經驗修改技能內容，保持格式一致。

原始技能：
{original_skill}

新發現/更正：
{findings}

請輸出完整的更新後技能內容（保持 YAML frontmatter + Markdown 格式）。"""


# ══════════════════════════════════════════════════════════════════
# 資料結構
# ══════════════════════════════════════════════════════════════════

@dataclass
class SkillMeta:
    """技能元資料（從 YAML frontmatter 解析）"""
    name: str
    description: str
    category: str = "system"
    version: str = "1.0.0"
    author: str = "MAGI SkillLearner"
    platforms: List[str] = field(default_factory=lambda: ["all"])
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    use_count: int = 0
    last_used_at: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "author": self.author,
            "platforms": self.platforms,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
        }


# ══════════════════════════════════════════════════════════════════
# 技能檔案管理
# ══════════════════════════════════════════════════════════════════

def _ensure_skills_dir():
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for cat in LEGAL_CATEGORIES:
        (SKILLS_DIR / cat).mkdir(exist_ok=True)


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """解析 YAML frontmatter + Markdown body"""
    content = content.strip()
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    yaml_str = parts[1].strip()
    body = parts[2].strip()

    # 簡單 YAML 解析（不依賴 pyyaml）
    meta = {}
    for line in yaml_str.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        # 處理列表 [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip("'\"") for x in val[1:-1].split(",")]
            meta[key] = [x for x in items if x]
        else:
            meta[key] = val.strip("'\"")

    return meta, body


def _build_frontmatter(meta: Dict[str, Any]) -> str:
    """構建 YAML frontmatter"""
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            items = ", ".join(str(v) for v in val)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines)


def _skill_path(category: str, name: str) -> Path:
    """取得技能檔案路徑"""
    safe_name = re.sub(r"[^a-z0-9_-]", "-", name.lower().strip())
    safe_cat = category if category in LEGAL_CATEGORIES else "system"
    return SKILLS_DIR / safe_cat / safe_name / "SKILL.md"


def list_skills(category: str = "") -> List[Dict[str, Any]]:
    """列出所有技能（或指定分類）"""
    _ensure_skills_dir()
    skills = []

    search_dirs = [SKILLS_DIR / category] if category and category in LEGAL_CATEGORIES else \
                  [SKILLS_DIR / cat for cat in LEGAL_CATEGORIES]

    for cat_dir in search_dirs:
        if not cat_dir.exists():
            continue
        for skill_dir in sorted(cat_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                content = skill_file.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(content)
                skills.append({
                    "name": meta.get("name", skill_dir.name),
                    "description": meta.get("description", ""),
                    "category": cat_dir.name,
                    "version": meta.get("version", "1.0.0"),
                    "tags": meta.get("tags", []),
                    "path": str(skill_file),
                    "size": len(content),
                })
            except Exception as e:
                logger.debug("Failed to read skill %s: %s", skill_file, e)

    return skills


def get_skill(category: str, name: str) -> Optional[str]:
    """讀取完整技能內容"""
    path = _skill_path(category, name)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def save_skill(
    category: str,
    name: str,
    content: str,
    *,
    is_update: bool = False,
) -> Dict[str, Any]:
    """
    儲存技能檔案。

    Args:
        category: 技能分類
        name: 技能名稱
        content: 完整技能內容（YAML frontmatter + Markdown）
        is_update: 是否為更新（vs 新建）
    """
    _ensure_skills_dir()

    # 驗證 frontmatter
    meta, body = _parse_frontmatter(content)
    if not meta.get("name"):
        meta["name"] = name
    if not meta.get("description"):
        return {"success": False, "error": "Missing description in frontmatter"}

    # 時間戳
    now = datetime.now().isoformat()
    if not is_update:
        meta["created_at"] = now
    meta["updated_at"] = now

    # 重建完整內容
    full_content = _build_frontmatter(meta) + "\n\n" + body

    # 寫入
    path = _skill_path(category, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full_content, encoding="utf-8")

    # 更新索引
    _update_index()

    action = "updated" if is_update else "created"
    logger.info("Skill %s: %s/%s (%s)", action, category, name, path)

    return {
        "success": True,
        "action": action,
        "path": str(path),
        "category": category,
        "name": meta.get("name", name),
    }


def patch_skill(
    category: str,
    name: str,
    old_text: str,
    new_text: str,
) -> Dict[str, Any]:
    """
    局部更新技能（find-and-replace）— 比全量重寫更安全。
    靈感來自 Hermes Agent 的 patch action。
    """
    path = _skill_path(category, name)
    if not path.exists():
        return {"success": False, "error": f"Skill not found: {category}/{name}"}

    content = path.read_text(encoding="utf-8")
    if old_text not in content:
        # Fuzzy match: try stripping whitespace
        stripped_content = " ".join(content.split())
        stripped_old = " ".join(old_text.split())
        if stripped_old not in stripped_content:
            return {"success": False, "error": "old_text not found in skill"}
        # Use original for replacement
        content = content.replace(old_text.strip(), new_text.strip(), 1)
    else:
        content = content.replace(old_text, new_text, 1)

    # 更新時間
    meta, body = _parse_frontmatter(content)
    meta["updated_at"] = datetime.now().isoformat()
    content = _build_frontmatter(meta) + "\n\n" + body

    path.write_text(content, encoding="utf-8")
    _update_index()

    logger.info("Skill patched: %s/%s", category, name)
    return {"success": True, "action": "patched", "path": str(path)}


def delete_skill(category: str, name: str) -> Dict[str, Any]:
    """刪除技能"""
    path = _skill_path(category, name)
    if not path.exists():
        return {"success": False, "error": f"Skill not found: {category}/{name}"}

    import shutil
    shutil.rmtree(path.parent, ignore_errors=True)
    _update_index()
    logger.info("Skill deleted: %s/%s", category, name)
    return {"success": True, "action": "deleted"}


# ══════════════════════════════════════════════════════════════════
# 技能索引（高效搜尋用）
# ══════════════════════════════════════════════════════════════════

def _update_index():
    """重建技能索引 JSON"""
    skills = list_skills()
    index = {
        "updated_at": datetime.now().isoformat(),
        "total": len(skills),
        "skills": skills,
    }
    try:
        SKILLS_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        SKILLS_INDEX_PATH.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.debug("Failed to update skill index: %s", e)


def build_skills_prompt(
    *,
    max_chars: int = 2000,
    category_filter: str = "",
    tag_filter: str = "",
) -> str:
    """
    構建技能索引摘要，注入 system prompt。
    採用 Hermes 的 progressive disclosure 策略：
    - Tier 1: 只列名稱 + 描述（在 system prompt 裡）
    - Tier 2: 需要時才載入完整內容（via get_skill）
    """
    skills = list_skills(category=category_filter)
    if tag_filter:
        skills = [s for s in skills if tag_filter in (s.get("tags") or [])]

    if not skills:
        return ""

    lines = ["[可用技能庫]"]
    chars_used = len(lines[0])

    by_cat: Dict[str, List] = {}
    for s in skills:
        cat = s.get("category", "system")
        by_cat.setdefault(cat, []).append(s)

    for cat, cat_skills in sorted(by_cat.items()):
        cat_desc = LEGAL_CATEGORIES.get(cat, cat)
        header = f"\n## {cat} — {cat_desc}"
        if chars_used + len(header) > max_chars:
            break
        lines.append(header)
        chars_used += len(header)

        for s in cat_skills:
            entry = f"- **{s['name']}**: {s['description']}"
            if chars_used + len(entry) > max_chars:
                lines.append(f"  ...({len(cat_skills) - cat_skills.index(s)} more)")
                break
            lines.append(entry)
            chars_used += len(entry)

    lines.append("\n[使用 get_skill(category, name) 載入完整技能內容]")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# 技能搜尋（語義匹配）
# ══════════════════════════════════════════════════════════════════

def search_skills(
    query: str,
    *,
    top_k: int = 3,
    category: str = "",
) -> List[Dict[str, Any]]:
    """
    搜尋與查詢最相關的技能。
    使用簡單的 TF-IDF + trigram 相似度（不依賴外部向量模型）。
    未來可整合 FAISS 向量搜尋。
    """
    skills = list_skills(category=category)
    if not skills or not query:
        return []

    query_lower = query.lower()
    query_trigrams = _trigrams(query_lower)

    scored = []
    for s in skills:
        # 組合搜尋文本
        search_text = f"{s['name']} {s['description']} {' '.join(s.get('tags', []))}".lower()
        text_trigrams = _trigrams(search_text)

        # Trigram Jaccard similarity
        if not query_trigrams or not text_trigrams:
            score = 0.0
        else:
            intersection = query_trigrams & text_trigrams
            union = query_trigrams | text_trigrams
            score = len(intersection) / len(union) if union else 0.0

        # Keyword boost
        for word in query_lower.split():
            if len(word) >= 2 and word in search_text:
                score += 0.15

        if score > 0.05:
            scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:top_k]]


def _trigrams(text: str) -> set:
    """Generate character trigrams"""
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i+3] for i in range(len(text) - 2)}


# ══════════════════════════════════════════════════════════════════
# 背景技能審查（Hermes-style learning loop）
# ══════════════════════════════════════════════════════════════════

def review_and_learn(
    task_record: str,
    *,
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    審查任務紀錄，判斷是否值得沉澱為技能。
    這是 Hermes Agent 「background review」機制的 MAGI 版本。

    Args:
        task_record: 任務執行紀錄（對話摘要、步驟、結果）
        quiet: 靜默模式

    Returns:
        {"learned": bool, "skill_name": str, "action": str}
    """
    import urllib.request

    if not task_record or len(task_record) < 50:
        return {"learned": False, "reason": "task_record too short"}

    categories_str = "\n".join(f"- {k}: {v}" for k, v in LEGAL_CATEGORIES.items())
    prompt = SKILL_REVIEW_PROMPT.format(
        categories=categories_str,
        task_record=task_record[:6000],
    )

    # 呼叫本地 LLM
    try:
        omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080/v1/chat/completions")
        payload = json.dumps({
            "model": os.environ.get("MAGI_OMLX_MODEL", "gemma-4-26b-a4b-it-4bit"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500,
            "temperature": 0.4,
        }).encode("utf-8")
        req = urllib.request.Request(omlx_url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        response = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.warning("Skill review LLM call failed: %s", e)
        return {"learned": False, "reason": f"llm_error: {e}"}

    # 降級檢測
    if any(m in response for m in ("系統降級", "逾時", "請稍後重試")):
        return {"learned": False, "reason": "degraded_response"}

    # 判斷：不需要沉澱
    if "不需要沉澱" in response or "不值得" in response or len(response) < 100:
        _log_review(task_record[:200], "skip", response[:200])
        return {"learned": False, "reason": "not_worth_saving"}

    # 解析技能內容 — 支援多種 LLM 回覆格式
    meta, body = _parse_frontmatter(response)
    if not meta.get("name") or not body:
        # 格式 A: ```yaml\n...\n``` + markdown body
        code_match = re.search(r"```(?:yaml|markdown)?\s*\n(---\n.*?\n---)\s*\n(.*?)```",
                                response, re.DOTALL)
        if code_match:
            fm_text = code_match.group(1)
            body = code_match.group(2).strip()
            meta, _ = _parse_frontmatter(fm_text + "\n\n" + body)

        # 格式 B: ```yaml\nname: ...\n``` (無 --- 分隔) + 後面是 markdown
        if not meta.get("name"):
            code_match = re.search(r"```yaml\s*\n(.*?)```\s*\n(.*)", response, re.DOTALL)
            if code_match:
                yaml_block = code_match.group(1).strip()
                body = code_match.group(2).strip()
                # 包裝成 frontmatter 格式再解析
                wrapped = f"---\n{yaml_block}\n---\n\n{body}"
                meta, body = _parse_frontmatter(wrapped)

        # 修正 category: LLM 有時會輸出 "- workflow: ..." 格式
        if meta.get("category"):
            cat = str(meta["category"]).strip().lstrip("-").strip()
            # 取第一個匹配的分類
            for key in LEGAL_CATEGORIES:
                if key in cat:
                    meta["category"] = key
                    break
            else:
                meta["category"] = "system"

    if not meta.get("name"):
        _log_review(task_record[:200], "parse_failed", response[:500])
        return {"learned": False, "reason": "failed_to_parse_skill"}

    name = meta["name"]
    category = meta.get("category", "system")
    if category not in LEGAL_CATEGORIES:
        category = "system"

    # 檢查是否已存在同名技能
    existing = get_skill(category, name)
    if existing:
        # 更新已有技能
        full_content = _build_frontmatter(meta) + "\n\n" + body
        result = save_skill(category, name, full_content, is_update=True)
        action = "updated"
    else:
        full_content = _build_frontmatter(meta) + "\n\n" + body
        result = save_skill(category, name, full_content, is_update=False)
        action = "created"

    if result.get("success"):
        _log_review(task_record[:200], action, name)
        if not quiet:
            logger.info("Skill %s: %s/%s", action, category, name)
        return {"learned": True, "skill_name": name, "category": category,
                "action": action, "path": result.get("path")}
    else:
        return {"learned": False, "reason": result.get("error", "save_failed")}


def _log_review(task_summary: str, action: str, detail: str):
    """記錄技能審查結果到 JSONL"""
    try:
        SKILL_REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "action": action,
            "detail": detail[:500],
            "task": task_summary[:200],
        }
        with open(SKILL_REVIEW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# Night Talk 整合
# ══════════════════════════════════════════════════════════════════

def night_review_skills(
    council_minutes: str = "",
    *,
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    夜議結束後的技能審查 — 從當日議事錄中提取可沉澱的知識。

    整合點：在 night_talk.py 的 kaizen 階段後呼叫。
    """
    if not council_minutes:
        # 嘗試讀取最新議事錄
        minutes_path = _MAGI_ROOT / "nightly_council_minutes.md"
        if minutes_path.exists():
            council_minutes = minutes_path.read_text(encoding="utf-8", errors="ignore")

    if not council_minutes or len(council_minutes) < 100:
        return {"reviewed": False, "reason": "no_minutes"}

    # 取最後一次會議紀錄（用日期分隔）
    sections = re.split(r"\n#{1,2}\s+\d{4}-\d{2}-\d{2}", council_minutes)
    latest = sections[-1] if sections else council_minutes
    latest = latest[-4000:]  # 限制長度

    result = review_and_learn(
        f"[夜議紀錄摘要]\n{latest}",
        quiet=quiet,
    )

    return {
        "reviewed": True,
        "learned": result.get("learned", False),
        "skill_name": result.get("skill_name", ""),
        "action": result.get("action", ""),
    }


# ══════════════════════════════════════════════════════════════════
# 統計與報告
# ══════════════════════════════════════════════════════════════════

def get_skill_stats() -> Dict[str, Any]:
    """技能庫統計"""
    skills = list_skills()
    by_cat = {}
    for s in skills:
        cat = s.get("category", "system")
        by_cat.setdefault(cat, []).append(s)

    return {
        "total_skills": len(skills),
        "by_category": {cat: len(ss) for cat, ss in by_cat.items()},
        "categories_available": list(LEGAL_CATEGORIES.keys()),
        "skills_dir": str(SKILLS_DIR),
    }


# ══════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="MAGI Skill Learner")
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="列出所有技能")
    p_list.add_argument("--category", default="", help="篩選分類")

    # search
    p_search = sub.add_parser("search", help="搜尋技能")
    p_search.add_argument("query", help="搜尋關鍵字")
    p_search.add_argument("--top-k", type=int, default=3)

    # review
    p_review = sub.add_parser("review", help="從夜議紀錄中學習技能")

    # stats
    p_stats = sub.add_parser("stats", help="技能庫統計")

    # prompt
    p_prompt = sub.add_parser("prompt", help="生成技能索引 prompt")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "list":
        skills = list_skills(category=args.category)
        if not skills:
            print("(no skills yet)")
            return
        for s in skills:
            print(f"  [{s['category']}] {s['name']}: {s['description']}")
        print(f"\n  Total: {len(skills)} skills")

    elif args.command == "search":
        results = search_skills(args.query, top_k=args.top_k)
        if not results:
            print(f"No skills matching '{args.query}'")
            return
        for s in results:
            print(f"  [{s['category']}] {s['name']}: {s['description']}")

    elif args.command == "review":
        result = night_review_skills()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.command == "stats":
        stats = get_skill_stats()
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    elif args.command == "prompt":
        prompt = build_skills_prompt()
        print(prompt or "(no skills to show)")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
