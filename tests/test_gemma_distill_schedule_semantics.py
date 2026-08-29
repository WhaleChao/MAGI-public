from __future__ import annotations

import json
import os
import tempfile

# The production module binds its mutable logging workspace at import time.
# Keep this unit test entirely under a disposable directory.
os.environ.setdefault(
    "GEMMA_DISTILL_DIR", tempfile.mkdtemp(prefix="magi-gemma-contract-")
)

from scripts.nightly_distill_gemma import (  # noqa: E402
    _candidate_rejected_schedule_result,
)
from skills.ops.cron_result_policy import (
    classify_cron_result,
    terminal_schedule_deferral_reason,
)


def test_rejected_candidate_is_visible_terminal_deferral_not_success() -> None:
    payload = _candidate_rejected_schedule_result(
        version="gemma-distill-v012",
        validate_result={"validation_pass": False, "passed": 1, "total": 3},
    )
    rendered = json.dumps(payload, ensure_ascii=False)

    classified = classify_cron_result(75, rendered, "")

    assert classified.success is False
    assert classified.status == "deferred"
    assert classified.error == "candidate_rejected"
    assert terminal_schedule_deferral_reason(rendered) == "candidate_rejected"
    assert payload["review_required"] is True
    assert payload["deploy_allowed"] is False
