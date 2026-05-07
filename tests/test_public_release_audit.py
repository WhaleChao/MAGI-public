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
