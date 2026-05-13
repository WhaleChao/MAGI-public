"""
OSC Settings Blueprint
======================
Handles /api/osc/settings, /api/osc/courts, /api/osc/legal-aid-branches routes.
Migrated from server.py to reduce monolith size.

Usage in server.py:
    from api.blueprints.osc_settings import osc_settings_bp
    app.register_blueprint(osc_settings_bp)
"""

from flask import Blueprint, request, jsonify
from flask_login import login_required

osc_settings_bp = Blueprint("osc_settings", __name__)


def _get_osc_helpers():
    """Lazy import OSC helpers from server.py to avoid circular imports."""
    from api.osc.utils import _osc_exec, _osc_text, _osc_log_activity
    return _osc_exec, _osc_text, _osc_log_activity


# ── Settings ──────────────────────────────────────────────────────────

@osc_settings_bp.route("/api/osc/settings", methods=["GET", "POST"])
@login_required
def osc_settings_api():
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT `key`, value, description, updated_date FROM settings WHERE 1=1 "
        params = []
        if q:
            like = f"%{q}%"
            sql += "AND (`key` LIKE %s OR value LIKE %s OR description LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY `key` ASC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    key = _osc_text(payload.get("key"))
    if not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO settings (`key`, value, description)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE value=VALUES(value), description=VALUES(description)
        """,
        (key, _osc_text(payload.get("value")), _osc_text(payload.get("description"))),
        fetch="none",
    )
    _osc_log_activity("setting:save", "settings", key, payload)
    return jsonify({"ok": True, "result": result, "key": key})


@osc_settings_bp.route("/api/osc/settings/<path:setting_key>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_setting_detail_api(setting_key):
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT `key`, value, description, updated_date FROM settings WHERE `key`=%s", (setting_key,), fetch="one")
        # 不存在的 key 回 200 + item:null（與 admin.js「沒這 key 是正常情況」對齊，
        # 避免瀏覽器 console 出現「Failed to load resource: 404」誤導律師）
        return jsonify({"ok": True, "item": row or None})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM settings WHERE `key`=%s", (setting_key,), fetch="none")
        _osc_log_activity("setting:delete", "settings", setting_key)
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["value", "description"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(setting_key)
    result, _ = _osc_exec(f"UPDATE settings SET {','.join(sets)} WHERE `key`=%s", tuple(vals), fetch="none")
    _osc_log_activity("setting:update", "settings", setting_key, payload)
    return jsonify({"ok": True, "result": result})


# ── Courts ────────────────────────────────────────────────────────────

@osc_settings_bp.route("/api/osc/courts", methods=["GET", "POST"])
@login_required
def osc_courts_api():
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        court_type = (request.args.get("type") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, name, address, type, last_updated FROM courts WHERE 1=1 "
        params = []
        if court_type:
            sql += "AND type=%s "
            params.append(court_type)
        if q:
            like = f"%{q}%"
            sql += "AND (name LIKE %s OR address LIKE %s OR type LIKE %s) "
            params.extend([like, like, like])
        sql += "ORDER BY name ASC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    name = _osc_text(payload.get("name"))
    address = _osc_text(payload.get("address"))
    if not name or not address:
        return jsonify({"ok": False, "error": "name/address required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO courts (name, address, type)
        VALUES (%s,%s,%s)
        ON DUPLICATE KEY UPDATE address=VALUES(address), type=VALUES(type)
        """,
        (name, address, _osc_text(payload.get("type"))),
        fetch="none",
    )
    _osc_log_activity("court:save", "courts", name, payload)
    return jsonify({"ok": True, "result": result})


@osc_settings_bp.route("/api/osc/courts/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_court_detail_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM courts WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM courts WHERE id=%s", (row_id,), fetch="none")
        _osc_log_activity("court:delete", "courts", str(row_id))
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["name", "address", "type"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE courts SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    _osc_log_activity("court:update", "courts", str(row_id), payload)
    return jsonify({"ok": True, "result": result})


# ── Legal Aid Branches ────────────────────────────────────────────────

@osc_settings_bp.route("/api/osc/legal-aid-branches", methods=["GET", "POST"])
@login_required
def osc_legal_aid_branches_api():
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        q = (request.args.get("q") or "").strip()
        limit = max(1, min(2000, int(request.args.get("limit") or "300")))
        sql = "SELECT id, name, address, last_updated FROM legal_aid_branches WHERE 1=1 "
        params = []
        if q:
            like = f"%{q}%"
            sql += "AND (name LIKE %s OR address LIKE %s) "
            params.extend([like, like])
        sql += "ORDER BY name ASC LIMIT %s"
        params.append(limit)
        rows, _ = _osc_exec(sql, tuple(params), fetch="all")
        return jsonify({"ok": True, "items": rows or []})
    payload = request.get_json() or {}
    name = _osc_text(payload.get("name"))
    address = _osc_text(payload.get("address"))
    if not name or not address:
        return jsonify({"ok": False, "error": "name/address required"}), 400
    result, _ = _osc_exec(
        """
        INSERT INTO legal_aid_branches (name, address)
        VALUES (%s,%s)
        ON DUPLICATE KEY UPDATE address=VALUES(address)
        """,
        (name, address),
        fetch="none",
    )
    _osc_log_activity("legal_aid_branch:save", "legal_aid_branches", name, payload)
    return jsonify({"ok": True, "result": result})


@osc_settings_bp.route("/api/osc/legal-aid-branches/<int:row_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def osc_legal_aid_branch_detail_api(row_id):
    _osc_exec, _osc_text, _osc_log_activity = _get_osc_helpers()
    if request.method == "GET":
        row, _ = _osc_exec("SELECT * FROM legal_aid_branches WHERE id=%s", (row_id,), fetch="one")
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "item": row})
    if request.method == "DELETE":
        result, _ = _osc_exec("DELETE FROM legal_aid_branches WHERE id=%s", (row_id,), fetch="none")
        _osc_log_activity("legal_aid_branch:delete", "legal_aid_branches", str(row_id))
        return jsonify({"ok": True, "result": result})
    payload = request.get_json() or {}
    sets, vals = [], []
    for key in ["name", "address"]:
        if key not in payload:
            continue
        sets.append(f"{key}=%s")
        vals.append(_osc_text(payload.get(key)))
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    vals.append(row_id)
    result, _ = _osc_exec(f"UPDATE legal_aid_branches SET {','.join(sets)} WHERE id=%s", tuple(vals), fetch="none")
    _osc_log_activity("legal_aid_branch:update", "legal_aid_branches", str(row_id), payload)
    return jsonify({"ok": True, "result": result})


# ── Discord Webhook test ──────────────────────────────────────────────
# 對應原版 PaperClip Discord 推播（osc.py SettingsDialog discord_group line 22790）。
# 網頁版過去無專屬 UI，現在 admin tab 有專屬 webhook 設定 + Test 推播按鈕。

@osc_settings_bp.route("/api/osc/discord/test", methods=["POST"])
@login_required
def osc_discord_test_api():
    """測試 Discord webhook：POST 一則「✅ MAGI Webhook 連線測試」訊息。

    payload: {"webhook_url": "https://discord.com/api/webhooks/...", "message?": "..."}
    若 webhook_url 為空、嘗試從 settings.discord_webhook_url 讀取。

    Returns: {ok, status_code, error?}
    """
    import json as _json
    import urllib.request as _ureq
    import urllib.error as _uerr

    payload = request.get_json(silent=True) or {}
    webhook_url = (payload.get("webhook_url") or "").strip()
    msg = (payload.get("message") or "").strip() or "✅ MAGI Webhook 連線測試（OSC admin → Test 推播）"

    if not webhook_url:
        # fallback：讀 settings.discord_webhook_url
        _osc_exec, _, _ = _get_osc_helpers()
        row, _ = _osc_exec(
            "SELECT value FROM settings WHERE `key`='discord_webhook_url'",
            (),
            fetch="one",
        )
        webhook_url = (row.get("value") if row else "") or ""
        webhook_url = webhook_url.strip()

    if not webhook_url:
        return jsonify({"ok": False, "error": "webhook_url required (and no settings.discord_webhook_url found)"}), 400

    if not webhook_url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        return jsonify({"ok": False, "error": "invalid Discord webhook URL"}), 400

    body = _json.dumps({"content": msg}).encode("utf-8")
    req = _ureq.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "MAGI-OSC/1.0"},
    )
    try:
        with _ureq.urlopen(req, timeout=10) as resp:
            return jsonify({"ok": True, "status_code": resp.status})
    except _uerr.HTTPError as e:
        return jsonify({"ok": False, "status_code": e.code, "error": f"HTTP {e.code}: {e.reason}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 502
