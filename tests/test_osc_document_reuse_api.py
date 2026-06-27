from pathlib import Path
from unittest.mock import patch

from docx import Document
from flask import Flask
from flask_login import LoginManager, UserMixin


ROOT = Path(__file__).resolve().parent.parent


def _build_app() -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    login = LoginManager()
    login.init_app(app)

    class _User(UserMixin):
        id = "test-user"

    @login.user_loader
    def _load_user(_user_id):
        return _User()

    from api.blueprints.osc_cases import osc_bp

    app.register_blueprint(osc_bp)
    return app


def _make_source_docx(path: Path) -> None:
    doc = Document()
    header = doc.sections[0].header.paragraphs[0]
    header.add_run("臺灣")
    header.add_run("臺北")
    header.add_run("地方法院")
    p = doc.add_paragraph()
    p.add_run("案號：113年度")
    p.add_run("訴字第1號")
    p.add_run(" 股別：義股")
    doc.add_paragraph("內部案號：SRC-001")
    doc.add_paragraph("案由：損害賠償")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "原告"
    table.cell(0, 1).text = "王小明"
    table.cell(1, 0).text = "被告"
    table.cell(1, 1).text = "李大華"
    doc.sections[0].footer.paragraphs[0].text = "此致 臺灣臺北地方法院"
    doc.save(path)


def _doc_text(path: Path) -> str:
    doc = Document(path)
    parts = []

    def collect(container):
        parts.extend(p.text for p in container.paragraphs)
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    collect(cell)

    collect(doc)
    for section in doc.sections:
        collect(section.header)
        collect(section.footer)
    return "\n".join(parts)


def test_reuse_document_api_creates_target_case_docx_and_logs(tmp_path):
    app = _build_app()
    client = app.test_client()
    source_dir = tmp_path / "source"
    target_case_dir = tmp_path / "target-case"
    source_dir.mkdir()
    target_case_dir.mkdir()
    source = source_dir / "舊案民事準備書狀.docx"
    _make_source_docx(source)

    calls = []
    source_case = {
        "id": "1",
        "case_number": "SRC-001",
        "client_name": "王小明",
        "opponent_name": "李大華",
        "court_name": "臺灣臺北地方法院",
        "court_case_no": "113年度訴字第1號",
        "court_case_number": "113年度訴字第1號",
        "court_division": "義股",
        "case_reason": "損害賠償",
        "folder_path": str(source_dir),
    }
    target_case = {
        "id": "2",
        "case_number": "TGT-002",
        "client_name": "陳新明",
        "opponent_name": "林新華",
        "court_name": "臺灣新北地方法院",
        "court_case_no": "115年度重訴字第9號",
        "court_case_number": "115年度重訴字第9號",
        "court_division": "忠股",
        "case_reason": "返還借款",
        "folder_path": str(target_case_dir),
    }

    def fake_exec(sql, params=(), fetch="none"):
        calls.append((sql, params, fetch))
        lowered = " ".join(str(sql).lower().split())
        if "from cases" in lowered and fetch == "one":
            if params and str(params[0]) == "2":
                return target_case, {}
            if params and str(params[0]) == "SRC-001":
                return source_case, {}
        if "from opponents" in lowered and fetch == "all":
            return [], {}
        if fetch == "all":
            return [], {}
        if fetch == "one":
            return None, {}
        return {"rowcount": 1, "lastrowid": 99}, {}

    def resolve_existing(path, prefer_dir=None):
        p = Path(str(path))
        return str(p) if p.exists() else ""

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), patch(
        "api.blueprints.osc_cases._osc_resolve_existing_local_path", side_effect=resolve_existing
    ):
        response = client.post(
            "/api/osc/drafts/reuse-document",
            json={
                "case_id": "2",
                "case_number": "115年度重訴字第9號",
                "division": "忠股",
                "court_name": "臺灣新北地方法院",
                "reason": "返還借款",
                "plaintiff": "陳新明",
                "defendant": "林新華",
                "source_path": str(source),
                "source_case_number": "SRC-001",
                "suggested_filename": "新案準備書狀.docx",
            },
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload["ok"] is True
    result = payload["result"]
    output = Path(result["output_path"])
    assert output.exists()
    assert output.parent == target_case_dir / "04_我方歷次書狀"
    text = _doc_text(output)
    for expected in ("115年度重訴字第9號", "忠股", "陳新明", "林新華", "臺灣新北地方法院", "返還借款"):
        assert expected in text
    assert "113年度訴字第1號" not in text
    assert "王小明" not in text
    assert any("insert into case_documents" in " ".join(sql.lower().split()) for sql, _, _ in calls)
    assert any("insert into document_replacements" in " ".join(sql.lower().split()) for sql, _, _ in calls)


def test_draft_reuse_ui_is_wired_to_api():
    html = (ROOT / "templates" / "partials" / "osc" / "drafts.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "osc" / "tabs" / "drafts.js").read_text(encoding="utf-8")
    events = (ROOT / "static" / "osc" / "osc-events.js").read_text(encoding="utf-8")
    state = (ROOT / "static" / "osc" / "osc-state.js").read_text(encoding="utf-8")

    assert "全書狀索引 / 沿用舊書狀" in html
    assert 'id="draftReuseDocsBody"' in html
    assert "/api/osc/drafts/reuse-document" in js
    assert "draft-reuse-select" in events
    assert "draftReuseRunBtn" in events
    assert "selectedReuseDocument" in state
