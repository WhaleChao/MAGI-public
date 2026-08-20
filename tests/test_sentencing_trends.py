from __future__ import annotations

import sys
import types

from flask import Flask
from flask_login import LoginManager, UserMixin

from api.sentencing_trends import parse_sentencing_judgment, search_sentencing_trends


def _row(full_text: str, *, jid: str = "CYDM,115,嘉交簡,613,20260731,1"):
    return {
        "id": 1,
        "jid": jid,
        "court_name": "CYDM",
        "case_number": "115年度嘉交簡字第613號",
        "case_type": "公共危險",
        "judgment_date": "2026-07-31",
        "full_text": full_text,
        "source_url": "https://data.judicial.gov.tw/opendl/JDocFile/CYDM/115%2c嘉交簡%2c613%2c20260731%2c1.pdf",
    }


def test_parser_uses_signature_judge_and_separates_execution_sentence():
    text = """
臺灣嘉義地方法院刑事判決
主 文
甲犯公共危險罪，處有期徒刑參月。又犯公共危險罪，處有期徒刑陸月。應執行有期徒刑柒月。
事實及理由
本院參酌乙法官曾經審理其他案件，此處不得當作簽署。
中華民國115年7月31日
嘉義簡易庭 法 官 蘇珈漪
以上正本證明與原本無異
"""
    item = parse_sentencing_judgment(_row(text))
    assert item["statistics_eligible"] is True
    assert item["judges"] == [{"name": "蘇珈漪", "role": "獨任法官"}]
    assert item["participating_judges"] == ["蘇珈漪"]
    assert item["last_listed_judge"] == "蘇珈漪"
    assert item["judgment_date"] == "2026-07-31"
    assert item["judgment_date_display"] == "民國115年7月31日"
    assert [sentence["months"] for sentence in item["sentences"]] == [3.0, 6.0]
    assert item["execution_sentence"]["months"] == 7.0


def test_panel_judges_keep_participants_and_highlight_last_listed_judge():
    text = """
臺灣高等法院刑事判決
主文
甲犯詐欺罪，處有期徒刑陸月。
理由
略。
中華民國115年7月31日
審判長 法 官 王大明
        法 官 李小華
        法 官 陳志平
"""
    item = parse_sentencing_judgment(_row(text))
    assert item["participating_judges"] == ["王大明", "李小華", "陳志平"]
    assert item["last_listed_judge"] == "陳志平"
    assert item["statistics_eligible"] is True


def test_judge_filter_defaults_to_last_listed_but_can_search_any_participant():
    text = """
臺灣嘉義地方法院刑事判決
主文
甲犯公共危險罪，處有期徒刑陸月。
理由
略。
中華民國115年7月31日
審判長 法 官 王大明
        法 官 李小華
        法 官 陳志平
"""
    common = {
        "court": "臺灣嘉義地方法院",
        "offense": "公共危險",
        "connector": lambda: (_Connection([_row(text)]), {}),
        "include_mcp": False,
    }
    assert search_sentencing_trends(judge="王大明", **common)["eligible_count"] == 0
    assert search_sentencing_trends(judge="陳志平", **common)["eligible_count"] == 1
    assert search_sentencing_trends(judge="王大明", judge_scope="participating", **common)["eligible_count"] == 1


def test_parser_includes_appendix_sentences_but_excludes_missing_appendix():
    complete = """
臺灣嘉義地方法院刑事裁定
主文
如附表所示各罪，應執行有期徒刑壹年伍月。
理由
略。
中華民國115年7月31日
刑事第四庭 法官 蘇珈漪
【附表】
編號1 宣告刑：處有期徒刑拾月。
編號2 宣告刑：處有期徒刑參月。
"""
    item = parse_sentencing_judgment(_row(complete))
    assert item["appendix_complete"] is True
    assert [sentence["months"] for sentence in item["appendix_sentences"]] == [10.0, 3.0]
    assert item["execution_sentence"]["months"] == 17.0
    missing = complete.split("【附表】", 1)[0]
    missing_item = parse_sentencing_judgment(_row(missing))
    assert missing_item["appendix_complete"] is False
    assert missing_item["statistics_eligible"] is False
    assert "附表" in missing_item["exclusion_reason"]


def test_duration_parser_does_not_absorb_a_later_roc_year():
    text = """
臺灣嘉義地方法院刑事判決
主文
處有期徒刑5月確定，於民國110年5月16日入監。
理由
略。
中華民國115年7月31日
法官 蘇珈漪
"""
    item = parse_sentencing_judgment(_row(text))
    assert item["sentences"][0]["months"] == 5.0


def test_parser_accepts_official_mcp_flattened_section_format():
    text = (
        "臺灣嘉義地方法院刑事簡易判決115年度嘉交簡字第433號"
        "上列被告因公共危險案件，本院判決如下：　　主　文"
        "甲犯公共危險罪，處有期徒刑肆月，如易科罰金，以新臺幣壹仟元折算壹日。"
        "　　事實及理由一、本件犯罪事實及證據均詳附件。"
        "中華民國115年5月29日　嘉義簡易庭　法　官　蘇珈漪"
        "以上正本證明與原本無異。"
    )
    item = parse_sentencing_judgment(_row(text))
    assert item["statistics_eligible"] is True
    assert item["judges"] == [{"name": "蘇珈漪", "role": "獨任法官"}]
    assert [sentence["months"] for sentence in item["sentences"]] == [4.0]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, _params):
        return None

    def fetchall(self):
        return self.rows

    def close(self):
        return None


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, dictionary=True):
        assert dictionary is True
        return _Cursor(self.rows)

    def close(self):
        return None


def test_search_keeps_mcp_candidates_out_of_statistics():
    text = """
臺灣嘉義地方法院刑事判決
主文
處有期徒刑陸月。
理由
略。
中華民國115年7月31日
法官 蘇珈漪
"""
    result = search_sentencing_trends(
        court="臺灣嘉義地方法院",
        judge="蘇珈漪",
        offense="公共危險",
        connector=lambda: (_Connection([_row(text)]), {}),
        mcp_search=lambda *_args, **_kwargs: {"success": True, "items": [{"title": "尚未核對的 MCP 候選"}]},
    )
    assert result["eligible_count"] == 1
    assert result["statistics"]["declared_terms"]["count"] == 1
    assert result["mcp"]["included_in_statistics"] is False
    assert len(result["mcp"]["items"]) == 1


def test_search_uses_mcp_fulltext_after_independent_official_verification():
    local_text = """
臺灣嘉義地方法院刑事判決
主文
處有期徒刑陸月。
理由
略。
中華民國115年7月31日
法官 蘇珈漪
"""
    remote_text = """
臺灣嘉義地方法院刑事判決
主文
乙犯公共危險罪，處有期徒刑肆月。
理由
略。
中華民國115年6月30日
法官 蘇珈漪
"""
    captured = {}

    def mcp_search(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "source": "legaltech_taiwan_law_mcp",
            "items": [
                {
                    "jid": "CYDM,115,嘉交簡,500,20260630,1",
                    "title": "臺灣嘉義地方法院115年度嘉交簡字第500號刑事判決",
                    "court": "臺灣嘉義地方法院",
                    "case_reason": "公共危險",
                    "judgment_date": "2026-06-30",
                    "full_text": remote_text,
                    "source_url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?id=CYDM115500",
                    "official_origin": True,
                }
            ],
        }

    result = search_sentencing_trends(
        court="臺灣嘉義地方法院",
        judge="蘇珈漪",
        offense="公共危險",
        connector=lambda: (_Connection([_row(local_text)]), {}),
        mcp_search=mcp_search,
    )
    assert captured["court"] == "臺灣嘉義地方法院"
    assert captured["limit"] == 10
    assert captured["fulltext_limit"] == 10
    assert result["local_eligible_count"] == 1
    assert result["mcp_verified_count"] == 1
    assert result["eligible_count"] == 2
    assert result["statistics"]["declared_terms"] == {
        "count": 2,
        "median_months": 5.0,
        "q1_months": 4.5,
        "q3_months": 5.5,
        "min_months": 4.0,
        "max_months": 6.0,
    }
    assert result["mcp"]["included_in_statistics"] is True
    assert result["items"][-1]["external_verified"] is True


def test_mcp_fulltext_from_non_official_url_never_enters_statistics():
    remote_text = """
臺灣嘉義地方法院刑事判決
主文
處有期徒刑肆月。
理由
略。
中華民國115年6月30日
法官 蘇珈漪
"""
    result = search_sentencing_trends(
        court="臺灣嘉義地方法院",
        judge="蘇珈漪",
        offense="公共危險",
        connector=lambda: (_Connection([]), {}),
        mcp_search=lambda *_args, **_kwargs: {
            "success": True,
            "items": [
                {
                    "jid": "untrusted",
                    "court": "臺灣嘉義地方法院",
                    "case_reason": "公共危險",
                    "judgment_date": "2026-06-30",
                    "full_text": remote_text,
                    "source_url": "https://example.invalid/fake",
                    "official_origin": True,
                }
            ],
        },
    )
    assert result["mcp_verified_count"] == 0


def test_mcp_candidate_verification_budget_keeps_ten_official_candidates():
    result = search_sentencing_trends(
        court="臺灣嘉義地方法院",
        judge="蘇珈漪",
        offense="公共危險",
        connector=lambda: (_Connection([]), {}),
        mcp_search=lambda *_args, **_kwargs: {
            "success": True,
            "items": [
                {
                    "jid": f"CYDM,115,嘉交簡,{number},20260630,1",
                    "court": "臺灣嘉義地方法院",
                    "case_reason": "公共危險",
                    "judgment_date": "2026-06-30",
                    "full_text": "",
                    "source_url": f"https://judgment.judicial.gov.tw/FJUD/data.aspx?id={number}",
                    "official_origin": True,
                }
                for number in range(1, 13)
            ],
        },
    )
    assert len(result["mcp"]["items"]) == 10
    assert result["eligible_count"] == 0
    assert result["mcp"]["included_in_statistics"] is False


def test_external_mcp_receives_only_privacy_approved_query(monkeypatch):
    from api.blueprints import sentencing_trends as blueprint

    captured = {}
    fake = types.ModuleType("api.osc.legaltech_taiwan_law_mcp")

    def search(query, **kwargs):
        captured.update({"query": query, **kwargs})
        return {"success": True, "source": "legaltech", "items": []}

    fake.search_practical_judgments_via_legaltech = search
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    result = blueprint._search_public_judgment_candidates(
        "臺灣嘉義地方法院 蘇珈漪 公共危險 量刑",
        case_type="刑事",
        limit=5,
        fulltext_limit=1,
    )
    assert result["success"] is True
    assert captured == {
        "query": "臺灣嘉義地方法院 蘇珈漪 公共危險 量刑",
        "case_type": "刑事",
        "limit": 5,
        "fulltext_limit": 1,
    }


class _User(UserMixin):
    id = "tester"


def _app(monkeypatch):
    from api.blueprints import sentencing_trends as blueprint

    app = Flask(__name__, template_folder="../templates")
    app.config.update(TESTING=True, SECRET_KEY="test", LOGIN_DISABLED=False)
    manager = LoginManager(app)
    manager.login_view = "login"

    @manager.request_loader
    def load_user(request):
        return _User() if request.headers.get("X-User-ID") else None

    @app.get("/login")
    def login():
        return "login"

    monkeypatch.setattr(blueprint, "search_sentencing_trends", lambda **_kwargs: {"ok": True, "items": [], "mcp": {"items": []}})
    app.register_blueprint(blueprint.sentencing_trends_bp)
    return app


def test_sentencing_routes_require_login_and_disable_cache(monkeypatch):
    client = _app(monkeypatch).test_client()
    assert client.get("/sentencing-trends").status_code == 302
    page = client.get("/sentencing-trends", headers={"X-User-ID": "u1"})
    assert page.status_code == 200
    assert "法官量刑與判決趨勢" in page.get_data(as_text=True)
    response = client.get("/api/sentencing-trends/search?judge=蘇珈漪", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"


def test_sentencing_api_items_keep_iso_canonical_date_and_add_roc_display_date(monkeypatch):
    from api.blueprints import sentencing_trends as blueprint

    app = _app(monkeypatch)
    monkeypatch.setattr(
        blueprint,
        "search_sentencing_trends",
        lambda **_kwargs: {
            "ok": True,
            "items": [{"judgment_date": "2026-08-17", "judgment_date_display": "民國115年8月17日"}],
            "mcp": {"items": []},
        },
    )
    response = app.test_client().get("/api/sentencing-trends/search?judge=蘇珈漪", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    item = response.get_json()["items"][0]
    assert item["judgment_date"] == "2026-08-17"
    assert item["judgment_date_display"] == "民國115年8月17日"


def test_sentencing_page_prefers_server_roc_display_and_has_iso_fallback_for_mobile_layout():
    page = open("templates/sentencing_trends.html", encoding="utf-8").read()
    assert "function rocDate(value)" in page
    assert "item.judgment_date_display||rocDate(item.judgment_date)||'未載日期'" in page


def test_sentencing_page_uses_roc_date_inputs_without_browser_english_placeholders():
    page = open("templates/sentencing_trends.html", encoding="utf-8").read()
    assert 'type="date"' not in page
    assert "判決日起（民國）" in page
    assert "判決日迄（民國）" in page
    assert page.count("例如：民國115年8月17日") == 2
    assert "function rocInputToIso(value)" in page
    assert 'type="hidden" name="date_from"' in page
    assert 'type="hidden" name="date_to"' in page


def test_sentencing_api_never_exposes_internal_exception(monkeypatch):
    from api.blueprints import sentencing_trends as blueprint

    app = _app(monkeypatch)
    monkeypatch.setattr(blueprint, "search_sentencing_trends", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("/private/path trace-123")))
    response = app.test_client().get("/api/sentencing-trends/search?judge=蘇珈漪", headers={"X-User-ID": "u1"})
    assert response.status_code == 503
    assert "/private/path" not in response.get_data(as_text=True)
    assert "trace-123" not in response.get_data(as_text=True)
