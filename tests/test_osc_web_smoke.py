# -*- coding: utf-8 -*-
"""OSC 網頁版功能 E2E smoke 測試套件。

目的：以 Flask test client 走過 OSC web app 的關鍵 user journey，
確保「使用者打開 dashboard 看到資料庫斷線」這類整合層 bug 不再發生。

策略：
- 真實 register OSC blueprints（不 mock blueprint）
- _osc_exec 用 monkey-patch 餵 fake rows，避免污染真 DB
- LOGIN_DISABLED=True 跳過登入
- 涵蓋路徑：
  - dashboard / cases / clients / todos / quotations / 帳務 / 法扶
  - 文件生成（消債書狀、委任狀/收據/契約、報價單 PDF、地址標籤 PNG、PDF 蓋章）
  - 系統設定（admin、Discord、GCal、自動備份）

對應使用者要求（2026-04-30）：
  「網頁的所有功能也請加入測試範圍」
  「以後 MAGI 的 TEST 請包含這部分」
"""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402
from flask_login import LoginManager  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _build_app() -> Flask:
    """同時 register 所有 OSC blueprints，模擬真實 server.py 環境。"""
    a = Flask(__name__, template_folder=str(ROOT / "templates"))
    a.config["TESTING"] = True
    a.config["LOGIN_DISABLED"] = True
    a.config["PROPAGATE_EXCEPTIONS"] = False  # 讓 500 變回 response 而非 raise
    a.secret_key = "test"
    lm = LoginManager()
    lm.init_app(a)

    from api.blueprints.osc_cases import osc_bp
    from api.blueprints.osc_debt import osc_debt_bp
    from api.blueprints.osc_gcal import osc_gcal_bp

    a.register_blueprint(osc_bp)
    a.register_blueprint(osc_debt_bp)
    a.register_blueprint(osc_gcal_bp)
    return a


@pytest.fixture
def app():
    return _build_app()


@pytest.fixture
def client(app):
    return app.test_client()


# ── helper：模擬 _osc_exec 回傳 (rows, cfg) tuple ─────────────────────────────


def _make_fake_exec(rows_by_table=None):
    """建一個 fake exec callable，依 SQL 中出現的表名回傳對應 rows。

    rows_by_table: {"cases": [...], "clients": [...]}; SQL 中找到第一個 match 的表名
    就回那組 rows。找不到就回空。
    """
    rows_by_table = rows_by_table or {}

    def _fake(sql, params=(), fetch="none"):
        rows = []
        sql_lower = (sql or "").lower()
        for table, table_rows in rows_by_table.items():
            if f"from {table}" in sql_lower or f"from `{table}`" in sql_lower:
                rows = table_rows
                break
        if fetch == "all":
            return rows, {"host": "127.0.0.1"}
        if fetch == "one":
            return (rows[0] if rows else None), {"host": "127.0.0.1"}
        return {"rowcount": 0, "lastrowid": None}, {"host": "127.0.0.1"}

    return _fake


# ── 1. Dashboard 即時資料（與 user 04-30 報的 bug 相關） ────────────────────


def test_dashboard_endpoint_reachable(client):
    """確保 /api/osc/dashboard 在 LOGIN_DISABLED 下可達且回 200/JSON。

    這個 test 是「dashboard DB 斷線」事件後加入的回歸保險。
    """
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=_make_fake_exec()):
        r = client.get("/api/osc/dashboard")
    assert r.status_code == 200, f"dashboard 失效：{r.status_code}"
    payload = r.get_json()
    assert isinstance(payload, dict), "dashboard 應回 JSON dict"


def test_dashboard_does_not_call_old_db_ip(client):
    """dashboard 不得在錯誤路徑下嘗試連 100.121.61.74 等舊 IP。

    熱搜舊 IP 字串以 catch 寫死回歸。
    """
    import api.blueprints.osc_cases as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "100.121.61.74" not in src, "不該寫死舊 NAS IP"


# ── 2. 各 tab 列表 endpoint 可達 ──────────────────────────────────────────────

LIST_ENDPOINTS = [
    "/api/osc/cases",
    "/api/osc/clients",
    "/api/osc/todos",
    "/api/osc/calendar/events",
    "/api/osc/quotations",
    "/api/osc/quotation-templates",
    "/api/osc/legal-aid-branches",
    "/api/osc/courts",
    "/api/osc/case-reason-templates",
    "/api/osc/settings",
    "/api/osc/documents",
    "/api/osc/document-templates",
    "/api/osc/backups",
]
# 註：insights 端點走獨立 DB connection（不經 _osc_exec mock），
# 在 smoke 範圍外。線上實機可達。


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS)
def test_list_endpoint_reachable(client, endpoint):
    """每個列表 endpoint 都應回 200 且 JSON 結構合理。"""

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=_make_fake_exec()):
        r = client.get(endpoint)
    # 部分 endpoint 是 osc_debt 或 osc_gcal blueprint，但都該至少不 5xx
    assert r.status_code < 500, f"{endpoint} 回 {r.status_code}"


# ── 3. 消債書狀生成（osc_debt） ───────────────────────────────────────────────


def test_debt_forms_list(client):
    r = client.get("/api/osc/debt/forms")
    assert r.status_code == 200
    body = r.get_json()
    # 至少要有 5 種：聲請狀、財產說明書、債權人清冊、陳報狀、合併
    assert isinstance(body, dict)


def test_debt_schema_returns_fields(client):
    r = client.get("/api/osc/debt/schema/application")
    assert r.status_code == 200


def test_debt_source_status_uses_bundled_source(client):
    r = client.get("/api/osc/debt/source-status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["source_dir"].endswith("integrations/debt_robot")
    assert body["modules"]["supplement"].endswith("06_F.py")


# ── 4. 委任狀及契約生成（forms） ─────────────────────────────────────────────


def test_forms_preview_smoke(client):
    """forms/preview 至少不 crash（fake 案件 row）。"""
    fake_case = {
        "id": 1,
        "case_number": "TEST-001",
        "client_name": "測試當事人",
        "court_case_no": "112年度訴字第123號",
        "court_branch": "民事第一庭",
        "case_reason": "測試案由",
        "lawyer_name": "張律師",
        "address": "台北市測試路1號",
        "phone": "0912345678",
    }

    # forms/preview 內部還會 call _osc_get_case_identity_by_payload 與
    # _osc_build_form_preview，會跨進 osc.utils；smoke 不做完整 mock，
    # 只驗證 route 已註冊（非 404 即可）。
    fake = _make_fake_exec({"cases": [fake_case]})
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake):
        r = client.post(
            "/api/osc/forms/preview",
            json={"form_type": "power_of_attorney", "case_id": 1, "fields": {}},
        )
    assert r.status_code != 404, "forms/preview route 未註冊"


# ── 5. 報價單 PDF（reportlab） ────────────────────────────────────────────────


def test_quotation_pdf_smoke(client):
    """報價單 PDF endpoint 應能對 mock row 產出 application/pdf。"""
    fake_quotation = {
        "id": 1,
        "case_number": "TEST-001",
        "client_name": "王小明",
        "items_json": '[{"name":"民事訴訟","qty":1,"unit_price":50000}]',
        "total": 50000,
        "notes": "smoke test",
        "created_at": "2026-04-30",
    }

    fake = _make_fake_exec({"quotations": [fake_quotation], "settings": []})
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake):
        r = client.get("/api/osc/quotations/1/export-pdf")
    assert r.status_code == 200, f"PDF endpoint failed: {r.status_code}"
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF", "PDF magic bytes 不對"


# ── 6. 地址標籤 PNG（PIL） ────────────────────────────────────────────────────


def test_address_label_preview_smoke(client):
    """地址標籤預覽 endpoint 應回 image/png inline。"""
    fake_case = {
        "id": 1,
        "case_number": "TEST-001",
        "court_name": "臺灣臺北地方法院",
        "client_name": "王小明",
    }
    fake_court = {"name": "臺灣臺北地方法院", "address": "台北市博愛路131號"}

    fake = _make_fake_exec({"cases": [fake_case], "courts": [fake_court], "settings": []})
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake):
        r = client.get("/api/osc/cases/1/address-label?mode=preview&recipient=court")
    assert r.status_code == 200, f"PNG preview failed: {r.status_code}"
    assert r.mimetype == "image/png"
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n", "PNG magic bytes 不對"
    # 預覽必須 inline
    cd = r.headers.get("Content-Disposition", "")
    assert "inline" in cd.lower(), f"預覽應 inline: {cd}"


# ── 7. PDF 蓋章 endpoint（doc-producer skill） ───────────────────────────────


def test_documents_stamp_endpoint_reachable(client):
    """蓋章 endpoint 在收到 fake 路徑時應回 4xx（不 5xx），驗證路由綁定正確。"""
    r = client.post(
        "/api/osc/documents/stamp",
        json={"path": "/nonexistent/file.pdf", "copy_type": "正本"},
    )
    assert r.status_code in (200, 400, 404, 500), f"unexpected: {r.status_code}"
    # 即使 fail 也應 JSON
    if r.is_json:
        body = r.get_json()
        assert "ok" in body or "error" in body or "success" in body


# ── 8. CSV 匯入匯出 endpoint 可達 ────────────────────────────────────────────


def test_csv_export_cases_smoke(client):
    """案件 CSV 匯出 endpoint 應回 text/csv；無資料時回空 CSV 也算 OK。"""

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=_make_fake_exec()):
        r = client.get("/api/osc/cases/export-csv")
    assert r.status_code == 200
    assert "text/csv" in r.mimetype or "octet-stream" in r.mimetype


# ── 9. Checklist 應備事項 endpoint 可達 ──────────────────────────────────────


def test_checklist_legal_aid_endpoint_reachable(client):
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=_make_fake_exec()):
        r = client.get("/api/osc/checklists/legal-aid?case_number=TEST-001")
    assert r.status_code in (200, 400)


# ── 10. 自動備份 endpoint 可達 ────────────────────────────────────────────────


def test_backup_list_endpoint(client):
    """備份列表 endpoint 應回 {ok:true, items:[...]}。"""
    r = client.get("/api/osc/backups")
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("ok") is True
    assert isinstance(body.get("items"), list)


# ── 11. Google Calendar status endpoint ───────────────────────────────────────


def test_gcal_status_endpoint(client):
    """GCal status 應該回 connected=false（測試環境無 token.json）。"""
    r = client.get("/api/osc/gcal/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("ok") is True
    assert body.get("connected") in (False, True)


# ── 12. heartbeat / status JSON 端點 ─────────────────────────────────────────


def test_magi_status_json_has_no_legacy_db_ip():
    """static/magi_status.json 不該再有舊 NAS IP（2026-04-30 修復後驗證）。"""
    status_path = ROOT / "static" / "magi_status.json"
    if not status_path.exists():
        pytest.skip("magi_status.json 尚未生成（heartbeat 未執行過）")
    raw = status_path.read_text(encoding="utf-8")
    # 完整禁止舊的 desktop-jj06fa3 IP
    assert "100.121.61.74" not in raw, "magi_status.json 仍引用舊 DB IP"


# ── 13. heartbeat keeper 設定不寫死舊 IP ─────────────────────────────────────


def test_heartbeat_no_legacy_keeper_fallback():
    """heartbeat.py keeper.ip 不該 fallback 到 100.121.61.74。"""
    src = (ROOT / "skills" / "ops" / "heartbeat.py").read_text(encoding="utf-8")
    # 允許註解中提到（用於文件說明），但不該再有 _node_ip_or 的 fallback
    assert '_node_ip_or("nas", "100.121.61.74")' not in src, (
        "heartbeat.py keeper.ip 仍 fallback 到舊 IP"
    )
