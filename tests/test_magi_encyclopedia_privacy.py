from __future__ import annotations

import ast
from pathlib import Path

from scripts.docs.build_magi_encyclopedia import (
    CHAPTERS,
    SOURCE_COMMIT,
    first_sentence,
    redact_workstation_paths,
    render_pdf,
    signature_for,
    source_url,
)


def test_pdf_builder_uses_reportlab_without_office_or_browser_processes():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/docs/build_magi_encyclopedia.py"
    ).read_text(encoding="utf-8")

    assert '"renderer": "reportlab"' in source
    assert "reportlab.platypus" in source
    assert "Contents/MacOS/soffice" not in source
    assert "--convert-to" not in source
    assert "--print-to-pdf" not in source


def test_pdf_builder_recurses_through_pandoc_section_containers(tmp_path):
    markdown = tmp_path / "nested.md"
    output = tmp_path / "nested.pdf"
    markdown.write_text(
        f"# {CHAPTERS[0][1]}\n\n"
        "## Nested section\n\n"
        "REPORTLAB_SECTION_SENTINEL\n",
        encoding="utf-8",
    )

    result = render_pdf(markdown, output, None)

    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(output)).pages)
    assert result["renderer"] == "reportlab"
    assert result["heading_pages"] == {CHAPTERS[0][0]: 1}
    assert "REPORTLAB_SECTION_SENTINEL" in text


def test_source_index_signature_redacts_posix_workstation_default():
    tree = ast.parse(
        "def probe(path: str = '/Volumes/private-share/input.json'): pass\n"
    )

    rendered = signature_for(tree.body[0])

    assert rendered == "probe(path: str='<workstation-path>')"
    assert "/Volumes/" not in rendered


def test_source_index_metadata_redacts_user_and_windows_paths():
    value = "Reads /Users/private/secret.txt and C:\\Users\\private\\secret.txt."

    rendered = first_sentence(value)

    assert rendered.count("<workstation-path>") == 2
    assert "/Users/" not in rendered
    assert "C:\\Users\\" not in rendered


def test_source_index_redaction_keeps_non_path_contract_text():
    value = "cursor must advance and receipt must remain hash-bound"

    assert redact_workstation_paths(value) == value


def test_source_links_use_immutable_commit_not_mutable_release_branch():
    rendered = source_url("MAGI-v3", "api/server.py", 123)

    assert rendered == (
        f"https://github.com/WhaleChao/MAGI-v3/blob/{SOURCE_COMMIT}/"
        "api/server.py#L123"
    )
    assert "/blob/rc632" not in rendered
