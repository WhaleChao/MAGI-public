from scripts.public_release_audit import scan_text, summarize


def test_public_release_audit_flags_real_secret_like_values():
    findings = scan_text("app.py", "API_TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'\n")

    assert summarize(findings)["ok"] is False
    assert findings[0].kind == "github_token"


def test_public_release_audit_allows_placeholder_examples():
    findings = scan_text(".env.example", "NVIDIA_NIM_API_KEY=<<REPLACE_WITH_YOUR_nvapi-KEY>>\n")

    assert summarize(findings)["ok"] is True


def test_public_release_audit_warns_on_tailnet_ip():
    findings = scan_text("README.md", "Use MAGI_REMOTE_DB_HOST=100.64.1.2 only in private deployments.\n")

    assert summarize(findings)["ok"] is True
    assert findings[0].severity == "warning"


def test_public_release_audit_allows_intentional_test_fixture_pii():
    findings = scan_text("tests/test_fixture.py", "assert row['phone'] == '0912345678'\n")

    assert summarize(findings)["ok"] is True
    assert findings == []


def test_public_release_audit_blocks_private_integration_markers():
    findings = scan_text(
        "skills/example/action.py",
        "provider = '" + "law" + "snote'\nmail='" + "whale" + "lawyer@example.com'\n",
        public_isolation=True,
    )

    assert {f.kind for f in findings} >= {"private_legal_source_marker", "private_mailbox_marker"}
    assert all(f.severity == "error" for f in findings)


def test_public_release_audit_allows_gitignore_private_source_rules():
    findings = scan_text(".gitignore", "skills/private-legal-source*/\n**/private_legal_source*\n", public_isolation=True)

    assert findings == []


def test_public_release_audit_blocks_tracked_runtime_paths():
    from scripts.public_release_audit import scan_tracked_files

    findings = scan_tracked_files(
        [
            "json/processed_laf_emails.json",
            "skills/pdf-namer/_filing_log.json",
            "skills/pdf-namer/db_rules_cache.json",
            "static/knowledge_lint_latest.json",
        ],
        public_isolation=True,
    )

    blocked = [f for f in findings if f.kind == "blocked_path"]
    assert {f.path for f in blocked} == {
        "json/processed_laf_emails.json",
        "skills/pdf-namer/_filing_log.json",
        "skills/pdf-namer/db_rules_cache.json",
        "static/knowledge_lint_latest.json",
    }
