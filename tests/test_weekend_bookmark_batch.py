import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "weekend_bookmark_batch.py"


class _FakeDoc:
    def __init__(self, toc=None, pages=20):
        self._toc = toc or []
        self.page_count = pages

    def get_toc(self):
        return list(self._toc)

    def close(self):
        return None


def _load_weekend_module():
    mod_name = "weekend_bookmark_batch_for_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_stage1_regex_no_boundary_marks_completed_without_error(tmp_path, monkeypatch):
    api_pkg = types.ModuleType("api")
    mapper_mod = types.ModuleType("api.case_path_mapper")
    mapper_mod.preferred_case_roots = lambda include_closed=False: []
    monkeypatch.setitem(sys.modules, "api", api_pkg)
    monkeypatch.setitem(sys.modules, "api.case_path_mapper", mapper_mod)

    fake_fitz = types.SimpleNamespace(open=lambda _path: _FakeDoc(toc=[], pages=17))
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

    mod = _load_weekend_module()
    monkeypatch.setattr(mod, "_save_state", lambda _state: None)

    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    state = {"completed": {}, "vision_done": {}}

    def _scan_fn(_path, output_path=None, dry_run=False):
        return {
            "success": False,
            "bookmarks": 0,
            "toc": [],
            "message": "未偵測到文件邊界，無法產生書籤",
        }

    stats = mod.stage1_regex([pdf], state, _scan_fn)

    key = str(pdf)
    assert stats["errors"] == 0
    assert stats["no_boundary"] == 1
    assert state["completed"][key]["stage1"] is True
    assert state["completed"][key]["stage1_bookmarks"] == 0
    assert state["completed"][key]["no_boundary"] is True


def test_stage1_regex_exception_still_counted_as_error(tmp_path, monkeypatch):
    api_pkg = types.ModuleType("api")
    mapper_mod = types.ModuleType("api.case_path_mapper")
    mapper_mod.preferred_case_roots = lambda include_closed=False: []
    monkeypatch.setitem(sys.modules, "api", api_pkg)
    monkeypatch.setitem(sys.modules, "api.case_path_mapper", mapper_mod)

    fake_fitz = types.SimpleNamespace(open=lambda _path: _FakeDoc(toc=[], pages=9))
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

    mod = _load_weekend_module()
    monkeypatch.setattr(mod, "_save_state", lambda _state: None)

    pdf = tmp_path / "boom.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    state = {"completed": {}, "vision_done": {}}

    def _scan_fn(_path, output_path=None, dry_run=False):
        raise RuntimeError("boom")

    stats = mod.stage1_regex([pdf], state, _scan_fn)

    assert stats["errors"] == 1
    assert stats["no_boundary"] == 0
    assert str(pdf) not in state["completed"]


def test_stage1_regex_success_stores_post_write_mtime(tmp_path, monkeypatch):
    api_pkg = types.ModuleType("api")
    mapper_mod = types.ModuleType("api.case_path_mapper")
    mapper_mod.preferred_case_roots = lambda include_closed=False: []
    monkeypatch.setitem(sys.modules, "api", api_pkg)
    monkeypatch.setitem(sys.modules, "api.case_path_mapper", mapper_mod)

    fake_fitz = types.SimpleNamespace(open=lambda _path: _FakeDoc(toc=[], pages=2))
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)

    mod = _load_weekend_module()
    monkeypatch.setattr(mod, "_save_state", lambda _state: None)

    pdf = tmp_path / "success.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    state = {"completed": {}, "vision_done": {}}

    def _scan_fn(path, output_path=None, dry_run=False):
        with open(path, "ab") as f:
            f.write(b"\n% updated")
        return {"success": True, "bookmarks": 2, "toc": [[1, "裁定", 1]]}

    first = mod.stage1_regex([pdf], state, _scan_fn)
    second = mod.stage1_regex([pdf], state, _scan_fn)

    assert first["processed"] == 1
    assert second["processed"] == 0
    assert second["skipped"] == 1
    assert state["completed"][str(pdf)]["mtime"] == str(pdf.stat().st_mtime)


def test_build_backfill_plan_reports_no_boundary_and_mtime_backlog(tmp_path, monkeypatch):
    api_pkg = types.ModuleType("api")
    mapper_mod = types.ModuleType("api.case_path_mapper")
    mapper_mod.preferred_case_roots = lambda include_closed=False: []
    monkeypatch.setitem(sys.modules, "api", api_pkg)
    monkeypatch.setitem(sys.modules, "api.case_path_mapper", mapper_mod)

    mod = _load_weekend_module()

    done_pdf = tmp_path / "done.pdf"
    done_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    backlog_pdf = tmp_path / "backlog.pdf"
    backlog_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    changed_pdf = tmp_path / "changed.pdf"
    changed_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    state = {
        "completed": {
            str(done_pdf): {
                "mtime": str(done_pdf.stat().st_mtime),
                "stage1": True,
                "stage1_bookmarks": 4,
                "pages": 12,
            },
            str(backlog_pdf): {
                "mtime": str(backlog_pdf.stat().st_mtime),
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": 20,
                "no_boundary": True,
            },
            str(changed_pdf): {
                "mtime": "0.0",
                "stage1": True,
                "stage1_bookmarks": 1,
                "pages": 9,
            },
        },
        "vision_done": {
            str(done_pdf): {"mtime": str(done_pdf.stat().st_mtime), "added": 0},
        },
    }

    plan = mod.build_backfill_plan([done_pdf, backlog_pdf, changed_pdf], state, sample_limit=10)

    assert plan["total_pdfs"] == 3
    assert plan["stage1_pending_count"] == 1
    assert str(changed_pdf) in plan["samples"]["stage1_pending"]
    assert plan["no_boundary_backlog_count"] == 1
    assert str(backlog_pdf) in plan["samples"]["no_boundary_backlog"]
    assert plan["vision_pending_count"] == 1
    assert str(backlog_pdf) in plan["samples"]["vision_pending"]
