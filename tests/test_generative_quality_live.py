from __future__ import annotations

from types import SimpleNamespace


def test_generative_quality_cli_loads_bound_live_environment(tmp_path, monkeypatch) -> None:
    from api import runtime_paths
    from scripts.ops import generative_quality_live

    env_file = tmp_path / ".env"
    env_file.write_text(
        "NVIDIA_NIM_ENABLE=1\nNVIDIA_NIM_MODEL=nvidia/test-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("NVIDIA_NIM_ENABLE", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_MODEL", raising=False)
    monkeypatch.setattr(runtime_paths, "get_env_file", lambda: env_file)
    monkeypatch.setattr(runtime_paths, "dotenv_override_allowed", lambda: False)

    loaded = generative_quality_live._load_live_environment()

    assert loaded == str(env_file)
    assert __import__("os").environ["NVIDIA_NIM_ENABLE"] == "1"
    assert __import__("os").environ["NVIDIA_NIM_MODEL"] == "nvidia/test-model"


def test_generative_quality_certifies_content_not_just_nonempty_output() -> None:
    from scripts.ops.generative_quality_live import DRAFT_FACTS, run

    draft = """民事起訴狀
案號：115年度訴字第123號
原告：測試原告甲
被告：測試被告乙
案由：損害賠償
聲明事項
一、被告應給付原告新臺幣50,000元。
事實及理由
一、民國115年7月1日，被告駕車不慎致原告受傷，醫療單據為甲證1號。
二、依民法第184條規定，被告應負損害賠償責任。
此致
臺灣臺北地方法院
具狀人：測試原告甲
中華民國115年8月9日
"""
    summary = """## 法律爭點
- 侵權行為損害賠償
## 實務見解
- 按民法第184條規定，侵權行為損害賠償請求權之成立，應以行為人有故意或過失、權利受侵害及相當因果關係為要件。
## 法院涵攝
- 本院認為，被告駕車未注意前方路況，且醫療單據足以證明原告受有損害，二者具有相當因果關係，故原告請求為有理由。
"""
    report = run(
        draft_provider=lambda _prompt: (draft, "nvidia/test"),
        summary_provider=lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            summary=summary,
            model="nvidia/test",
            pii_scrubbed=True,
        ),
        translation_provider=lambda *_args, **_kwargs: {
            "success": True,
            "route": "nvidia_nim",
            "provider": "nvidia",
            "model": "nvidia/test",
            "text": "依臺灣國民法官法，司法通譯應準確翻譯被告陳述。法院命於2026年9月8日前給付新臺幣50,000元。",
        },
    )

    assert report["passed"] is True, report
    assert report["raw_outputs_persisted"] is False
    assert all(item["output_sha256"] for item in report["checks"].values())


def test_generative_quality_rejects_fluent_but_factually_wrong_draft() -> None:
    from scripts.ops.generative_quality_live import certify_draft

    wrong = """民事起訴狀
案號：115年度訴字第123號
原告：測試原告甲
被告：測試被告乙
聲明事項：被告應給付新臺幣99,999元。
事實及理由：依民法第184條請求。
此致
臺灣臺北地方法院
具狀人：測試原告甲
""" + "理由補充。" * 80
    result = certify_draft(lambda _prompt: (wrong, "nvidia/test"))

    assert result["passed"] is False
    assert "ungrounded_amounts" in result["quality_issue_codes"]
    assert result["anchors"]["evidence"] is False


def test_generative_quality_rejects_non_nvidia_translation_mislabeled_as_nim() -> None:
    from scripts.ops.generative_quality_live import certify_translation

    result = certify_translation(
        lambda *_args, **_kwargs: {
            "success": True,
            "route": "nvidia_nim",
            "provider": "melchior_chunk_complete",
            "model": "google_gtx",
            "text": "依臺灣國民法官法，司法通譯應準確翻譯被告陳述。"
            "法院命於2026年9月8日前給付新臺幣50,000元。",
        }
    )

    assert result["passed"] is False
    assert result["nvidia_provenance"] is False


def test_generative_quality_rejects_nvidia_provider_with_local_model() -> None:
    from scripts.ops.generative_quality_live import certify_translation

    result = certify_translation(
        lambda *_args, **_kwargs: {
            "success": True,
            "route": "nvidia_nim",
            "provider": "nvidia",
            "model": "local/gemma-4b",
            "text": "依臺灣國民法官法，司法通譯應準確翻譯被告陳述。"
            "法院命於2026年9月8日前給付新臺幣50,000元。",
        }
    )

    assert result["passed"] is False
    assert result["nvidia_provenance"] is False


def test_generative_quality_rejects_local_draft_mislabeled_by_caller() -> None:
    from scripts.ops.generative_quality_live import certify_draft

    draft = """民事起訴狀
案號：115年度訴字第123號
原告：測試原告甲
被告：測試被告乙
案由：損害賠償
聲明事項：被告應給付原告新臺幣50,000元。
事實及理由：民國115年7月1日，被告駕車不慎致原告受傷，醫療單據為甲證1號；依民法第184條請求。
此致
臺灣臺北地方法院
具狀人：測試原告甲
中華民國115年8月9日
"""
    result = certify_draft(lambda _prompt: (draft, "local/gemma-4b"))

    assert result["passed"] is False
    assert result["nvidia_provenance"] is False


def test_generative_quality_rejects_summary_without_verified_pii_scrub() -> None:
    from scripts.ops.generative_quality_live import certify_summary

    summary = """## 法律爭點
- 侵權行為損害賠償
## 實務見解
- 按民法第184條規定，成立損害賠償須具備故意或過失、權利受侵害及相當因果關係。
## 法院涵攝
- 本院認為，被告未注意前方路況，醫療單據證明損害，故請求有理由。
"""
    result = certify_summary(
        lambda *_args, **_kwargs: SimpleNamespace(
            success=True,
            summary=summary,
            model="nvidia/nemotron",
            pii_scrubbed=False,
        )
    )

    assert result["passed"] is False
    assert result["pii_scrubbed"] is False
