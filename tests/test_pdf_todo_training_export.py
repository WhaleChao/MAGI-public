from __future__ import annotations

from pathlib import Path

from scripts.ops import pdf_todo_training_export as mod


def test_folder_kind_covers_court_notice_judgment_and_transcript():
    assert mod._folder_kind(Path("/cases/09_法院通知或程序裁定/a.pdf")) == "程序裁定"
    assert mod._folder_kind(Path("/cases/10_判決書或終局裁定及處分/a.pdf")) == "判決書或終局裁定及處分"
    assert mod._folder_kind(Path("/cases/10_判決書/a.pdf")) == "判決書或終局裁定及處分"
    assert mod._folder_kind(Path("/cases/08_筆錄/a.pdf")) == "筆錄"


def test_training_label_flags_todo_like_filename_without_hit():
    item = {
        "file_name": "20260513 花蓮地方法院函（請於115年6月3日前表示意見）.pdf",
        "todos": [],
        "text_error": "",
    }

    assert mod._training_label_for_pdf(item) == "todo_like_name_no_hit"


def test_write_outputs_creates_summary(tmp_path):
    rows = [
        {
            "source_kind": "court_pdf",
            "folder_kind": "判決書或終局裁定及處分",
            "training_label": "no_todo_expected",
            "todo_count": 0,
        },
        {
            "source_kind": "transcript_pdf",
            "folder_kind": "筆錄",
            "training_label": "transcript_high_confidence_todo",
            "todo_count": 1,
        },
    ]

    summary = mod.write_outputs(
        rows,
        jsonl_out=tmp_path / "training.jsonl",
        summary_out=tmp_path / "summary.json",
    )

    assert summary["rows"] == 2
    assert summary["todo_rows"] == 1
    assert summary["folder_kinds"]["筆錄"] == 1
