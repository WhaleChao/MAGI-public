"""PII Scrubber for cloud LLM requests.

將律所資料送往雲端（NVIDIA NIM / OpenAI / Anthropic）前，先把高敏感欄位遮蔽。
遮蔽策略是**可逆**的：記住 mapping，收到回覆後可還原（例如 [當事人A] → 原始姓名）。

涵蓋：
- 台灣身分證字號（A123456789）
- 法扶案號（1150409-I-004）
- 法院案號（114年度原訴字第000024號）
- 台灣手機號（09XX-XXX-XXX / +8869XXXXXXXX）
- 客戶姓名（從 DB 既有 cases.client_name 動態載入）

不涵蓋（v1 限制）：
- 地址（太難用 regex 穩定抽，建議走 NER，排 v2）
- email（律所工作中 email 多為對外通訊，不屬 PII 核心）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("PIIScrubber")

# ────────────────────────────────────────────────────────────
# Regex rules
# ────────────────────────────────────────────────────────────
_TW_ID_RE = re.compile(r"[A-Z][12]\d{8}")
_LAF_CASE_RE = re.compile(r"\b\d{7}-[A-Z]-\d{3}\b")
_COURT_CASE_RE = re.compile(
    r"\d{2,3}年(?:度)?[\u4e00-\u9fff]{1,8}字第\d{3,6}號"
)
_TW_MOBILE_RE = re.compile(r"(?:\+886-?|0)9\d{2}[-\s]?\d{3}[-\s]?\d{3}")


@dataclass
class ScrubResult:
    """遮蔽結果。

    - scrubbed_text: 已遮蔽的文字（可送雲端）
    - mapping: 佔位符 → 原始值（本機保留，用於回覆還原）
    - counts: 各類型遮蔽次數
    """
    scrubbed_text: str
    mapping: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)

    def restore(self, text: str) -> str:
        """把 LLM 回覆中的佔位符還原為原始值。"""
        out = text or ""
        # 長佔位符優先還原（避免 [當事人A] 被 [當事人A10] 吃掉）
        for placeholder in sorted(self.mapping.keys(), key=len, reverse=True):
            out = out.replace(placeholder, self.mapping[placeholder])
        return out


class PIIScrubber:
    def __init__(self, *, known_names: Optional[List[str]] = None):
        """
        known_names: 來自 DB 的 cases.client_name 清單，會做 exact-match 遮蔽。
                     不給的話只跑 regex 層。
        """
        self.known_names = [n for n in (known_names or []) if n and len(n.strip()) >= 2]
        # 長名字優先（避免「王小明」被「王小」吃掉）
        self.known_names.sort(key=len, reverse=True)

    def scrub(self, text: str) -> ScrubResult:
        if not text:
            return ScrubResult(scrubbed_text="")

        mapping: Dict[str, str] = {}
        counts: Dict[str, int] = {"id": 0, "laf_case": 0, "court_case": 0, "mobile": 0, "name": 0}
        out = str(text)

        # 1) 台灣身分證
        out = self._scrub_regex(out, _TW_ID_RE, "ID", mapping, counts, "id")
        # 2) 法扶案號
        out = self._scrub_regex(out, _LAF_CASE_RE, "LAF", mapping, counts, "laf_case")
        # 3) 法院案號
        out = self._scrub_regex(out, _COURT_CASE_RE, "CASE", mapping, counts, "court_case")
        # 4) 手機
        out = self._scrub_regex(out, _TW_MOBILE_RE, "TEL", mapping, counts, "mobile")
        # 5) 客戶姓名（DB 已知）
        for name in self.known_names:
            if name in out:
                placeholder = self._next_placeholder("當事人", mapping)
                out = out.replace(name, placeholder)
                mapping[placeholder] = name
                counts["name"] += 1

        result = ScrubResult(scrubbed_text=out, mapping=mapping, counts=counts)
        if any(counts.values()):
            logger.info(
                "PII scrubbed: id=%d laf=%d case=%d tel=%d name=%d",
                counts["id"], counts["laf_case"], counts["court_case"],
                counts["mobile"], counts["name"],
            )
        return result

    @staticmethod
    def _scrub_regex(
        text: str,
        pattern: re.Pattern,
        prefix: str,
        mapping: Dict[str, str],
        counts: Dict[str, int],
        count_key: str,
    ) -> str:
        def _sub(match):
            raw = match.group(0)
            # 同一值 reuse 同一佔位符
            for ph, orig in mapping.items():
                if orig == raw and ph.startswith(f"[{prefix}-"):
                    return ph
            ph = f"[{prefix}-{len([k for k in mapping if k.startswith(f'[{prefix}-')]) + 1:03d}]"
            mapping[ph] = raw
            counts[count_key] += 1
            return ph
        return pattern.sub(_sub, text)

    @staticmethod
    def _next_placeholder(prefix: str, mapping: Dict[str, str]) -> str:
        """產生不重複的中文佔位符，如 [當事人A]、[當事人B]…"""
        used = {k for k in mapping if k.startswith(f"[{prefix}")}
        for i in range(26):
            ch = chr(ord("A") + i)
            ph = f"[{prefix}{ch}]"
            if ph not in used:
                return ph
        # A-Z 用完，補數字
        return f"[{prefix}{len(used) + 1}]"


# ────────────────────────────────────────────────────────────
# Factory: 從 MAGI DB 載入已知當事人清單
# ────────────────────────────────────────────────────────────
def build_scrubber_from_magi_db(limit: int = 1000) -> PIIScrubber:
    """從 cases 表撈 client_name 建立 scrubber。失敗時退回純 regex 模式。

    - `cases` 表在 `law_firm_data` DB（不是預設的 `magi_brain`）
    - 用 `api.db_helper.get_cursor(config={...})` 可覆寫 database 名稱
    - 可透過 env `MAGI_CASES_DB_NAME` 自訂 DB 名稱
    """
    import os
    try:
        from api.db_helper import _default_config, get_cursor
        cfg = _default_config()
        cfg["database"] = os.environ.get("MAGI_CASES_DB_NAME", "law_firm_data")
        names: list[str] = []
        with get_cursor(config=cfg, dictionary=True) as (_conn, cur):
            cur.execute(
                "SELECT DISTINCT client_name FROM cases "
                "WHERE client_name IS NOT NULL AND TRIM(client_name) != '' "
                "ORDER BY updated_at DESC LIMIT %s",
                (int(limit),),
            )
            for row in cur.fetchall() or []:
                n = str((row or {}).get("client_name") or "").strip()
                if n and len(n) >= 2:
                    names.append(n)
        logger.info("PII Scrubber loaded %d known names from DB", len(names))
        return PIIScrubber(known_names=names)
    except Exception as e:
        logger.warning("PII Scrubber DB load failed, falling back to regex-only: %s", e)
        return PIIScrubber()
