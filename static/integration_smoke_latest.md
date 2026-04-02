# MAGI Integration Smoke

- Generated: 2026-03-12T20:38:10.220887
- Overall OK: True
- Main API: http://127.0.0.1:5002
- Tools API: http://127.0.0.1:5003

## Summary
- [PASS] main_health: status=operational http=200
- [PASS] tools_sages: casper_online=True melchior_online=True http=200
- [PASS] module_provenance: all core modules import from MAGI
- [PASS] embed_service_health: status=healthy http=200
- [PASS] embed_roundtrip: dims=768 http=200
- [PASS] notification_config: telegram_targets=1
- [PASS] skill:laf-orchestrator:self_test: self_test
- [PASS] skill:file-review-orchestrator:self_test: self_test/db_smoke
- [PASS] skill:file-review-orchestrator:db_smoke {}: self_test/db_smoke
- [PASS] skill:transcript-downloader:self_test: self_test/db_probe
- [PASS] skill:transcript-downloader:db_probe: self_test/db_probe
- [PASS] skill:osc-orchestrator:self_test: self_test
- [PASS] skill:osc-scan-folder:self_test: self_test
- [PASS] skill:db-dual-sync:self_test: self_test
- [PASS] skill:laf-withdrawal-report:self_test: wrapper_self_test
- [PASS] skill:crawler-targets:self_test: self_test
- [PASS] skill:magi-autopilot:self_test: self_test
- [PASS] skill:statutes-vdb:help: help
- [PASS] skill:gmail-drafts:help: help
- [PASS] launch_agent: com.magi.casper present in launchctl list
