import json
from io import BytesIO
from urllib.error import HTTPError

from skills.ops import red_phone


def test_notification_chunks_keep_all_content():
    text = "開頭\n" + ("完整內容" * 900) + "\n結尾"
    chunks = red_phone._numbered_chunks(text, 900)
    joined = "\n".join(
        chunk.split("\n", 1)[1] if chunk.startswith("(") and "\n" in chunk else chunk
        for chunk in chunks
    )
    assert len(chunks) > 1
    assert "開頭" in joined
    assert "結尾" in joined
    assert "完整內容" * 10 in joined
    assert all(len(chunk) <= 900 for chunk in chunks)


def test_telegram_send_splits_long_messages(monkeypatch):
    sent_payloads = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=0):
        sent_payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(red_phone.urlrequest, "urlopen", fake_urlopen)

    long_message = "A" * 8100 + "最後"
    result = red_phone._send_telegram_once("token", ["123"], long_message, timeout_sec=4)

    assert result["ok_any"] is True
    assert len(sent_payloads) >= 3
    assert sent_payloads[0]["text"].startswith("(1/")
    assert sent_payloads[-1]["text"].endswith("最後")


def test_telegram_send_includes_thread_id(monkeypatch):
    sent_payloads = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=0):
        sent_payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(red_phone.urlrequest, "urlopen", fake_urlopen)

    result = red_phone._send_telegram_once("token", ["-1001"], "分層測試", thread_id=42)

    assert result["ok_any"] is True
    assert sent_payloads == [{"chat_id": "-1001", "text": "分層測試", "message_thread_id": 42}]


def test_generic_laf_topic_refines_dispatch_message(monkeypatch):
    monkeypatch.setattr(
        red_phone,
        "_load_topic_map",
        lambda: {"laf": 2814, "laf_dispatch": 2816},
    )

    topic, thread_id = red_phone._resolve_thread_id(
        "📧 法扶派案通知\n分會: 花蓮\n當事人: 王惠薰\n法扶案號: 1150529-E-005",
        "laf_notifier",
        "info",
        topic_key="laf",
    )

    assert topic == "laf_dispatch"
    assert thread_id == 2816


def test_invalid_telegram_thread_does_not_fallback_to_general(monkeypatch):
    sent_payloads = []

    def fake_urlopen(req, timeout=0):
        sent_payloads.append(json.loads(req.data.decode("utf-8")))
        body = BytesIO(b'{"description":"Bad Request: message thread not found"}')
        raise HTTPError(
            req.full_url,
            400,
            "Bad Request",
            hdrs=None,
            fp=body,
        )

    monkeypatch.setattr(red_phone.urlrequest, "urlopen", fake_urlopen)

    result = red_phone._send_telegram_once("token", ["-1001"], "分層測試", thread_id=42)

    assert result["ok_any"] is False
    assert "invalid_thread:42" in result["error"]
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["message_thread_id"] == 42


def test_ensure_telegram_forum_topics_creates_and_persists_map(tmp_path, monkeypatch):
    topic_file = tmp_path / "telegram_topic_map.json"
    state_file = tmp_path / "telegram_channel_state.json"
    monkeypatch.setattr(red_phone, "RED_PHONE_TOPIC_MAP_FILE", str(topic_file))
    monkeypatch.setattr(red_phone, "TELEGRAM_CHANNEL_STATE_FILE", str(state_file))
    monkeypatch.setattr(red_phone, "_get_telegram_config", lambda: ("token", ["-1001"]))

    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if req.full_url.endswith("/getChat?chat_id=-1001"):
            return FakeResponse({"ok": True, "result": {"id": -1001, "title": "MAGI", "is_forum": True}})
        body = json.loads(req.data.decode("utf-8"))
        title = body["name"]
        return FakeResponse({"ok": True, "result": {"message_thread_id": 100 + len(title)}})

    monkeypatch.setattr(red_phone.urlrequest, "urlopen", fake_urlopen)

    result = red_phone.ensure_telegram_forum_topics(
        topic_names={"filereview_payment": "閱卷繳費", "quiet_cron": "MAGI 巡檢"},
    )

    assert result["ok"] is True
    saved = json.loads(topic_file.read_text(encoding="utf-8"))
    assert set(saved) == {"check", "filereview_payment"}
    assert saved["filereview_payment"] > 0
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["chat_id"] == "-1001"
    assert state["topicMap"] == saved
