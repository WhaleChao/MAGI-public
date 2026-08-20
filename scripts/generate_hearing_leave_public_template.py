#!/usr/bin/env python3
"""Generate the de-identified public hearing-conflict leave-request template."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path = [str(ROOT), *(entry for entry in sys.path if entry != str(ROOT))]
for _name in [name for name in sys.modules if name == "api" or name.startswith("api.")]:
    sys.modules.pop(_name, None)
from api.osc.hearing_conflicts import build_leave_request_docx
from api.runtime_paths import get_hearing_leave_template_path


def template_payload() -> dict:
    return {
        "document_title": "民事聲請變更期日狀",
        "court_case_no": "【法院案號】",
        "division": "【股別】",
        "court_name": "【法院全銜及庭別】",
        "party_name": "【當事人姓名】",
        "party_role": "【當事人身分】",
        "lawyer_name": "【律師姓名】",
        "is_legal_aid": False,
        "lawyer_capacity": "【扶助／委任律師】",
        "case_reason": "【案由】",
        "target_start": datetime(2026, 1, 1, 9, 0),
        "target_display": "【新庭期日期及時間】",
        "target_hearing_label": "【新庭期程序】",
        "prior_start": datetime(2026, 1, 1, 9, 30),
        "prior_display": "【較早排定庭期之日期及時間】",
        "prior_hearing_label": "【既有庭期程序】",
        "prior_court_name": "【較早排定庭期之法院】",
        "prior_court_case_no": "【較早排定庭期之法院案號】",
        "generated_on": date(2026, 1, 1),
        "office_address": "【事務所地址】",
        "office_phone": "【事務所電話】",
        "generation_mode": "public_template",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="external output path (defaults to MAGI_HEARING_LEAVE_TEMPLATE_PATH/shared state)",
    )
    args = parser.parse_args()
    output = args.output.expanduser().resolve() if args.output else get_hearing_leave_template_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    build_leave_request_docx(template_payload(), output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
