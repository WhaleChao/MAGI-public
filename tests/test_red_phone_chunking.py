import json

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
