from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path


def test_pdf_markitdown_image_only_falls_back_to_native(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_USE_MARKITDOWN", "1")
    import skills.obsidian.extractors as extractors

    importlib.reload(extractors)

    fake_reader = types.ModuleType("skills.engine.document_reader")

    class Result:
        success = True
        text = "![page](page-1.png)"

    fake_reader.read_document = lambda _path: Result()
    monkeypatch.setitem(sys.modules, "skills.engine.document_reader", fake_reader)
    monkeypatch.setattr(
        extractors,
        "_extract_pdf",
        lambda path: {"success": True, "text": "臺灣法院通知 被告應於115年7月9日到庭", "pages": 1, "method": "pdfplumber"},
    )

    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    result = extractors.extract_text(pdf)

    assert result["success"] is True
    assert result["method"] == "pdfplumber"
    assert "法院通知" in result["text"]


def test_structured_obsidian_note_contains_required_sections():
    import skills.obsidian.action as action

    frontmatter = action._generate_frontmatter(
        source_root="案件",
        source_path="/tmp/source.pdf",
        source_relpath="法扶案件/刑事/2026-0001-王小明-偵查-詐欺/09_法院通知或程序裁定/source.pdf",
        file_type="pdf",
        mtime=1,
        case_info={"case_number": "2026-0001", "client_name": "王小明"},
        file_hash_val="abc",
        extraction_method="pdfplumber",
        extraction_pages=1,
        extraction_quality="ok",
    )
    content = action._build_note_content(
        frontmatter=frontmatter,
        title="法院通知",
        relpath="法扶案件/刑事/2026-0001-王小明-偵查-詐欺/09_法院通知或程序裁定/source.pdf",
        suffix=".pdf",
        result={"method": "pdfplumber", "pages": 1},
        text="臺灣臺北地方法院通知 被告王小明應於115年7月9日上午10時到庭。請提出證據資料。",
        case_info={"case_number": "2026-0001", "client_name": "王小明"},
    )

    assert "summary_schema: magi-obsidian-note-v2" in content
    for heading in ("## 摘要", "## 法律/程序意義", "## 期限與待辦", "## 爭點與證據", "## Full Text"):
        assert heading in content
    assert "115年7月9日" in content


def test_note_content_strips_generated_imagefile_artifacts():
    import skills.obsidian.action as action

    frontmatter = action._generate_frontmatter(
        source_root="案件",
        source_path="/tmp/source.pdf",
        source_relpath="一般案件/民事/2025-0136-聯鋒國際有限公司-一審-支付命令/04_我方歷次書狀/source.pdf",
        file_type="pdf",
        mtime=1,
        case_info={"case_number": "2025-0136", "client_name": "聯鋒國際有限公司"},
        file_hash_val="abc",
        extraction_method="opendataloader_pdf",
        extraction_pages=1,
        extraction_quality="image_only",
    )
    content = action._build_note_content(
        frontmatter=frontmatter,
        title="20260105 支付命令聲請狀",
        relpath="一般案件/民事/2025-0136-聯鋒國際有限公司-一審-支付命令/source.pdf",
        suffix=".pdf",
        result={"method": "opendataloader_pdf", "pages": 1},
        text="\n".join(
            [
                "![image 61](<20260105 支付命令聲請狀_images/imageFile61.png>)",
                "![image 62](<20260105 支付命令聲請狀_images/imageFile62.png>)",
                "> _images/imageFile63.png>) LightPDF",
            ]
        ),
        case_info={"case_number": "2025-0136", "client_name": "聯鋒國際有限公司"},
    )

    assert "imageFile" not in content
    assert "![image" not in content
    assert "圖片佔位符" in content


def test_cleanup_duplicate_notes_moves_noncanonical_to_archive(tmp_path, monkeypatch):
    import skills.obsidian.action as action

    vault = tmp_path / "vault"
    case_dir = vault / "20_Notes" / "案件" / "法扶案件" / "刑事" / "2026-0001-王小明-偵查-詐欺" / "08_筆錄"
    case_dir.mkdir(parents=True)

    good = case_dir / "summary__調查筆錄.md"
    dup = case_dir / "summary__調查筆錄_2.md"
    for path, schema in ((good, "magi-obsidian-note-v2"), (dup, "")):
        path.write_text(
            "\n".join([
                "---",
                f"summary_schema: {schema}",
                "case_number: 2026-0001",
                "client_name: 王小明",
                "file_hash: samehash",
                "extraction_quality: ok",
                "---",
                "",
                "# 調查筆錄",
                "",
                "## 摘要",
                "內容",
                "",
                "## Full Text",
                "臺灣臺北地方法院調查筆錄內容，王小明到庭表示意見，法院諭知下次期日，書記官記載完整。" * 4,
            ]),
            encoding="utf-8",
        )

    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    ghost_rel = "20_Notes/案件/法扶案件/刑事/2026-0001-王小明-偵查-詐欺/ghost.md"
    monkeypatch.setattr(
        action,
        "_load_index",
        lambda: {"notes": {str(good.relative_to(vault)): {}, str(dup.relative_to(vault)): {}, ghost_rel: {}}},
    )
    saved = {}
    monkeypatch.setattr(action, "_replace_index", lambda idx: saved.update(idx))

    result = action.task_cleanup_duplicate_notes()

    assert result["success"] is True
    assert result["moved"] == 1
    assert result["orphaned_index_pruned"] == 1
    assert good.exists()
    assert not dup.exists()
    assert str(dup.relative_to(vault)) not in saved["notes"]
    assert ghost_rel not in saved["notes"]


def test_repair_notes_prioritizes_weak_reextract_with_limit(tmp_path, monkeypatch):
    import skills.obsidian.action as action
    import skills.obsidian.extractors as extractors

    vault = tmp_path / "vault"
    note_dir = vault / "20_Notes" / "案件" / "法扶案件" / "刑事" / "2026-0001-王小明-偵查-詐欺"
    note_dir.mkdir(parents=True)
    source = tmp_path / "weak.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    weak_note = note_dir / "summary__弱抽取.md"
    weak_note.write_text(
        f"""---
summary_schema: magi-obsidian-note-v2
case_number: 2026-0001
client_name: 王小明
source_path: {source}
source_relpath: 案件/weak.pdf
file_type: pdf
file_hash: weakhash
mtime: 1
extraction_method: markitdown
extraction_quality: image_only
---

# 弱抽取

## 摘要

- 舊摘要

## 法律/程序意義

- 舊資料

## 期限與待辦

- 舊資料

## 爭點與證據

- 舊資料

## Full Text

![page](page-1.png)
""",
        encoding="utf-8",
    )
    ok_text = "臺灣臺北地方法院通知王小明到庭並提出證據資料，內容完整可供檢索使用。" * 5
    ok_note = note_dir / "summary__正常.md"
    ok_note.write_text(
        f"""---
summary_schema: magi-obsidian-note-v2
case_number: 2026-0001
client_name: 王小明
extraction_method: pdfplumber
extraction_quality: ok
---

# 正常

## 摘要

- 已可用

## 法律/程序意義

- 已可用

## 期限與待辦

- 已可用

## 爭點與證據

- 已可用

## Full Text

{ok_text}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {str(weak_note.relative_to(vault)): {}}})
    monkeypatch.setattr(action, "_save_index", lambda _idx: None)
    monkeypatch.setattr(
        extractors,
        "extract_text",
        lambda _path: {
            "success": True,
            "method": "pdfplumber",
            "pages": 2,
            "text": "臺灣臺北地方法院通知王小明於115年7月9日到庭，並提出交易明細、匯款紀錄與答辯資料。" * 8,
        },
    )

    result = action.task_repair_notes(limit=1, reextract=True)

    assert result["success"] is True
    assert result["reextracted"] == 1
    repaired = weak_note.read_text("utf-8")
    assert "extraction_method: pdfplumber" in repaired
    assert "extraction_quality: ok" in repaired
    assert "115年7月9日" in repaired


def test_repair_notes_relocates_missing_source_path(tmp_path, monkeypatch):
    import skills.obsidian.action as action
    import skills.obsidian.extractors as extractors

    vault = tmp_path / "vault"
    note_dir = vault / "20_Notes" / "案件" / "一般案件" / "民事" / "2025-0136-聯鋒國際有限公司-一審-支付命令"
    note_dir.mkdir(parents=True)
    case_root = tmp_path / "cases"
    rel_source = Path("一般案件/民事/2025-0136-聯鋒國際有限公司-一審-支付命令/04_我方歷次書狀/source.pdf")
    real_source = case_root / rel_source
    real_source.parent.mkdir(parents=True)
    real_source.write_bytes(b"%PDF-1.4 fake")
    missing_source = tmp_path / "missing" / "source.pdf"

    note = note_dir / "summary__source.md"
    note.write_text(
        f"""---
summary_schema: magi-obsidian-note-v2
case_number: 2025-0136
client_name: 聯鋒國際有限公司
source_path: {missing_source}
source_relpath: {rel_source}
file_type: pdf
file_hash: weakhash
mtime: 1
extraction_method: opendataloader_pdf
extraction_quality: image_only
---

# source

## 摘要

- 舊摘要

## 法律/程序意義

- 舊資料

## 期限與待辦

- 舊資料

## 爭點與證據

- 舊資料

## Full Text

![image 1](<source_images/imageFile1.png>)
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(action, "_known_case_roots", lambda: [case_root])
    monkeypatch.setattr(action, "_load_index", lambda: {"notes": {str(note.relative_to(vault)): {}}})
    monkeypatch.setattr(action, "_save_index", lambda _idx: None)
    monkeypatch.setattr(
        extractors,
        "extract_text",
        lambda path: {
            "success": True,
            "method": "pdfplumber",
            "pages": 1,
            "text": f"從重新定位來源 {Path(path).name} 抽取到臺灣法院通知與支付命令資料。" * 6,
        },
    )

    result = action.task_repair_notes(limit=1, reextract=True)

    assert result["success"] is True
    assert result["source_relocated"] == 1
    assert result["missing_sources"] == 0
    repaired = note.read_text("utf-8")
    assert f"source_path: {real_source}" in repaired
    assert "imageFile" not in repaired


def test_ingest_rebuilds_same_hash_zero_chunk_notes(tmp_path, monkeypatch):
    import skills.obsidian.action as action

    vault = tmp_path / "vault"
    notes_dir = vault / "20_Notes"
    notes_dir.mkdir(parents=True)
    note = notes_dir / "note.md"
    note.write_text("臺灣臺北地方法院通知王小明到庭並提出證據資料。" * 5, encoding="utf-8")
    rel = str(note.relative_to(vault))
    current_hash = action._note_hash(note.read_text("utf-8"))
    current_mtime = int(note.stat().st_mtime)

    fake_vector_pipeline = types.ModuleType("skills.documents.vector_pipeline")
    fake_vector_pipeline.ingest_text_to_vector_memory = lambda **_kwargs: {
        "success": True,
        "doc_key": "doc-zero",
        "chunks_written": 2,
    }
    monkeypatch.setitem(sys.modules, "skills.documents.vector_pipeline", fake_vector_pipeline)
    monkeypatch.setattr(action, "_get_vault_path", lambda: vault)
    monkeypatch.setattr(
        action,
        "_load_index",
        lambda: {"notes": {rel: {"hash": current_hash, "mtime": current_mtime, "chunks": 0}}},
    )
    saved = {}
    monkeypatch.setattr(action, "_save_index", lambda idx: saved.update(idx))
    monkeypatch.setenv("MAGI_OBSIDIAN_CHECKPOINT_EVERY", "1")

    result = action.task_ingest(folder="20_Notes")

    assert result["success"] is True
    assert result["ingested"] == 1
    assert result["skipped"] == 0
    assert result["checkpoint_writes"] == 1
    assert saved["notes"][rel]["chunks"] == 2


def test_opendataloader_imagefile_only_result_is_rejected(monkeypatch, tmp_path):
    import skills.obsidian.extractors as extractors

    fake_ocr = types.ModuleType("skills.engine.ocr")

    class Result:
        success = True
        corrected_text = "\n".join(
            f"![image {i}](<source_images/imageFile{i}.png>)" for i in range(1, 12)
        )
        raw_text = ""
        provider = "opendataloader_pdf"

    fake_ocr.opendataloader_provider = types.SimpleNamespace(run_pdf=lambda *_args, **_kwargs: Result())
    monkeypatch.setitem(sys.modules, "skills.engine.ocr", fake_ocr)

    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    assert extractors._maybe_opendataloader_pdf(pdf, "") is None


def test_wiki_synthesizer_writes_required_pages(tmp_path, monkeypatch):
    import scripts.wiki_synthesizer as wiki

    vault = tmp_path / "vault"
    note_dir = vault / "20_Notes" / "案件" / "法扶案件" / "刑事" / "2026-0001-王小明-偵查-詐欺" / "09_法院通知或程序裁定"
    note_dir.mkdir(parents=True)
    note = note_dir / "summary__通知.md"
    note.write_text(
        """---
summary_schema: magi-obsidian-note-v2
case_number: 2026-0001
client_name: 王小明
---

# 通知

## 摘要

- 文件類型：法院通知
- 被告王小明應於115年7月9日到庭

## 期限與待辦

- 115年7月9日到庭

## Full Text

臺灣臺北地方法院通知，被告王小明應於115年7月9日到庭，並提出證據。
""",
        encoding="utf-8",
    )

    state_path = tmp_path / "wiki_state.json"
    monkeypatch.setattr(wiki, "WIKI_STATE_PATH", state_path)
    monkeypatch.setattr(wiki, "_get_vault_path", lambda: vault)
    monkeypatch.setenv("MAGI_WIKI_STRUCTURAL_ONLY", "1")

    wiki.synthesize(force=True, quiet=True, skip_ingest=True)

    case_wiki = vault / "30_Wiki" / "2026-0001-王小明"
    for page in wiki.WIKI_PAGE_NAMES:
        assert (case_wiki / f"{page}.md").exists()
    state = json.loads(state_path.read_text("utf-8"))
    assert set(state["cases"]["2026-0001"]["wiki_pages"]) == set(wiki.WIKI_PAGE_NAMES)
