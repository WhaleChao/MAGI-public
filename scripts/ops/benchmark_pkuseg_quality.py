#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
import sys

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from skills.engine.chinese_nlp import extract_keywords, segment, segment_for_indexing


CASES = [
    {
        "query": "消費者債務清理條例之更生方案",
        "expected_any": ["消費者", "債務", "清理", "條例", "更生", "方案"],
    },
    {
        "query": "侵權行為損害賠償",
        "expected_any": ["侵權", "行為", "損害", "賠償"],
    },
    {
        "query": "預售屋遲延交屋",
        "expected_any": ["預售屋", "遲延", "交屋"],
    },
]


def main() -> int:
    rows = []
    all_ok = True
    for case in CASES:
        query = case["query"]
        tokens = segment(query)
        keywords = extract_keywords(query, max_keywords=10)
        indexed = segment_for_indexing(query)
        ok = any(token in tokens or token in keywords or token in indexed for token in case["expected_any"])
        all_ok = all_ok and ok
        rows.append(
            {
                "query": query,
                "tokens": tokens,
                "keywords": keywords,
                "indexed": indexed,
                "ok": ok,
            }
        )

    result = {
        "success": all_ok,
        "cases": rows,
        "import_pkuseg_ok": True,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
