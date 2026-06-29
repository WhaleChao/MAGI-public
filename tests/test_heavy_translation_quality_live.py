from __future__ import annotations

from pypdf import PdfReader

from scripts.ops import heavy_translation_quality_live as gate


def test_generated_heavy_translation_fixture_is_extractable(tmp_path):
    path = gate.write_generated_fixture(tmp_path / "fixture.pdf")

    reader = PdfReader(str(path))
    texts = [(page.extract_text() or "") for page in reader.pages]

    assert len(reader.pages) == 100
    assert "司法通譯語言風格如何影響國民法官對被告的印象" in texts[0]
    assert "國民法官法" in "".join(texts[3:5])
    assert "court interpreters" in "".join(texts[5:7])
