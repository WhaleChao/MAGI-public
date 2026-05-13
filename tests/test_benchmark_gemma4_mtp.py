from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_benchmark_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_gemma4_mtp.py"
    spec = importlib.util.spec_from_file_location("benchmark_gemma4_mtp", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_tasks_and_build_mtp_payload(tmp_path):
    mod = _load_benchmark_module()
    task_path = tmp_path / "tasks.jsonl"
    task_path.write_text(
        json.dumps(
            {
                "name": "json_case",
                "category": "json_extract",
                "expect_json": True,
                "messages": [{"role": "user", "content": "請輸出 JSON"}],
                "max_tokens": 123,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    tasks = mod.load_tasks(task_path)
    payload = mod.build_payload(tasks[0], model="gemma-4-e4b-it-4bit", draft_model="e4b-draft")

    assert len(tasks) == 1
    assert tasks[0].expect_json is True
    assert payload["model"] == "gemma-4-e4b-it-4bit"
    assert payload["draft_model"] == "e4b-draft"
    assert payload["draft_kind"] == "mtp"
    assert payload["draft_block_size"] >= 1


def test_json_validation_and_summary():
    mod = _load_benchmark_module()

    assert mod.valid_json_text('{"ok": true}') is True
    assert mod.valid_json_text("不是 JSON") is False

    summary = mod.summarize(
        [
            {"ok": True, "elapsed_sec": 1.0, "tokens_per_sec": 20, "expect_json": True, "json_valid": True},
            {"ok": False, "elapsed_sec": 2.0, "tokens_per_sec": 0, "expect_json": True, "json_valid": False},
        ]
    )

    assert summary["total"] == 2
    assert summary["ok"] == 1
    assert summary["failed"] == 1
    assert summary["json_success"] == 1
    assert summary["json_total"] == 2
