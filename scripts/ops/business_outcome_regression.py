#!/usr/bin/env python3
"""Add a deidentified manual business finding to a test-only regression corpus."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.business_outcome_eval import write_manual_finding

DEFAULT = ROOT / "tests" / "v3" / "evals" / "business_outcome_regression_corpus.json"

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="local JSON finding; never sent externally")
    parser.add_argument("--corpus", default=str(DEFAULT))
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    _corpus, added = write_manual_finding(args.corpus, raw, test_root=ROOT / "tests")
    print(json.dumps({"ok": True, "added": added, "corpus": str(Path(args.corpus))}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
