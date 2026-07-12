from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api.saas_schema import inspect_tenant_schema


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[1]).resolve()
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
COMMON_WEAK_VALUES = {
    "",
    "changeme",
    "change-me",
    "default",
    "demo",
    "example",
    "password",
    "secret",
    "test",
    "test-key",
    "test-secret",
    "test_flask_secret",
}
CRITICAL_RUNTIME_FILES = (
    "api/app_factory.py",
    "api/request_guards.py",
    "api/server.py",
    "api/blueprints/admin_runtime.py",
    "api/blueprints/osc_cases.py",
    "api/blueprints/osc_files.py",
    "api/osc/saas_workbench.py",
    "static/osc/tabs/file_manager.js",
    "static/osc/tabs/saas.js",
    "templates/dashboard.html",
    "templates/dashboard_nerv.html",
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return _truthy(os.environ.get(name))


def deployment_mode() -> str:
    raw = str(os.environ.get("MAGI_DEPLOYMENT_MODE") or "").strip().lower()
    if _env_truthy("MAGI_SAAS_MODE") or raw in {"saas", "formal_saas", "managed_saas", "multi_tenant_saas"}:
        return "formal_saas"
    return raw or "single_host"


def formal_saas_enabled() -> bool:
    return deployment_mode() == "formal_saas"


def _public_source_root(root: Path) -> Path | None:
    for env_name in ("MAGI_PUBLIC_SOURCE_ROOT_DIR", "MAGI_SOURCE_ROOT_DIR"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw).expanduser().resolve()
    for candidate in (
        root,
        Path.home() / "Desktop" / "MAGI_v2",
        Path.home() / "Library" / "Application Support" / "MAGI" / "source" / "MAGI_v2",
    ):
        try:
            if (candidate / ".git").exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(
    key: str,
    title: str,
    ok: bool,
    *,
    status: str | None = None,
    required_for_saas: bool = True,
    detail: str = "",
    evidence: dict[str, Any] | None = None,
    action: str = "",
) -> dict[str, Any]:
    if status is None:
        status = "pass" if ok else ("fail" if required_for_saas and formal_saas_enabled() else "warn")
    return {
        "key": key,
        "title": title,
        "ok": bool(ok),
        "status": status,
        "required_for_saas": bool(required_for_saas),
        "detail": detail,
        "evidence": evidence or {},
        "action": action,
    }


def _value_strong(value: str, *, min_len: int = 32) -> bool:
    raw = str(value or "").strip()
    if len(raw) < min_len:
        return False
    return raw.lower() not in COMMON_WEAK_VALUES


def _url_is_https(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except Exception:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _permission_ok(path: Path) -> bool:
    mode = _file_mode(path)
    if mode is None:
        return True
    return mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def _secret_file_checks(root: Path) -> dict[str, Any]:
    files = [
        root / ".env",
        root / "json" / "credentials.json",
        root / "json" / "google_calendar_token.json",
    ]
    items = []
    for path in files:
        mode = _file_mode(path)
        items.append({
            "path": str(path.relative_to(root)) if path.exists() else str(path.name),
            "exists": path.exists(),
            "mode": "" if mode is None else oct(mode),
            "ok": _permission_ok(path),
        })
    return {"items": items, "ok": all(item["ok"] for item in items)}


def _runtime_consistency(root: Path, source_root: Path | None) -> dict[str, Any]:
    if source_root is None or not source_root.exists():
        return {"ok": False, "reason": "source_root_missing", "source_root": ""}
    if source_root == root:
        return {"ok": True, "source_root": str(source_root), "mismatches": []}
    mismatches = []
    missing = []
    for rel in CRITICAL_RUNTIME_FILES:
        left = source_root / rel
        right = root / rel
        if not left.exists() or not right.exists():
            missing.append(rel)
            continue
        try:
            if _sha256(left) != _sha256(right):
                mismatches.append(rel)
        except OSError:
            missing.append(rel)
    return {
        "ok": not mismatches and not missing,
        "source_root": str(source_root),
        "mismatches": mismatches,
        "missing": missing,
    }


def build_saas_readiness(
    *,
    root: Path | str | None = None,
    db_config: dict[str, Any] | None = None,
    app_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root_path = Path(root or ROOT).expanduser().resolve()
    db_config = db_config or {}
    app_config = app_config or {}
    mode = deployment_mode()
    formal = mode == "formal_saas"

    tenant_id = str(os.environ.get("MAGI_TENANT_ID") or "").strip()
    tenant_name = str(os.environ.get("MAGI_TENANT_NAME") or "").strip()
    public_base = str(
        os.environ.get("MAGI_PUBLIC_BASE_URL")
        or os.environ.get("PUBLIC_BASE_URL")
        or os.environ.get("BASE_URL")
        or ""
    ).strip()
    share_base = str(os.environ.get("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL") or "").strip()
    source_root = _public_source_root(root_path)
    secret_files = _secret_file_checks(root_path)
    runtime_consistency = _runtime_consistency(root_path, source_root)
    tenant_migration = root_path / "migrations" / "versions" / "003_add_tenant_scope.sql"
    audit_path = Path(os.environ.get("MAGI_SAAS_AUDIT_PATH") or root_path / ".runtime" / "saas_audit_events.jsonl")
    audit_dir = audit_path.parent
    tenant_schema = inspect_tenant_schema(tenant_id=tenant_id or None, auth_config=db_config)

    checks = [
        _check(
            "deployment_mode",
            "正式 SaaS 模式",
            formal,
            required_for_saas=False,
            status="pass" if formal else "warn",
            detail="MAGI_SAAS_MODE=1 或 MAGI_DEPLOYMENT_MODE=formal_saas 時啟用正式 SaaS gate。",
            evidence={"mode": mode},
            action="正式上線環境必須設定 MAGI_SAAS_MODE=1。",
        ),
        _check(
            "tenant_identity",
            "租戶識別",
            bool(TENANT_ID_RE.fullmatch(tenant_id)) and bool(tenant_name),
            detail="正式 SaaS 必須明確標示租戶，避免跨事務所資料與設定混用。",
            evidence={"tenant_id_configured": bool(tenant_id), "tenant_name_configured": bool(tenant_name)},
            action="設定 MAGI_TENANT_ID 與 MAGI_TENANT_NAME；tenant_id 僅用小寫英數、底線或連字號。",
        ),
        _check(
            "https_public_base",
            "HTTPS 公開入口",
            _url_is_https(public_base) and "trycloudflare.com" not in public_base.lower(),
            detail="正式 SaaS 必須使用穩定 HTTPS 網域，不可依賴臨時 tunnel 網址。",
            evidence={"public_base_configured": bool(public_base), "force_https": _env_truthy("MAGI_FORCE_HTTPS")},
            action="設定 MAGI_PUBLIC_BASE_URL=https://... 並開啟 MAGI_FORCE_HTTPS=1。",
        ),
        _check(
            "secure_cookies",
            "安全 Cookie",
            _env_truthy("MAGI_FORCE_HTTPS") or bool(app_config.get("SESSION_COOKIE_SECURE")),
            detail="正式 SaaS 登入 session cookie 必須只透過 HTTPS 傳送。",
            evidence={"session_cookie_secure": bool(app_config.get("SESSION_COOKIE_SECURE"))},
            action="設定 MAGI_FORCE_HTTPS=1，並確認反向代理提供 HTTPS。",
        ),
        _check(
            "strong_flask_secret",
            "Flask secret 強度",
            _value_strong(os.environ.get("FLASK_SECRET_KEY", ""), min_len=32),
            detail="正式 SaaS 不能使用測試或短 secret。",
            evidence={"configured": bool(os.environ.get("FLASK_SECRET_KEY")), "min_length": 32},
            action="使用至少 32 字元隨機值更新 FLASK_SECRET_KEY。",
        ),
        _check(
            "strong_api_key",
            "API key 強度",
            _value_strong(os.environ.get("MAGI_API_KEY", ""), min_len=32),
            detail="工具 API 與內部呼叫必須使用強隨機 API key。",
            evidence={"configured": bool(os.environ.get("MAGI_API_KEY")), "min_length": 32},
            action="使用至少 32 字元隨機值更新 MAGI_API_KEY。",
        ),
        _check(
            "registration_closed",
            "公開註冊關閉",
            not _env_truthy("MAGI_ALLOW_PUBLIC_REGISTRATION"),
            detail="正式 SaaS 不允許公開自行註冊，帳號需由管理員建立或由受控 IdP 開通。",
            evidence={"public_registration_enabled": _env_truthy("MAGI_ALLOW_PUBLIC_REGISTRATION")},
            action="移除 MAGI_ALLOW_PUBLIC_REGISTRATION=1。",
        ),
        _check(
            "cloudflare_surface",
            "Cloudflare surface 限縮",
            not _env_truthy("MAGI_ALLOW_CLOUDFLARE_WEB_UI"),
            detail="正式 SaaS 不可用一個環境變數繞過 Cloudflare route allowlist。",
            evidence={"allow_cloudflare_web_ui": _env_truthy("MAGI_ALLOW_CLOUDFLARE_WEB_UI")},
            action="關閉 MAGI_ALLOW_CLOUDFLARE_WEB_UI，改走 request_guards 白名單。",
        ),
        _check(
            "db_config",
            "資料庫設定",
            bool(str(db_config.get("host") or "").strip())
            and bool(str(db_config.get("user") or "").strip())
            and _value_strong(str(db_config.get("password") or ""), min_len=12),
            detail="正式 SaaS 必須有獨立 DB 帳號與非弱密碼。",
            evidence={
                "host_configured": bool(str(db_config.get("host") or "").strip()),
                "user_configured": bool(str(db_config.get("user") or "").strip()),
                "password_configured": bool(str(db_config.get("password") or "").strip()),
            },
            action="設定正式 DB 帳號與強密碼，避免共用或空白密碼。",
        ),
        _check(
            "share_base",
            "分享入口",
            (not share_base) or (_url_is_https(share_base) and "trycloudflare.com" not in share_base.lower()),
            required_for_saas=False,
            detail="若啟用公開分享，必須使用獨立穩定 HTTPS 分享入口。",
            evidence={"share_base_configured": bool(share_base)},
            action="設定 MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL=https://share.example.com 或停用公開分享。",
        ),
        _check(
            "secret_file_permissions",
            "敏感檔權限",
            bool(secret_files.get("ok")),
            detail=".env、Google credentials/token 不應讓 group/others 讀取。",
            evidence=secret_files,
            action="將敏感檔 chmod 600。",
        ),
        _check(
            "runtime_source_consistency",
            "runtime/source 一致",
            bool(runtime_consistency.get("ok")),
            detail="正式 SaaS runtime 必須與候選 source critical files 一致，避免修到工作樹但服務跑舊碼。",
            evidence=runtime_consistency,
            action="同步 runtime 與 source，或設定 MAGI_PUBLIC_SOURCE_ROOT_DIR 指向候選 source。",
        ),
        _check(
            "tenant_schema_migration",
            "租戶 schema 版本化",
            tenant_migration.exists(),
            detail="正式 SaaS 必須把 tenants、memberships、tenant_id 欄位 migration 納入版本控制。",
            evidence={"migration": str(tenant_migration), "exists": tenant_migration.exists()},
            action="確認 migrations/versions/003_add_tenant_scope.sql 存在並在正式 DB 維護窗口套用。",
        ),
        _check(
            "tenant_schema_applied",
            "租戶 schema 已套用",
            bool(tenant_schema.get("ok")),
            detail="正式 SaaS 必須在 auth DB 與 OSC 業務 DB 實際建立 tenants/support tables 與既有資料表 tenant 欄位。",
            evidence=tenant_schema,
            action="執行 scripts/ops/apply_tenant_scope.py，並確認 /saas-readyz 的 tenant_schema_applied 通過。",
        ),
        _check(
            "audit_sink",
            "稽核紀錄可寫",
            (audit_dir.exists() and os.access(audit_dir, os.W_OK)) or (root_path.exists() and os.access(root_path, os.W_OK)),
            detail="正式 SaaS 必須能寫入不含內容的操作稽核 JSONL。",
            evidence={"audit_path": str(audit_path), "parent_exists": audit_dir.exists()},
            action="確認 .runtime 可寫，或設定 MAGI_SAAS_AUDIT_PATH。",
        ),
    ]

    required = [item for item in checks if item.get("required_for_saas")]
    failed_required = [item for item in required if not item.get("ok")]
    warnings = [item for item in checks if item.get("status") == "warn"]
    ok = (not formal) or not failed_required
    return {
        "ok": ok,
        "status": "ready" if ok and formal else ("not_ready" if formal else "single_host"),
        "mode": mode,
        "mode_label": "正式 SaaS" if formal else "單主機 MAGI",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(root_path),
        "summary": {
            "total": len(checks),
            "required": len(required),
            "failed_required": len(failed_required),
            "warnings": len(warnings),
        },
        "checks": checks,
        "failed_keys": [item["key"] for item in failed_required],
        "warnings": [item["key"] for item in warnings],
    }
