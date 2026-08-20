from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from magi_v3.dispatcher import load_capability_worker_adapter
from magi_v3.forensic_transcript import build_worker_spec, verify_completion
from magi_v3.state import JobStatus


ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = ROOT / "skills" / "forensic-transcript-verifier" / "scripts" / "audit_engine.py"
SOFFICE_PATH = ROOT / "skills" / "docx" / "scripts" / "office" / "soffice.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location("forensic_audit_engine_test", ENGINE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ENGINE = _load_engine()


def _load_soffice_helper():
    spec = importlib.util.spec_from_file_location("forensic_soffice_helper_test", SOFFICE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOFFICE = _load_soffice_helper()
sys.path.insert(0, str(ENGINE_PATH.parent))

from video_agent import (  # noqa: E402
    _call_chat,
    _locked_turn_indices,
    _observation_pass,
    _review_text_points,
    _runtime_asr_evidence,
    _asr_provenance,
    _secondary_asr_independence,
    _transcribe_to_json,
    extract_contact_sheet_batch,
    merge_consecutive_same_speaker,
    prepare_autonomous_video_review,
    reconcile_visual_observations,
    run_autonomous_video_review,
)


def test_soffice_helper_resolves_known_binary_when_path_is_minimal(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "soffice"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(SOFFICE.shutil, "which", lambda _name: None)
    monkeypatch.setattr(SOFFICE, "SOFFICE_FALLBACKS", (binary,))

    assert SOFFICE._soffice_binary() == binary


def test_baseline_entries_can_match_one_semantically_merged_same_speaker_turn() -> None:
    baseline = """[00:00:01] 被告\n「第一句」\n\n[00:00:04] 被告\n「第二句」"""
    candidate = """[00:00:01–00:00:04] 被告\n「第一句。第二句」"""

    result = ENGINE.compare_baseline(candidate, baseline)

    assert result["baseline_entries"] == 2
    assert result["matched_entries"] == 2
    assert result["exact"] is True


def test_baseline_text_cannot_match_an_unrelated_turn_elsewhere() -> None:
    baseline = """[00:00:01] 被告\n「唯一句」"""
    candidate = """[00:00:01] 被告\n「別句」\n\n[00:00:09] 被告\n「唯一句」"""

    result = ENGINE.compare_baseline(candidate, baseline)

    assert result["exact"] is False
    assert result["mismatches"][0]["time_ok"] is True
    assert result["mismatches"][0]["text_ok"] is False


def test_speakerless_baseline_header_keeps_following_line_as_text() -> None:
    turns = ENGINE.parse_turns("""[01:06:23.21]\n\n  人工校正的句子""")

    assert len(turns) == 1
    assert turns[0].speaker == ""
    assert turns[0].text == "人工校正的句子"


def test_unresolved_items_section_is_not_appended_to_last_spoken_turn() -> None:
    turns = ENGINE.parse_turns(
        """[02:04:08–02:05:38] 吳慧珠
「好，謝謝。」

未決事項與人工最終確認清單

項次 時間 語者 未定內容及原因
1 約00:33:22 吳慧珠 姓名字形未定。

複核原則：不能確認者明示。"""
    )

    assert len(turns) == 1
    assert turns[0].text == "好，謝謝。"


def test_cross_speaker_overlap_is_flagged_but_clock_reset_is_a_new_block() -> None:
    text = """[00:01:00–00:01:05] 檢察官\n「問」

[00:01:04–00:01:07] 被告\n「答」

[00:00:01–00:00:03] 被告\n「另一時鐘的節文」"""
    turns = ENGINE.parse_turns(text)

    findings = ENGINE.find_overlaps(turns)

    assert len(ENGINE.split_monotonic_blocks(turns)) == 2
    assert len(findings) == 1
    assert findings[0]["cross_speaker"] is True


def test_baseline_lock_selects_the_reset_excerpt_block_not_same_clock_in_main_video() -> None:
    turns = ENGINE.parse_turns(
        """[01:06:23.21] 檢察官\n「主影片」

[01:10:00.00] 被告\n「主影片後段」

[01:06:23.21] 檢察官\n「人工節文」"""
    )

    locked = _locked_turn_indices(turns, {"01:06:23.21"})

    assert locked == {2}


def test_review_plan_covers_every_nonbaseline_turn_without_mapping_same_clock_to_excerpt(
    tmp_path: Path,
) -> None:
    video = tmp_path / "hearing.mp4"
    video.write_bytes(b"fixture")
    transcript = tmp_path / "candidate.txt"
    transcript.write_text(
        """[01:06:23.21–01:06:25] 檢察官\n「主影片」

[01:10:00–01:10:02] 被告\n「主影片後段」

[01:06:23.21] 檢察官\n「人工節文」""",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("[01:06:23.21] 檢察官\n「人工節文」", encoding="utf-8")
    asr = tmp_path / "asr.json"
    asr.write_text(json.dumps({"segments": []}), encoding="utf-8")

    result = prepare_autonomous_video_review(
        {
            "video": str(video),
            "transcript": str(transcript),
            "baseline": str(baseline),
            "asr_json": str(asr),
            "baseline_video_start": "01:23:49",
            "output_dir": str(tmp_path / "audit"),
            "max_visual_reviews": 20,
        }
    )

    full_turns = [
        row
        for row in result["review_points"]
        if "full_turn_review" in row.get("reasons", [])
    ]
    assert {row["turn_index"] for row in full_turns} == {0, 1}
    assert all(row["video_second"] < 4300 for row in result["review_points"])
    assert result["timeline_complete"] is True


def test_visual_speaker_change_requires_two_high_confidence_local_passes() -> None:
    point = {
        "id": "p0001",
        "video_time": "00:00:01.00",
        "speaker": "被告",
        "text": "答話",
        "candidate_speakers": ["檢察官", "被告"],
    }
    agreed = {
        "point": point,
        "decision": {"speaker": "檢察官", "confidence": 0.91, "uncertain": False},
    }
    conflict = {
        "point": point,
        "decision": {"speaker": "被告", "confidence": 0.93, "uncertain": False},
    }

    accepted, unresolved = reconcile_visual_observations([agreed], [agreed])
    rejected, conflict_rows = reconcile_visual_observations([agreed], [conflict])

    assert accepted == {"p0001": "檢察官"}
    assert unresolved == []
    assert rejected == {}
    assert conflict_rows[0]["reason"].startswith("兩次畫面觀察")


def test_visual_review_batches_same_turn_without_losing_point_evidence(tmp_path: Path) -> None:
    points = [
        {
            "id": f"p{index:04d}",
            "turn_index": 3,
            "video_second": float(index),
            "video_time": f"00:00:{index:02d}",
            "speaker": "被告",
            "text": f"第{index}點",
            "candidate_speakers": ["被告", "檢察官"],
        }
        for index in range(1, 8)
    ]
    batch_calls = []

    def fake_batch(_video, batch, output, *, pass_number):
        batch_calls.append([row["id"] for row in batch])
        return {"success": True, "path": str(output), "pass": pass_number}

    class BatchGateway:
        calls = 0

        @classmethod
        def _omlx_vision(cls, _image_path, prompt, **_kwargs):
            cls.calls += 1
            raw = re.search(r"觀察點 JSON：\n(\[.*\])\n只輸出", prompt, re.S)
            rows = json.loads(raw.group(1))
            return {
                "success": True,
                "route": "omlx",
                "model": "fixture-local",
                "analysis": json.dumps(
                    {
                        "decisions": [
                            {
                                "id": row["id"],
                                "speaker": "被告",
                                "confidence": 0.9,
                                "uncertain": False,
                            }
                            for row in rows
                        ]
                    },
                    ensure_ascii=False,
                ),
            }

    result = _observation_pass(
        {"video": str(tmp_path / "video.mp4")},
        {"review_points": points},
        pass_number=1,
        gateway=BatchGateway(),
        frame_extractor=lambda *_args, **_kwargs: {},
        batch_frame_extractor=fake_batch,
        output_dir=tmp_path,
    )

    assert len(result) == 7
    assert batch_calls == [[f"p{index:04d}" for index in range(1, 5)], [f"p{index:04d}" for index in range(5, 8)]]
    assert BatchGateway.calls == 2
    assert {row["point"]["id"] for row in result} == {row["id"] for row in points}
    assert all(row["model"]["success"] for row in result)


def test_batched_contact_sheet_constructs_one_ffmpeg_process_for_four_points(
    monkeypatch, tmp_path: Path
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    output = tmp_path / "batch.jpg"
    captured = []

    def fake_run(command, timeout=0, env=None):
        captured.append((command, timeout, env))
        output.write_bytes(b"image")
        return {"returncode": 0, "stdout": "", "stderr": "", "command": command}

    monkeypatch.setattr("video_agent.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("video_agent._run", fake_run)
    points = [
        {"id": f"p{index}", "video_second": float(index)} for index in range(1, 5)
    ]

    result = extract_contact_sheet_batch(video, points, output, pass_number=1)

    assert result["success"] is True
    assert len(captured) == 1
    command = captured[0][0]
    assert command.count("-i") == 4
    assert "vstack=inputs=4[out]" in command[command.index("-filter_complex") + 1]


def test_text_review_batches_all_points_of_one_turn_into_two_local_calls(tmp_path: Path) -> None:
    calls = []

    class TextGateway:
        @staticmethod
        def _omlx_chat(prompt, **_kwargs):
            calls.append(prompt)
            return {
                "success": True,
                "route": "omlx",
                "model": "fixture-local",
                "response": json.dumps(
                    {
                        "change_required": False,
                        "speaker": "被告",
                        "text": "完整答話",
                        "confidence": 0.95,
                    },
                    ensure_ascii=False,
                ),
            }

    points = [
        {
            "id": f"p{index}",
            "turn_index": 0,
            "display": "00:00:01–00:00:20",
            "speaker": "被告",
            "text": "完整答話",
            "video_start": 1,
            "video_end": 20,
            "video_second": float(index),
            "video_time": f"00:00:{index:02d}",
            "reason": "full_turn_review" if index == 1 else "speaker_sweep",
            "reasons": ["full_turn_review" if index == 1 else "speaker_sweep"],
            "baseline_locked": False,
        }
        for index in range(1, 8)
    ]
    segments = [{"start": 1, "end": 20, "text": "完整答話"}]
    _accepted, _unresolved, proposals = _review_text_points(
        {"review_points": points},
        segments,
        segments,
        {},
        gateway=TextGateway(),
        output_dir=tmp_path,
    )

    assert len(calls) == 2
    assert len(proposals) == 1
    assert proposals[0]["covered_review_points"] == 7
    assert proposals[0]["point_ids"] == [f"p{index}" for index in range(1, 8)]


def test_semantic_merge_keeps_all_words_and_never_merges_locked_baseline() -> None:
    turns = ENGINE.parse_turns(
        """[00:00:01–00:00:02] 被告\n「第一句」

[00:00:02.20–00:00:03] 被告\n「第二句」

[00:00:00.50] 被告\n「節文」"""
    )

    merged = merge_consecutive_same_speaker(turns, locked_indices={2}, maximum_gap=1.5)

    assert len(merged) == 2
    assert "第一句" in merged[0].text and "第二句" in merged[0].text
    assert merged[1].text == "節文"


class _FakeLocalGateway:
    @staticmethod
    def _omlx_vision(_image_path: str, prompt: str, **_kwargs):
        speaker = re.search(r"現有草稿標籤：([^\n]+)", prompt).group(1).strip()
        return {
            "success": True,
            "route": "omlx",
            "model": "fake-local-vision",
            "analysis": json.dumps(
                {
                    "speaker": speaker,
                    "active_position": "中",
                    "confidence": 0.95,
                    "uncertain": False,
                    "visible_evidence": "連續畫面可見發話動作",
                    "contrary_evidence": "",
                },
                ensure_ascii=False,
            ),
        }

    @staticmethod
    def _omlx_chat(prompt: str, **_kwargs):
        speaker = re.search(r"草稿發話者：([^\n]+)", prompt).group(1).strip()
        text = re.search(r"草稿文字：([^\n]+)", prompt).group(1).strip()
        return {
            "success": True,
            "route": "omlx",
            "model": "fake-local-text",
            "response": json.dumps(
                {
                    "change_required": False,
                    "speaker": speaker,
                    "text": text,
                    "confidence": 0.95,
                    "reason": "兩路一致，維持草稿",
                },
                ensure_ascii=False,
            ),
        }


def test_text_review_uses_direct_local_route_not_general_fallback_chain() -> None:
    class DirectLocalGateway:
        @staticmethod
        def _omlx_chat(_prompt: str, **_kwargs):
            return {
                "success": True,
                "route": "omlx",
                "model": "local-model",
                "response": '{"change_required":false}',
            }

        @staticmethod
        def chat(*_args, **_kwargs):
            raise AssertionError("general fallback chain must not be called")

    parsed, model = _call_chat(DirectLocalGateway(), "test")

    assert parsed == {"change_required": False}
    assert model["success"] is True
    assert model["route"] == "omlx"


def test_visual_review_rejects_public_gateway_before_any_possible_cloud_call() -> None:
    from video_agent import _call_vision

    class PublicOnlyGateway:
        @staticmethod
        def vision(*_args, **_kwargs):
            raise AssertionError("public gateway may contain a cloud route")

    parsed, model = _call_vision(PublicOnlyGateway(), "/fixture.jpg", "prompt")

    assert parsed == {}
    assert model["success"] is False
    assert "local-only" in model["error"]


def test_autonomous_workflow_needs_no_codex_and_generates_valid_docx(tmp_path: Path) -> None:
    video = tmp_path / "hearing.mp4"
    video.write_bytes(b"fixture-video")
    transcript = tmp_path / "candidate.txt"
    transcript.write_text(
        """[00:00:10–00:00:12] 檢察官\n「你有看到嗎？」

[00:00:12–00:00:14] 被告\n「我有看到。」

[00:00:00.50] 檢察官\n「人工節文」""",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("[00:00:00.50] 檢察官\n「人工節文」", encoding="utf-8")
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    primary_segments = {
        "segments": [
            {"start": 10.0, "end": 12.0, "text": "你有看到嗎？"},
            {"start": 12.0, "end": 14.0, "text": "我有看到。"},
        ],
        "forensic_provenance": {
            "backend_id": "mlx_whisper",
            "model_id": "/models/court-primary",
            "install_identity_sha256": "primary-install",
            "run_id": "primary-run",
            "audio_sha256": "primary-audio",
            "model_artifact_sha256": "primary-model-sha",
            "backend_binary_sha256": "primary-binary-sha",
            "license_id": "MIT",
            "execution_ordinal": 1,
            "offline": True,
        },
    }
    secondary_segments = {
        "segments": [
            {"start": 10.0, "end": 12.1, "text": "你有看到嗎？"},
            {"start": 12.1, "end": 14.0, "text": "我有看到。"},
        ],
        "forensic_provenance": {
            "backend_id": "whisper_cli",
            "model_id": "/models/court-secondary.pt",
            "install_identity_sha256": "secondary-install",
            "run_id": "secondary-run",
            "audio_sha256": "secondary-audio",
            "model_artifact_sha256": "secondary-model-sha",
            "backend_binary_sha256": "secondary-binary-sha",
            "license_id": "MIT",
            "execution_ordinal": 2,
            "offline": True,
        },
    }
    primary.write_text(json.dumps(primary_segments, ensure_ascii=False), encoding="utf-8")
    secondary.write_text(json.dumps(secondary_segments, ensure_ascii=False), encoding="utf-8")

    def fake_frame(_video, _second, output, *, pass_number):
        return {"success": True, "path": str(output), "pass": pass_number}

    result = run_autonomous_video_review(
        {
            "video": str(video),
            "transcript": str(transcript),
            "baseline": str(baseline),
            "asr_json": str(primary),
            "secondary_asr_json": str(secondary),
            "secondary_asr_independent_verified": True,
            "output_dir": str(tmp_path / "audit"),
            "output_docx": "final.docx",
            "max_visual_reviews": 10,
        },
        gateway=_FakeLocalGateway(),
        frame_extractor=fake_frame,
    )

    assert result["independent_of_codex"] is True
    assert result["local_model_execution_complete"] is True
    assert result["video_observation_passes"] == 2
    assert result["secondary_asr_independent"] is True
    assert result["audit_consistent"] is True
    assert result["docx_validation"]["passed"] is True
    validation = result["docx_validation"]
    render = validation["render"]
    if render.get("renderer") == "python-docx+reportlab-cjk/v1":
        assert render["command"] == []
        assert render["network_access_performed"] is False
        assert render["subprocess_started"] is False
        assert len(render["source_docx_sha256"]) == 64
        assert len(render["source_text_sha256"]) == 64
        assert render["structure"]["paragraphs"] > 0
        assert render["structure"]["tables"] >= 0
        assert all(section["a4"] for section in render["structure"]["sections"])
        assert render["limitations"]
    else:
        assert render["isolation"] == {
            "method": "unique-user-installation",
            "profile_inside_output_dir": True,
            "profile_removed_after_render": True,
            "network_policy_unchanged": True,
        }
        render_command = render["command"]
        assert any(
            part.startswith("-env:UserInstallation=file://")
            for part in render_command
        )
        assert {"--nolockcheck", "--norestore", "--nofirststartwizard"}.issubset(
            render_command
        )
    assert validation["render"]["fresh_pdf"]["passed"] is True
    assert len(validation["render"]["fresh_pdf"]["sha256"]) == 64
    assert validation["pdfinfo"]["pages"] >= 1
    assert validation["pdfinfo"]["a4"] is True
    assert len(validation["pdfinfo"]["binary_sha256"]) == 64
    assert validation["pdf_text_validation"]["extracted_characters"] > 0
    assert validation["pdf_text_validation"]["extracted_has_cjk"] is True
    assert len(validation["pdf_text_validation"]["binary_sha256"]) == 64
    assert validation["pdf_text_validation"]["all_time_markers_present"] is True
    assert validation["pdf_text_validation"]["all_speakers_present"] is True
    assert validation["pdf_text_validation"]["all_turn_text_present"] is True
    assert result["passed"] is True
    assert result["court_grade_contract_satisfied"] is True
    assert Path(result["output_docx"]).is_file()
    output_text, _ = ENGINE.extract_text(result["output_docx"], tmp_path / "readback")
    output_turns = ENGINE.parse_turns(output_text)
    assert output_turns[-1].text == "人工節文"


def test_formal_seatbelt_fallback_renders_and_verifies_when_soffice_returns_no_pdf(
    tmp_path: Path, monkeypatch,
) -> None:
    from write_transcript_docx import write_document

    transcript = write_document(
        {
            "output_path": str(tmp_path / "forensic.docx"),
            "turns": [
                {
                    "display": "00:00:10–00:00:12",
                    "speaker": "檢察官",
                    "text": "你有看到嗎？",
                },
                {
                    "display": "00:00:12–00:00:14",
                    "speaker": "被告",
                    "text": "我有看到。",
                },
            ],
            "unresolved": [],
        }
    )
    original_run = ENGINE._run

    def no_pdf_from_soffice(command, **kwargs):
        if any(str(part).endswith("/soffice.py") for part in command):
            return {
                "command": command,
                "returncode": 0,
                "stdout": "",
                "stderr": "TIS/Keyboard Layout warning",
            }
        return original_run(command, **kwargs)

    monkeypatch.setattr(ENGINE, "_run", no_pdf_from_soffice)
    monkeypatch.setenv("MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD", "1")
    monkeypatch.setenv("MAGI_V3_OFFLINE_CERTIFICATION", "1")

    result = ENGINE.validate_docx(
        {"transcript": str(transcript), "output_dir": str(tmp_path / "render")},
        magi_root=ROOT,
    )

    assert result["libreoffice_render"]["returncode"] == 0
    assert result["libreoffice_render"]["fresh_pdf"]["passed"] is False
    assert result["render"]["renderer"] == "python-docx+reportlab-cjk/v1"
    assert result["render"]["network_access_performed"] is False
    assert result["render"]["subprocess_started"] is False
    assert len(result["render"]["font_sha256"]) == 64
    assert result["render"]["fresh_pdf"]["passed"] is True
    assert result["pdfinfo"]["a4"] is True
    assert result["pdfinfo"]["pages"] >= 1
    assert result["pdf_text_validation"]["extracted_has_cjk"] is True
    assert result["pdf_text_validation"]["all_time_markers_present"] is True
    assert result["pdf_text_validation"]["all_speakers_present"] is True
    assert result["pdf_text_validation"]["all_turn_text_present"] is True
    assert result["passed"] is True


def test_seatbelt_child_without_offline_certification_cannot_use_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    from write_transcript_docx import write_document

    transcript = write_document(
        {
            "output_path": str(tmp_path / "forensic.docx"),
            "turns": [
                {
                    "display": "00:00:10–00:00:12",
                    "speaker": "檢察官",
                    "text": "你有看到嗎？",
                }
            ],
            "unresolved": [],
        }
    )
    original_run = ENGINE._run

    def no_pdf_from_soffice(command, **kwargs):
        if any(str(part).endswith("/soffice.py") for part in command):
            return {"command": command, "returncode": 0, "stdout": "", "stderr": ""}
        return original_run(command, **kwargs)

    monkeypatch.setattr(ENGINE, "_run", no_pdf_from_soffice)
    monkeypatch.setattr(
        ENGINE,
        "_wait_for_fresh_pdf",
        lambda *_args, **_kwargs: {"passed": False, "reason": "fixture no PDF"},
    )
    monkeypatch.setenv("MAGI_V3_RELEASE_QUALITY_SEATBELT_CHILD", "1")
    monkeypatch.delenv("MAGI_V3_OFFLINE_CERTIFICATION", raising=False)

    result = ENGINE.validate_docx(
        {"transcript": str(transcript), "output_dir": str(tmp_path / "render")},
        magi_root=ROOT,
    )

    assert "libreoffice_render" not in result
    assert result["render"]["fresh_pdf"]["passed"] is False
    assert result["passed"] is False


def test_docx_validation_rejects_empty_document_fail_closed(
    tmp_path: Path,
) -> None:
    from docx import Document

    transcript = tmp_path / "empty.docx"
    Document().save(transcript)

    result = ENGINE.validate_docx(
        {"transcript": str(transcript), "output_dir": str(tmp_path / "render")},
        magi_root=ROOT,
    )

    pages = result["pdfinfo"]["pages"]
    if pages is not None:
        assert pages >= 1
    assert result["pdfinfo"]["passed"] is False
    if "extracted_characters" in result["pdf_text_validation"]:
        assert result["pdf_text_validation"]["extracted_characters"] == 0
    assert result["pdf_text_validation"]["passed"] is False
    assert result["passed"] is False


def test_court_grade_rejects_same_content_secondary_before_model_calls(tmp_path: Path) -> None:
    video = tmp_path / "hearing.mp4"
    video.write_bytes(b"fixture-video")
    transcript = tmp_path / "candidate.txt"
    transcript.write_text(
        "[00:00:10–00:00:12] 檢察官\n「問題」\n\n"
        "[00:00:00.50] 檢察官\n「人工節文」",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("[00:00:00.50] 檢察官\n「人工節文」", encoding="utf-8")
    body = {
        "segments": [{"start": 10.0, "end": 12.0, "text": "問題"}],
        "forensic_provenance": {
            "backend_id": "mlx_whisper",
            "model_id": "/models/same",
            "install_identity_sha256": "same-install",
            "run_id": "same-run",
            "audio_sha256": "same-audio",
            "model_artifact_sha256": "same-model-sha",
            "backend_binary_sha256": "same-binary-sha",
            "license_id": "MIT",
            "execution_ordinal": 1,
            "offline": True,
        },
    }
    primary = tmp_path / "primary.json"
    secondary = tmp_path / "secondary.json"
    primary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    secondary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

    class NoModelCalls:
        def __getattr__(self, name):
            raise AssertionError(f"model route reached before ASR independence gate: {name}")

    try:
        run_autonomous_video_review(
            {
                "video": str(video),
                "transcript": str(transcript),
                "baseline": str(baseline),
                "asr_json": str(primary),
                "secondary_asr_json": str(secondary),
                "output_dir": str(tmp_path / "audit"),
            },
            gateway=NoModelCalls(),
        )
    except RuntimeError as exc:
        assert "same_transcript_content" in str(exc)
        assert "same_backend" in str(exc)
        assert "same_model" in str(exc)
    else:
        raise AssertionError("same-content/same-model ASR was accepted as independent")


def _worker_payload(worker) -> dict:
    index = worker.argv.index("--task")
    return json.loads(worker.argv[index + 1])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure_dual_runtime(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    primary = tmp_path / "mlx-turbo"
    primary.mkdir()
    weights = primary / "weights.safetensors"
    config = primary / "config.json"
    weights.write_bytes(b"fixture-mlx-turbo-weights")
    config.write_text('{"model_type":"whisper"}', encoding="utf-8")
    primary_binary = tmp_path / "mlx-load-models.py"
    primary_binary.write_text("# fixture backend\n", encoding="utf-8")
    model_dir = tmp_path / "whisper-models"
    model_dir.mkdir()
    secondary = model_dir / "tiny.pt"
    secondary.write_bytes(b"fixture-whisper-tiny")
    secondary_binary = tmp_path / "whisper"
    secondary_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    secondary_binary.chmod(0o755)
    values = {
        "MAGI_FORENSIC_PRIMARY_ASR_BACKEND": "mlx_whisper",
        "MAGI_FORENSIC_PRIMARY_ASR_MODEL": str(primary),
        "MAGI_FORENSIC_MLX_MODEL_PATH": str(primary),
        "MAGI_FORENSIC_PRIMARY_BACKEND_BINARY": str(primary_binary),
        "MAGI_FORENSIC_PRIMARY_MODEL_LICENSE": "MIT",
        "MAGI_FORENSIC_PRIMARY_MODEL_WEIGHTS_SHA256": _sha(weights),
        "MAGI_FORENSIC_PRIMARY_MODEL_CONFIG_SHA256": _sha(config),
        "MAGI_FORENSIC_PRIMARY_BACKEND_BINARY_SHA256": _sha(primary_binary),
        "MAGI_FORENSIC_SECONDARY_ASR_BACKEND": "whisper_cli",
        "MAGI_FORENSIC_SECONDARY_ASR_MODEL": str(secondary),
        "MAGI_WHISPER_MODEL_DIR": str(model_dir),
        "MAGI_WHISPER_BIN": str(secondary_binary),
        "MAGI_FORENSIC_SECONDARY_MODEL_LICENSE": "MIT",
        "MAGI_FORENSIC_SECONDARY_MODEL_WEIGHTS_SHA256": _sha(secondary),
        "MAGI_FORENSIC_SECONDARY_BACKEND_BINARY_SHA256": _sha(secondary_binary),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return {
        "primary": primary,
        "weights": weights,
        "config": config,
        "primary_binary": primary_binary,
        "secondary": secondary,
        "secondary_binary": secondary_binary,
    }


def _write_test_v3_binding(payload: dict, result: dict) -> None:
    output_dir = Path(payload["output_dir"])
    names = {
        "audit": ("audit.json",),
        "autonomous": ("autonomous.json", "audit.json", "docx-validation.json"),
    }[payload["operation"]]
    report_hashes = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in names
    }
    artifact_hashes = {}
    if result.get("output_docx"):
        artifact = Path(result["output_docx"])
        artifact_hashes[artifact.name] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output_dir / "v3-completion-binding.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contract": payload["_v3_contract"],
                "completed_at_ns": time.time_ns(),
                "report_sha256": report_hashes,
                "artifact_sha256": artifact_hashes,
            }
        ),
        encoding="utf-8",
    )


def _bound_asr_report(payload: dict) -> dict:
    runtime = payload["_v3_contract"]["asr_runtime"]
    return {
        "primary": {
            "provenance": {
                "model_evidence": runtime["primary"],
                "execution_ordinal": 1,
                "execution_mode": "serialized",
            }
        },
        "secondary": {
            "provenance": {
                "model_evidence": runtime["secondary"],
                "execution_ordinal": 2,
                "execution_mode": "serialized",
            }
        },
    }


def test_v3_adapter_builds_shared_skill_worker_and_requires_report_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAGI_V3_FORENSIC_MUTABLE_ROOT", str(tmp_path / "v3-mutable"))
    mlx_model = tmp_path / "mlx-model"
    mlx_model.mkdir()
    monkeypatch.setenv("MAGI_FORENSIC_MLX_MODEL_PATH", str(mlx_model))
    monkeypatch.setenv("INFERENCE_LOCAL_OLLAMA_BASE", "http://127.0.0.1:8080")
    transcript = tmp_path / "candidate.docx"
    transcript.write_bytes(b"test")
    baseline = tmp_path / "baseline.docx"
    baseline.write_bytes(b"baseline")
    job = SimpleNamespace(
        capability="audio_transcription_translation",
        operation="audit",
        input={
            "transcript": str(transcript),
            "baseline": str(baseline),
        },
        resource_claim={
            "memory_mb": 512,
            "metal_mb": 0,
            "cpu_percent": 100,
            "disk_io": "light",
            "nas_io": "none",
            "network": "none",
            "browser_tokens": 0,
        },
        job_id="forensic-job",
        priority_class="P2",
        timeout_sec=900,
    )
    lease = SimpleNamespace(attempt_number=1, token="lease-token")

    worker = build_worker_spec(job, lease)
    payload = _worker_payload(worker)
    output_dir = Path(payload["output_dir"])
    (output_dir / "audit.json").write_text(
        json.dumps({"passed_deterministic_gates": True}), encoding="utf-8"
    )
    _write_test_v3_binding(payload, {"operation": "audit"})
    completion = verify_completion(
        job,
        lease,
        SimpleNamespace(returncode=0, timed_out=False, killed=False, duration_sec=0.1),
    )

    assert worker.worker_class == "transcription"
    assert any(part.endswith("skills/forensic-transcript-verifier/action.py") for part in worker.argv)
    assert worker.env["HF_HUB_OFFLINE"] == "1"
    assert worker.env["MAGI_FORENSIC_MLX_MODEL_PATH"] == str(mlx_model)
    assert worker.env["INFERENCE_LOCAL_OLLAMA_BASE"] == "http://127.0.0.1:8080"
    assert worker.network == "none"
    assert str(output_dir).startswith(str(tmp_path / "v3-mutable"))
    assert (output_dir / "home").is_dir()
    assert (output_dir / "tmp").is_dir()
    assert completion.target is JobStatus.SUCCEEDED
    assert completion.business_completed is True
    assert completion.result["manual_second_pass_required"] is True
    try:
        build_worker_spec(job, lease)
    except ValueError as exc:
        assert "workspace already exists" in str(exc)
    else:
        raise AssertionError("a stale lease workspace was reused")


def test_v3_adapter_accepts_autonomous_completion_only_with_final_docx_and_gates(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("MAGI_V3_FORENSIC_MUTABLE_ROOT", str(tmp_path / "v3-mutable"))
    dual = _configure_dual_runtime(monkeypatch, tmp_path)
    video = tmp_path / "hearing.mp4"
    transcript = tmp_path / "draft.docx"
    baseline = tmp_path / "baseline.docx"
    video.write_bytes(b"video")
    transcript.write_bytes(b"draft")
    baseline.write_bytes(b"baseline")
    job = SimpleNamespace(
        capability="audio_transcription_translation",
        operation="autonomous",
        input={
            "video": str(video),
            "transcript": str(transcript),
            "baseline": str(baseline),
        },
        resource_claim={
            "memory_mb": 6144,
            "metal_mb": 3072,
            "cpu_percent": 400,
            "disk_io": "heavy",
            "nas_io": "none",
            "network": "light",
            "browser_tokens": 0,
        },
        job_id="autonomous-job",
        priority_class="P2",
        timeout_sec=3600,
    )
    lease = SimpleNamespace(attempt_number=1, token="lease-token")
    worker = build_worker_spec(job, lease)
    payload = _worker_payload(worker)
    output_dir = Path(payload["output_dir"])
    final_docx = output_dir / "final.docx"
    final_docx.write_bytes(b"artifact")
    (output_dir / "autonomous.json").write_text(
        json.dumps(
            {
                "passed": True,
                "court_grade_contract_satisfied": False,
                "output_docx": str(final_docx),
                "dual_asr_execution": "serialized",
                "asr_evidence": _bound_asr_report(payload),
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "audit.json").write_text(
        json.dumps({"passed_deterministic_gates": True}), encoding="utf-8"
    )
    (output_dir / "docx-validation.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )
    _write_test_v3_binding(
        payload,
        {"operation": "autonomous", "output_docx": str(final_docx)},
    )
    rejected = verify_completion(
        job,
        lease,
        SimpleNamespace(returncode=0, timed_out=False, killed=False, duration_sec=0.1),
    )
    (output_dir / "autonomous.json").write_text(
        json.dumps(
            {
                "passed": True,
                "court_grade_contract_satisfied": True,
                "output_docx": str(final_docx),
                "dual_asr_execution": "serialized",
                "asr_evidence": _bound_asr_report(payload),
            }
        ),
        encoding="utf-8",
    )
    _write_test_v3_binding(
        payload,
        {"operation": "autonomous", "output_docx": str(final_docx)},
    )
    completion = verify_completion(
        job,
        lease,
        SimpleNamespace(returncode=0, timed_out=False, killed=False, duration_sec=0.1),
    )

    assert worker.worker_class == "transcription"
    assert worker.estimated_footprint_mb == 6144
    assert worker.estimated_metal_mb == 3072
    assert worker.disk_io == "heavy"
    assert worker.env["MAGI_FORENSIC_DUAL_ASR_SERIALIZED"] == "1"
    assert worker.env["MAGI_FORENSIC_PRIMARY_ASR_MODEL"] == str(dual["primary"])
    runtime = payload["_v3_contract"]["asr_runtime"]
    assert runtime["execution"] == "serialized"
    assert runtime["maximum_concurrent_heavy_workers"] == 1
    assert runtime["primary"]["weights"]["sha256"] == _sha(dual["weights"])
    assert runtime["secondary"]["weights"]["sha256"] == _sha(dual["secondary"])
    assert rejected.target is JobStatus.FAILED
    assert completion.target is JobStatus.SUCCEEDED
    assert completion.result["human_final_confirmation_required"] is True
    assert completion.artifacts[0]["uri"] == str(final_docx)


def test_v3_adapter_rejects_release_inputs_and_underclaimed_autonomous_worker(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAGI_V3_FORENSIC_MUTABLE_ROOT", str(tmp_path / "v3-mutable"))
    _configure_dual_runtime(monkeypatch, tmp_path)
    outside_video = tmp_path / "video.mp4"
    outside_video.write_bytes(b"video")
    outside_baseline = tmp_path / "baseline.docx"
    outside_baseline.write_bytes(b"baseline")
    job = SimpleNamespace(
        capability="audio_transcription_translation",
        operation="autonomous",
        input={
            "video": str(outside_video),
            "transcript": str(ROOT / "requirements.txt"),
            "baseline": str(outside_baseline),
        },
        resource_claim={
            "memory_mb": 512,
            "metal_mb": 0,
            "cpu_percent": 100,
            "disk_io": "light",
            "nas_io": "none",
            "network": "none",
            "browser_tokens": 0,
        },
        job_id="unsafe-job",
        priority_class="P2",
        timeout_sec=3600,
    )
    lease = SimpleNamespace(attempt_number=1, token="lease-token")

    try:
        build_worker_spec(job, lease)
    except ValueError as exc:
        assert "release/V3 production" in str(exc)
    else:
        raise AssertionError("release tree input was accepted")


def test_v3_autonomous_rejects_underclaimed_dual_asr_before_workspace_creation(
    monkeypatch, tmp_path: Path
) -> None:
    mutable = tmp_path / "v3-mutable"
    monkeypatch.setenv("MAGI_V3_FORENSIC_MUTABLE_ROOT", str(mutable))
    _configure_dual_runtime(monkeypatch, tmp_path)
    video = tmp_path / "video.mp4"
    transcript = tmp_path / "draft.docx"
    baseline = tmp_path / "baseline.docx"
    for path in (video, transcript, baseline):
        path.write_bytes(path.name.encode())
    job = SimpleNamespace(
        capability="audio_transcription_translation",
        operation="autonomous",
        input={"video": str(video), "transcript": str(transcript), "baseline": str(baseline)},
        resource_claim={
            "memory_mb": 4096,
            "metal_mb": 2048,
            "cpu_percent": 400,
            "disk_io": "heavy",
            "nas_io": "none",
            "network": "light",
            "browser_tokens": 0,
        },
        job_id="underclaimed",
        priority_class="P2",
        timeout_sec=3600,
    )

    try:
        build_worker_spec(job, SimpleNamespace(attempt_number=1, token="lease"))
    except ValueError as exc:
        assert "memory claim" in str(exc)
    else:
        raise AssertionError("underclaimed dual ASR was admitted")
    assert not mutable.exists()


def test_runtime_artifact_sha_mismatch_fails_before_audio_or_model_call(
    monkeypatch, tmp_path: Path
) -> None:
    dual = _configure_dual_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MAGI_FORENSIC_PRIMARY_MODEL_WEIGHTS_SHA256", "0" * 64)

    try:
        _runtime_asr_evidence("PRIMARY", "mlx_whisper", str(dual["primary"]))
    except RuntimeError as exc:
        assert "SHA mismatch" in str(exc)
    else:
        raise AssertionError("mutated primary weights were not rejected")


def test_bound_dual_asr_runs_serially_and_persists_content_provenance(
    monkeypatch, tmp_path: Path
) -> None:
    dual = _configure_dual_runtime(monkeypatch, tmp_path)
    video = tmp_path / "fixture.wav"
    video.write_bytes(b"non-sensitive-fixture")
    output = tmp_path / "output"
    calls = []

    def fake_extract(_video, audio, *, filtered):
        Path(audio).parent.mkdir(parents=True, exist_ok=True)
        Path(audio).write_bytes(b"filtered-audio" if filtered else b"primary-audio")
        calls.append("extract-secondary" if filtered else "extract-primary")
        return {"success": True, "path": str(audio), "error": ""}

    def fake_mlx(*_args, **_kwargs):
        calls.append("mlx")
        return {
            "success": True,
            "text": "第一路文字",
            "segments": [{"start": 0.0, "end": 1.0, "text": "第一路文字"}],
        }

    def fake_cli(*_args, **_kwargs):
        calls.append("cli")
        return {
            "success": True,
            "text": "第二路文字",
            "segments": [{"start": 0.0, "end": 1.1, "text": "第二路文字"}],
            "model": str(dual["secondary"]),
        }

    monkeypatch.setattr("video_agent._extract_audio", fake_extract)
    monkeypatch.setattr("skills.hearing.balthasar_local.transcribe_audio", fake_mlx)
    monkeypatch.setattr(
        "skills.bridge.balthasar_bridge._transcribe_with_whisper_cli", fake_cli
    )

    primary_path, primary_segments = _transcribe_to_json(video, output, filtered=False)
    secondary_path, secondary_segments = _transcribe_to_json(video, output, filtered=True)
    primary = _asr_provenance(Path(primary_path), primary_segments)
    secondary = _asr_provenance(Path(secondary_path), secondary_segments)
    independent, reasons = _secondary_asr_independence(primary, secondary)

    assert calls == ["extract-primary", "mlx", "extract-secondary", "cli"]
    assert primary["execution_ordinal"] == 1
    assert secondary["execution_ordinal"] == 2
    assert primary["model_artifact_sha256"] == _sha(dual["weights"])
    assert secondary["model_artifact_sha256"] == _sha(dual["secondary"])
    assert primary["backend_binary_sha256"] == _sha(dual["primary_binary"])
    assert secondary["backend_binary_sha256"] == _sha(dual["secondary_binary"])
    assert primary["audio_sha256"] != secondary["audio_sha256"]
    assert primary["run_id"] != secondary["run_id"]
    assert independent is True
    assert reasons == []


def test_dual_asr_preflight_rejects_missing_license_and_same_backend(
    monkeypatch, tmp_path: Path
) -> None:
    _configure_dual_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("MAGI_FORENSIC_SECONDARY_MODEL_LICENSE", "")
    from magi_v3.forensic_transcript import _validated_dual_asr_runtime

    try:
        _validated_dual_asr_runtime()
    except ValueError as exc:
        assert "MODEL_LICENSE" in str(exc)
    else:
        raise AssertionError("missing secondary model license was accepted")

    monkeypatch.setenv("MAGI_FORENSIC_SECONDARY_MODEL_LICENSE", "MIT")
    monkeypatch.setenv("MAGI_FORENSIC_SECONDARY_ASR_BACKEND", "mlx_whisper")
    try:
        _validated_dual_asr_runtime()
    except ValueError as exc:
        assert "backend pair" in str(exc)
    else:
        raise AssertionError("same backend was accepted as dual ASR")


def test_v3_adapter_rejects_nonlocal_model_runtime_before_worker_start(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAGI_V3_FORENSIC_MUTABLE_ROOT", str(tmp_path / "v3-mutable"))
    monkeypatch.setenv("INFERENCE_LOCAL_OLLAMA_BASE", "https://models.example.invalid")
    transcript = tmp_path / "candidate.docx"
    baseline = tmp_path / "baseline.docx"
    transcript.write_bytes(b"transcript")
    baseline.write_bytes(b"baseline")
    job = SimpleNamespace(
        capability="audio_transcription_translation",
        operation="audit",
        input={"transcript": str(transcript), "baseline": str(baseline)},
        resource_claim={
            "memory_mb": 512,
            "metal_mb": 0,
            "cpu_percent": 100,
            "disk_io": "light",
            "nas_io": "none",
            "network": "none",
            "browser_tokens": 0,
        },
        job_id="remote-model-job",
        priority_class="P2",
        timeout_sec=900,
    )

    try:
        build_worker_spec(job, SimpleNamespace(attempt_number=1, token="lease"))
    except ValueError as exc:
        assert "loopback-only" in str(exc)
    else:
        raise AssertionError("nonlocal model endpoint reached the worker specification")


def test_magi_v2_and_v3_registrations_are_unique_and_bound_to_the_skill() -> None:
    definitions = json.loads((ROOT / "skills" / "definitions.json").read_text(encoding="utf-8"))
    names = [row["name"] for row in definitions["tools"]]
    assert names.count("forensic_transcript_verify") == 1

    capabilities = json.loads((ROOT / "config" / "agent_capabilities.json").read_text(encoding="utf-8"))
    rows = capabilities["capabilities"]
    capability = next(row for row in rows if row["id"] == "transcript.forensic_verify")
    assert capability["tool"] == "skills/forensic-transcript-verifier/action.py:execute"

    manifest = json.loads((ROOT / "config" / "v3_capability_manifest.json").read_text(encoding="utf-8"))
    transcription = next(
        row for row in manifest["capabilities"] if row["id"] == "audio_transcription_translation"
    )
    assert transcription["worker_class"] == "transcription"
    assert "skills/forensic-transcript-verifier/action.py" in transcription["v2_evidence"]
    assert transcription["v3_worker_adapter"]["module"] == "magi_v3.forensic_transcript"
    factory, verifier = load_capability_worker_adapter("audio_transcription_translation")
    assert factory is build_worker_spec
    assert verifier is verify_completion
    assert "autonomous" in build_worker_spec.__globals__["ALLOWED_OPERATIONS"]
