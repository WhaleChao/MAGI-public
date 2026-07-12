from skills.bridge.intention_classifier import IntentionClassifier


def test_intent_classifier_uses_normalized_heavy_and_control_intents(tmp_path, monkeypatch):
    from skills.bridge import intention_classifier as ic

    cache_path = tmp_path / "intent_classifier_cache.json"
    monkeypatch.setattr(ic, "_CACHE_PERSIST_PATH", str(cache_path))

    clf = IntentionClassifier(use_llm=False, cache_size=8)

    assert clf.classify("@HEAVY 請幫我比較民法184條與相關判決見解") == "QUERY"
    assert clf.classify("@重型：我只是想跟你聊聊天") == "CHAT"
    assert clf.classify("取消") == "CMD"
    assert clf.classify("更正：正確是臺灣新北地方法院") == "CHAT"
