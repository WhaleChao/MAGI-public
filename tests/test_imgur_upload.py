from __future__ import annotations

from pathlib import Path

import requests


CLIENT_ID = "d3101526084ac92"
IMGUR_API_URL = "https://api.imgur.com/3/image"


def upload_to_imgur(image_path: Path):
    headers = {"Authorization": f"Client-ID {CLIENT_ID}"}
    with image_path.open("rb") as file:
        files = {"image": file}
        return requests.post(IMGUR_API_URL, headers=headers, files=files, timeout=30)


def test_upload_uses_imgur_api_contract_without_network(monkeypatch, tmp_path):
    image_path = tmp_path / "test_image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nmock")
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"link": "https://imgur.example/mock.png"}}

    def fake_post(url, *, headers, files, timeout):
        calls.append((url, headers, files["image"].read(), timeout))
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)

    response = upload_to_imgur(image_path)

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [
        (
            IMGUR_API_URL,
            {"Authorization": f"Client-ID {CLIENT_ID}"},
            b"\x89PNG\r\n\x1a\nmock",
            30,
        )
    ]
