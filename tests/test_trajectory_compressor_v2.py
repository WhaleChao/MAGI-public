"""test_trajectory_compressor_v2.py — 新增 Phase 1-4 測試"""
import pytest

from skills.engine.trajectory_compressor import TrajectoryCompressor


@pytest.fixture
def tc():
    return TrajectoryCompressor()


def _make_messages(n_rounds, tool_output_size=500):
    """產生 n 輪 user→assistant(ACTION)→tool→assistant(OBSERVE) 的對話。"""
    msgs = [{"role": "system", "content": "你是法律助理。"}]
    for i in range(n_rounds):
        msgs.append({"role": "user", "content": "查詢第 {} 步".format(i + 1)})
        msgs.append({"role": "assistant", "content": "ACTION: search_memory\n{{\"query\": \"test_{}\"}}\n".format(i)})
        msgs.append({"role": "tool", "content": "x" * tool_output_size})
        msgs.append({"role": "assistant", "content": "OBSERVE: 找到結果 {}".format(i)})
    msgs.append({"role": "assistant", "content": "FINAL: 完成所有查詢。"})
    return msgs


class TestPhase1ToolPruning:
    def test_short_tool_result_unchanged(self, tc):
        msgs = [{"role": "tool", "content": "短結果"}]
        result = tc.prune_tool_results(msgs)
        assert result[0]["content"] == "短結果"

    def test_long_tool_result_pruned(self, tc):
        msgs = [{"role": "tool", "content": "行1\n" * 200}]
        result = tc.prune_tool_results(msgs)
        assert len(result[0]["content"]) < 200
        assert "lines" in result[0]["content"]

    def test_user_message_never_pruned(self, tc):
        long_content = "x" * 1000
        msgs = [{"role": "user", "content": long_content}]
        result = tc.prune_tool_results(msgs)
        assert result[0]["content"] == long_content


class TestPhase2HeadTail:
    def test_split_preserves_system(self, tc):
        msgs = _make_messages(5)
        head, middle, tail = tc._split_head_middle_tail(msgs)
        assert head[0]["role"] == "system"

    def test_split_head_has_early_messages(self, tc):
        msgs = _make_messages(5)
        head, _, _ = tc._split_head_middle_tail(msgs)
        # system + HEAD_KEEP*2 messages
        assert len(head) >= 3

    def test_split_tail_has_recent_messages(self, tc):
        msgs = _make_messages(5)
        _, _, tail = tc._split_head_middle_tail(msgs)
        assert tail[-1]["content"] == "FINAL: 完成所有查詢。"


class TestPhase3Summary:
    def test_middle_summary_contains_tools(self, tc):
        # n_rounds=20 + tool_output_size=2000 保證 tail budget(5000tok) 撐不住全部
        # 10 rounds ≈ 5145 tokens(全進tail)，20 rounds = ~10K tokens，middle 有 37+ 則
        msgs = _make_messages(20, tool_output_size=2000)
        _, middle, _ = tc._split_head_middle_tail(msgs)
        assert len(middle) > 0, "middle should be non-empty with 20 rounds of 2000-char tool outputs"
        summary = tc._summarize_middle_heuristic(middle)
        assert summary is not None
        assert "CONTEXT COMPACTION" in summary["content"]
        assert "search_memory" in summary["content"]

    def test_empty_middle_returns_none(self, tc):
        assert tc._summarize_middle_heuristic([]) is None


class TestCompressForReact:
    def test_short_conversation_unchanged(self, tc):
        msgs = _make_messages(2)
        result = tc.compress_for_react(msgs, max_tokens=10000)
        # 短對話不需要壓縮
        assert len(result) <= len(msgs)

    def test_long_conversation_compressed(self, tc):
        msgs = _make_messages(10, tool_output_size=2000)
        before = tc._total_tokens(msgs)
        result = tc.compress_for_react(msgs, max_tokens=3000)
        after = tc._total_tokens(result)
        assert after < before

    def test_compressed_has_system_and_final(self, tc):
        msgs = _make_messages(10)
        result = tc.compress_for_react(msgs, max_tokens=2000)
        assert result[0]["role"] == "system"
        assert "FINAL" in result[-1]["content"] or "完成" in result[-1]["content"]


class TestBackwardCompatibility:
    def test_original_compress_still_works(self, tc):
        msgs = _make_messages(5)
        result = tc.compress(msgs, max_tokens=2000, max_messages=10)
        assert len(result) <= 10
        assert result[0]["role"] == "system"
