import importlib.util
import sys
from pathlib import Path


_MODULE_PATH = Path("/Users/ai/Desktop/MAGI_v2/scripts/supreme_interpreter_pdf_backfill.py")
_SPEC = importlib.util.spec_from_file_location("supreme_interpreter_pdf_backfill", _MODULE_PATH)
pdf_backfill = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = pdf_backfill
_SPEC.loader.exec_module(pdf_backfill)


def test_jid_from_url_decodes_judicial_id():
    url = "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=TPSM%2c115%2c%e5%8f%b0%e4%b8%8a%2c566%2c20260506%2c1&ot=in"
    assert pdf_backfill.jid_from_url(url) == "TPSM,115,台上,566,20260506,1"


def test_wrap_text_line_keeps_nonempty_segments_under_budget():
    parts = pdf_backfill.wrap_text_line("最高法院刑事判決通譯品質爭議" * 8, max_units=20)
    assert len(parts) > 1
    assert all(pdf_backfill.text_width_units(part) <= 20 for part in parts)


def test_render_text_pdf_creates_pdf(tmp_path):
    out = tmp_path / "sample.pdf"
    pdf_backfill.render_text_pdf("裁判字號：最高法院測試\n\n主文\n抗告駁回。", out, title="測試")
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF")
    assert out.stat().st_size > 500
