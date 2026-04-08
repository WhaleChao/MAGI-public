# -*- coding: utf-8 -*-
"""Tests for skills/ops/quicklook.py and skills/ops/finder_ops.py."""

import os
import time
from unittest.mock import patch, MagicMock

import pytest

from skills.ops.quicklook import (
    generate_thumbnail,
    generate_thumbnails_batch,
    cleanup_thumbnails,
)
from skills.ops.finder_ops import (
    move_file,
    copy_file,
    rename_file,
    create_folder,
    reveal_in_finder,
    get_file_info,
    move_to_trash,
)


# ── Quick Look tests ──

class TestGenerateThumbnail:
    @patch("skills.ops.quicklook.subprocess.run")
    def test_success(self, mock_run, tmp_path):
        # Create a fake source file
        src = tmp_path / "test.pdf"
        src.write_text("fake pdf")

        output_dir = str(tmp_path / "thumbs")
        os.makedirs(output_dir)

        # Simulate qlmanage creating a thumbnail
        thumb_path = os.path.join(output_dir, "test.pdf.png")
        mock_run.return_value = MagicMock(returncode=0)

        # Create the expected thumbnail file
        with open(thumb_path, "w") as f:
            f.write("fake png")

        result = generate_thumbnail(str(src), output_dir=output_dir)
        assert result == thumb_path

    def test_nonexistent_file(self):
        result = generate_thumbnail("/nonexistent/file.pdf")
        assert result is None

    @patch("skills.ops.quicklook.subprocess.run")
    def test_qlmanage_failure(self, mock_run, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("fake pdf")
        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        result = generate_thumbnail(str(src), output_dir=str(tmp_path))
        assert result is None


class TestCleanupThumbnails:
    def test_removes_old_files(self, tmp_path):
        # Create old and new files
        old_file = tmp_path / "old.png"
        old_file.write_text("old")
        os.utime(str(old_file), (0, 0))  # Set to epoch

        new_file = tmp_path / "new.png"
        new_file.write_text("new")

        deleted = cleanup_thumbnails(str(tmp_path), max_age_hours=1)
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_nonexistent_dir(self):
        assert cleanup_thumbnails("/nonexistent") == 0


class TestBatchThumbnails:
    @patch("skills.ops.quicklook.generate_thumbnail")
    def test_batch(self, mock_gen):
        mock_gen.side_effect = ["/tmp/a.png", None, "/tmp/c.png"]
        results = generate_thumbnails_batch(["/a.pdf", "/b.pdf", "/c.pdf"])
        assert results["/a.pdf"] == "/tmp/a.png"
        assert results["/b.pdf"] is None
        assert results["/c.pdf"] == "/tmp/c.png"


# ── Finder Ops tests ──

class TestFinderOps:
    @patch("skills.ops.finder_ops._run_osascript")
    def test_move_file(self, mock_osa, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")
        dst = tmp_path / "dest"
        dst.mkdir()

        mock_osa.return_value = (True, "")
        assert move_file(str(src), str(dst)) is True

    def test_move_nonexistent_source(self, tmp_path):
        dst = tmp_path / "dest"
        dst.mkdir()
        assert move_file("/nonexistent", str(dst)) is False

    def test_move_nonexistent_dest(self, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")
        assert move_file(str(src), "/nonexistent") is False

    @patch("skills.ops.finder_ops._run_osascript")
    def test_copy_file(self, mock_osa, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")
        dst = tmp_path / "dest"
        dst.mkdir()

        mock_osa.return_value = (True, "")
        assert copy_file(str(src), str(dst)) is True

    @patch("skills.ops.finder_ops._run_osascript")
    def test_rename_file(self, mock_osa, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")

        mock_osa.return_value = (True, "")
        assert rename_file(str(src), "new_name.pdf") is True

    @patch("skills.ops.finder_ops._run_osascript")
    def test_create_folder(self, mock_osa, tmp_path):
        mock_osa.return_value = (True, "")
        new_folder = str(tmp_path / "new_folder")
        assert create_folder(new_folder) is True

    def test_create_folder_already_exists(self, tmp_path):
        assert create_folder(str(tmp_path)) is True

    @patch("skills.ops.finder_ops._run_osascript")
    def test_reveal_in_finder(self, mock_osa):
        mock_osa.return_value = (True, "")
        assert reveal_in_finder("/tmp") is True

    @patch("skills.ops.finder_ops._run_osascript")
    def test_get_file_info(self, mock_osa, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")

        mock_osa.return_value = (True, "test.pdf||1234||PDF document||2026-04-08")
        info = get_file_info(str(src))
        assert info is not None
        assert info["name"] == "test.pdf"
        assert info["size"] == 1234
        assert info["kind"] == "PDF document"

    def test_get_file_info_nonexistent(self):
        assert get_file_info("/nonexistent") is None

    @patch("skills.ops.finder_ops._run_osascript")
    def test_move_to_trash(self, mock_osa, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_text("content")

        mock_osa.return_value = (True, "")
        assert move_to_trash(str(src)) is True

    def test_move_to_trash_nonexistent(self):
        assert move_to_trash("/nonexistent") is False
