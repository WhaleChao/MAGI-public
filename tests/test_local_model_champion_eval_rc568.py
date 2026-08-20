from __future__ import annotations

import json

from scripts.ops.local_model_champion_eval import main, score


def _rows(passed: bool = True):
    return [{"area": area, "passed": passed} for area in ("zh_legal", "tool_json", "refusal", "latency_memory", "crash")]


def test_score_requires_all_contract_areas():
    assert score(_rows()) ["ok"] is True
    assert score(_rows()[:-1])["ok"] is False


def test_runner_is_offline_and_requires_challenger_gate(tmp_path, capsys):
    champion, challenger = tmp_path / "26b.jsonl", tmp_path / "20b.jsonl"
    champion.write_text("\n".join(json.dumps(row) for row in _rows()), encoding="utf-8")
    challenger.write_text("\n".join(json.dumps(row) for row in _rows()), encoding="utf-8")
    assert main(["--champion", str(champion), "--challenger", str(challenger)]) == 0
    assert json.loads(capsys.readouterr().out)["offline_only"] is True


def test_latency_memory_and_crash_thresholds_block_promotion():
    rows = _rows()
    rows[3].update({"p95_latency_sec": 91, "peak_memory_gb": 20})
    rows[4]["crash_count"] = 1
    assert score(rows)["ok"] is False
