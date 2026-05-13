"""PIIScrubber 測試"""
from __future__ import annotations

from skills.engine.pii_scrubber import PIIScrubber, ScrubResult


class TestRegexScrubbing:
    def test_taiwan_id_masked(self):
        s = PIIScrubber()
        r = s.scrub("當事人身分證字號 A123456789 已核對")
        assert "A123456789" not in r.scrubbed_text
        assert "[ID-001]" in r.scrubbed_text
        assert r.mapping["[ID-001]"] == "A123456789"

    def test_laf_case_masked(self):
        s = PIIScrubber()
        r = s.scrub("法扶案號 1150409-I-004 已受理")
        assert "1150409-I-004" not in r.scrubbed_text
        assert "[LAF-001]" in r.scrubbed_text

    def test_court_case_masked(self):
        s = PIIScrubber()
        r = s.scrub("本案為 114年度原訴字第000024號")
        assert "114年度原訴字第000024號" not in r.scrubbed_text
        assert "[CASE-001]" in r.scrubbed_text

    def test_mobile_masked(self):
        s = PIIScrubber()
        r = s.scrub("聯絡電話 0912-345-678 或 0988666555")
        assert "0912" not in r.scrubbed_text
        assert "0988666555" not in r.scrubbed_text
        assert r.counts["mobile"] == 2


class TestNameScrubbing:
    def test_known_name_masked(self):
        s = PIIScrubber(known_names=["王小明", "張大華"])
        r = s.scrub("王小明控告張大華侵權")
        assert "王小明" not in r.scrubbed_text
        assert "張大華" not in r.scrubbed_text
        assert r.counts["name"] == 2

    def test_unknown_name_not_masked(self):
        s = PIIScrubber(known_names=["王小明"])
        r = s.scrub("陳大文不是當事人")
        assert "陳大文" in r.scrubbed_text  # 非已知姓名，不處理

    def test_name_longer_first(self):
        """歐陽文中 不應被切成 歐陽+文中"""
        s = PIIScrubber(known_names=["歐陽", "歐陽文中"])
        r = s.scrub("歐陽文中出庭")
        # 長名字優先匹配，應該只出現一個佔位符
        assert r.counts["name"] == 1


class TestRestore:
    def test_restore_reverses_scrub(self):
        s = PIIScrubber(known_names=["王小明"])
        original = "王小明的身分證 A123456789 已歸檔"
        r = s.scrub(original)
        assert s.__class__  # guard
        # 模擬 LLM 回覆含佔位符
        llm_reply = f"{list(r.mapping.keys())[0]}的資料已處理"  # 任何含佔位符的句子
        restored = r.restore(llm_reply)
        # 還原後不應再有佔位符
        for ph in r.mapping:
            assert ph not in restored


class TestCounts:
    def test_multiple_scrubs_sum(self):
        s = PIIScrubber(known_names=["王小明"])
        r = s.scrub("王小明A123456789王小明B198765432")  # B198... 也符合 [A-Z][12]\d{8}
        assert r.counts["name"] == 1   # 同名 reuse 同 placeholder
        assert r.counts["id"] == 2     # 兩個不同 ID

    def test_empty_input(self):
        s = PIIScrubber()
        r = s.scrub("")
        assert r.scrubbed_text == ""
        assert all(v == 0 for v in r.counts.values())
