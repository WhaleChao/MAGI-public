from __future__ import annotations

import io

from flask import Flask


def _make_app() -> Flask:
    app = Flask(__name__, template_folder="../templates")
    app.config.update(TESTING=True, SECRET_KEY="test-secret")
    from api.blueprints.lottery import lottery_bp

    app.register_blueprint(lottery_bp)
    return app


def test_lottery_page_is_public():
    client = _make_app().test_client()

    response = client.get("/lottery")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<title>抽獎</title>" in page
    assert "<h1>抽獎</h1>" in page


def test_lottery_csv_draw_masks_sensitive_fields():
    from api.blueprints.lottery import draw_lottery

    csv_text = "姓名,電話,地址\n王小明,5551234,花蓮縣花蓮市中山路1號\n李大華,5559876,臺北市大安區和平東路2號\n"

    result = draw_lottery("名單.csv", csv_text.encode("utf-8-sig"), 1)

    assert result["ok"] is True
    assert result["total_rows"] == 2
    assert result["winner_count"] == 1
    winner = result["winners"][0]
    assert "小明" not in str(winner)
    assert "5551234" not in str(winner)
    assert "中山路1號" not in str(winner)
    assert "○" in winner["name"]
    assert "○" in winner["phone"]
    assert winner["address"].endswith("○○○")


def test_lottery_xlsx_draw_count_can_be_specified():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["姓名", "手機", "收件地址"])
    ws.append(["王小明", "5551234", "花蓮縣花蓮市中山路1號"])
    ws.append(["李大華", "5552345", "臺北市大安區和平東路2號"])
    ws.append(["陳美玉", "5553456", "臺中市西區民生路3號"])
    buf = io.BytesIO()
    wb.save(buf)

    client = _make_app().test_client()
    response = client.post(
        "/api/lottery/draw",
        data={
            "winner_count": "2",
            "file": (io.BytesIO(buf.getvalue()), "名單.xlsx"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["total_rows"] == 3
    assert body["winner_count"] == 2
    assert len(body["winners"]) == 2
    assert len({w["row_number"] for w in body["winners"]}) == 2


def test_lottery_rejects_too_many_winners():
    client = _make_app().test_client()
    csv_text = "姓名,電話,地址\n王小明,5551234,花蓮縣花蓮市中山路1號\n"

    response = client.post(
        "/api/lottery/draw",
        data={
            "winner_count": "2",
            "file": (io.BytesIO(csv_text.encode("utf-8-sig")), "名單.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "不可超過有效資料筆數" in response.get_json()["message"]
