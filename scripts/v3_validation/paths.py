from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROUTES_PATH = REPO_ROOT / "docs" / "architecture" / "v3" / "generated" / "v2_runtime_routes.json"
CAPABILITY_MANIFEST_PATH = REPO_ROOT / "config" / "v3_capability_manifest.json"
API_ENVELOPE_SCHEMA_PATH = REPO_ROOT / "docs" / "architecture" / "v3" / "contracts" / "api-envelope.schema.json"
JOB_ENVELOPE_SCHEMA_PATH = REPO_ROOT / "docs" / "architecture" / "v3" / "contracts" / "job-envelope.schema.json"
SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
REPLAY_FIXTURE_SCHEMA_PATH = SCHEMAS_DIR / "replay-fixture.schema.json"
LIVE_PLAN_SCHEMA_PATH = SCHEMAS_DIR / "live-validation-plan.schema.json"
ISOLATED_LIVE_EXECUTION_PLAN_SCHEMA_PATH = (
    SCHEMAS_DIR / "isolated-live-execution-plan.schema.json"
)
LIVE_REPORT_SCHEMA_PATH = SCHEMAS_DIR / "live-validation-report.schema.json"
ROUTE_METHOD_REVIEW_PATH = Path(__file__).resolve().parent / "route-method-review.json"
