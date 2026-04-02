# MAGI Full Validation

- Overall OK: True
- Integration smoke: /Users/ai/Desktop/MAGI/static/integration_smoke_latest.json
- Three-channel smoke: /Users/ai/Desktop/MAGI/static/smoke_three_channels_latest.json
- Autopilot self test: /Users/ai/Desktop/MAGI/_autopilot_runs/20260312_164441_self_test/report.json

## Safe Skill Self Tests
- [PASS] translator self_test (degraded fallback preview returned)
- [PASS] judicial-web-search self_test
- [PASS] judicial-flow-search-archive self_test
- [PASS] laf-refine-case self_test
- [PASS] iron-dome self_test

## Mock Tests
- [PASS] file_review: 8 PASS / 0 FAIL / 0 SKIP
- [PASS] portal: 1 PASS / 0 FAIL / 0 SKIP
- [WARN] laf_workflows: downloadable_cases/go_live/condition/inquiry/withdrawal/fee passed; closing timed out at 160s
- [PASS] laf_closing_route_checks: closing list + summary + save endpoints responded correctly

## Known Limitation
- LAF direct browser closing draft on laf_mock still times out, but route-level closing endpoints are healthy.
