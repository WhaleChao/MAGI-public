"""Three-sample proposals for bounded product schedule adapters.

This module deliberately does not modify or load the authoritative schedule
registry.  It prepares semantically distinct fixture inputs that can be wired
into the registry once concurrent registry work has been merged.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


JOBS = (
    "job_business_module_live_check",
    "job_heavy_translation_quality_live",
    "job_distill_train_gemma",
    "job_insight_sync",
    "job_reprocess_insights",
)


def _common_contract(job_id: str, checks: tuple[str, ...]) -> dict[str, Any]:
    equals: dict[str, Any] = {
        "schema": "magi.schedule-product-result/v1",
        "job_id": job_id,
        "success": True,
        "status": "passed",
        "safety.external_network_accessed": False,
        "safety.production_database_accessed": False,
        "safety.production_state_written": False,
        "safety.nas_accessed": False,
        "safety.writes_bounded_to_fixture": True,
    }
    equals.update({f"checks.{name}": True for name in checks})
    return {
        "type": "json_file",
        "path": "outputs/result.json",
        "equals": equals,
        "minimum": {"fixture_sample_id": 1},
        "lengths": {},
    }


_ADAPTERS: dict[str, dict[str, Any]] = {
    "job_business_module_live_check": {
        "job_id": "job_business_module_live_check",
        "production_entrypoint": "scripts/ops/business_module_live_check.py",
        "safety_class": "bounded_business_true_orchestration_with_terminal_providers",
        "fixture_kind": "product_business_module_live",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/ops/business_module_live_check.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": {
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
            "MAGI_RUNTIME_DIR": "<FIXTURE>/workspace/runtime",
        },
        "success_contract": {"type": "business_module_true_probes"},
    },
    "job_heavy_translation_quality_live": {
        "job_id": "job_heavy_translation_quality_live",
        "production_entrypoint": "scripts/ops/heavy_translation_quality_live.py",
        "safety_class": "bounded_translation_provider_route_docx_and_terminal_close",
        "fixture_kind": "product_heavy_translation_quality",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/ops/heavy_translation_quality_live.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": {
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
            "MAGI_RUNTIME_DIR": "<FIXTURE>/workspace/runtime",
            "MAGI_EXPORTS_DIR": "<FIXTURE>/workspace/exports",
            "TMPDIR": "<FIXTURE>/workspace",
        },
        "success_contract": {"type": "heavy_translation_provider_terminal"},
    },
    "job_distill_train_gemma": {
        "job_id": "job_distill_train_gemma",
        "production_entrypoint": "scripts/nightly_distill_gemma.py",
        "safety_class": "bounded_distill_training_optimizer_checkpoint_eval_no_deploy",
        "fixture_kind": "product_distill_train_gemma",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/nightly_distill_gemma.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": {
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
            "GEMMA_DISTILL_DIR": "<FIXTURE>/workspace/gemma-distill",
            "MAGI_ROOT_DIR": "<ROOT>",
        },
        "success_contract": {"type": "distill_training_terminal"},
    },
    "job_insight_sync": {
        "job_id": "job_insight_sync",
        "production_entrypoint": "scripts/sync_insights_to_vectors.py",
        "safety_class": "bounded_formal_embedding_and_disposable_vector_database_terminal",
        "fixture_kind": "product_insight_sync",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/sync_insights_to_vectors.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": {
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
            "MAGI_AGENT_DIR": "<FIXTURE>/workspace/agent",
        },
        "success_contract": {"type": "insight_sync_embedding_database_terminal"},
    },
    "job_reprocess_insights": {
        "job_id": "job_reprocess_insights",
        "production_entrypoint": "scripts/reprocess_insights.py",
        "safety_class": "bounded_formal_api_model_and_disposable_database_update_terminal",
        "fixture_kind": "product_reprocess_insights",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/reprocess_insights.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": {
            "MAGI_V3_SCHEDULE_FIXTURE": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
            "MAGI_RUNTIME_DIR": "<FIXTURE>/workspace/runtime",
        },
        "success_contract": {"type": "reprocess_insights_api_model_database_terminal"},
    },
}


def adapter_proposals() -> list[dict[str, Any]]:
    return [copy.deepcopy(_ADAPTERS[job_id]) for job_id in JOBS]


def _business_input(sample_id: int) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "business_provider": "business-provider.json",
        "laf_provider": "laf-provider.json",
        "file_review_provider": "file-review-provider.json",
        "transcript_config": "transcript-config.json",
        "expected_drive_matches": 10 + sample_id,
        "expected_calendar_imported": sample_id,
    }


def _distill_row(token: str, *, rejected: bool = False) -> dict[str, Any]:
    prompt = "THE 7-STEP REASONING CHAIN" if rejected else f"請摘要判決 {token}。"
    response = (
        "## 裁判要旨\n"
        f"法院認為損害賠償請求 {token} 應證明權利受侵害、損害及相當因果關係。\n"
        "## 法院見解\n"
        "法院依卷內證據與民法規定審酌雙方主張，並說明舉證責任之分配；"
        "請求人仍應具體證明損害範圍與可歸責事由，始得認定其請求有理由。"
    )
    return {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ],
        "metadata": {
            "source": "nim_resummary",
            "content_hash": f"sha256:{token}",
        },
    }


def _insight(
    row_id: int, case_number: str, insight_text: str
) -> dict[str, Any]:
    return {
        "id": row_id,
        "case_number": case_number,
        "case_reason": "損害賠償",
        "court_reference": f"最高法院{case_number}",
        "insight_type": "法院見解",
        "document_name": "fixture.pdf",
        "insight_text": insight_text,
    }


def _insight_content(row: Mapping[str, Any]) -> str:
    return (
        f"案號：{row['case_number']}\n"
        f"案由：{row['case_reason']}\n"
        f"裁判字號：{row['court_reference']}\n"
        f"類型：{row['insight_type']}\n"
        f"文件：{row['document_name']}\n\n"
        f"{row['insight_text']}"
    )


def _insight_input(sample_id: int) -> dict[str, Any]:
    if sample_id == 1:
        new = _insight(1, "115年度台上字第1號", "法院認為相當因果關係應由請求人證明。")
        duplicate = {**new, "id": 2}
        existing = _insight(3, "115年度台上字第3號", "既有見解。")
        return {
            "sample_id": sample_id,
            "insights": [new, duplicate, existing],
            "existing_contents": [_insight_content(existing)],
            "expected_new_ids": [1],
        }
    if sample_id == 2:
        first = _insight(10, "115年度台上字第10號", "法院認為契約解除後應回復原狀。")
        second = _insight(11, "115年度台上字第11號", "法院認為舉證責任應依主張分配。")
        return {
            "sample_id": sample_id,
            "insights": [first, second],
            "existing_contents": [],
            "expected_new_ids": [10, 11],
        }
    existing = _insight(20, "115年度台上字第20號", "已存在之裁判見解。")
    new = _insight(21, "115年度台上字第21號", "法院認為損害範圍必須具體證明。")
    return {
        "sample_id": sample_id,
        "insights": [existing, new],
        "existing_contents": [_insight_content(existing)],
        "expected_new_ids": [21],
    }


def _reprocess_input(sample_id: int) -> dict[str, Any]:
    raw = "法院認為應依民法規定審酌損害、因果關係及舉證責任。" * 5
    samples = {
        1: {
            "rows": [
                {"id": 1, "raw_text": "", "is_degraded": 1, "insight_text": "fallback"},
                {"id": 2, "raw_text": raw, "is_degraded": 0, "insight_text": "正常見解"},
                {"id": 3, "raw_text": "短文", "is_degraded": 1, "insight_text": "fallback"},
            ],
            "only_with_raw": False,
            "only_degraded": True,
            "limit": 1,
            "expected_selected_ids": [1],
        },
        2: {
            "rows": [
                {"id": 10, "raw_text": raw, "is_degraded": 0, "insight_text": "見解甲"},
                {"id": 11, "raw_text": "", "is_degraded": 0, "insight_text": "見解乙"},
                {"id": 12, "raw_text": raw + "丙", "is_degraded": 0, "insight_text": "見解丙"},
            ],
            "only_with_raw": False,
            "only_degraded": False,
            "limit": 2,
            "expected_selected_ids": [10, 11],
        },
        3: {
            "rows": [
                {"id": 20, "raw_text": raw, "is_degraded": 1, "insight_text": "degraded"},
                {"id": 21, "raw_text": "", "is_degraded": 0, "insight_text": "fallback result"},
                {"id": 22, "raw_text": raw + "丙", "is_degraded": 0, "insight_text": "正常見解"},
            ],
            "only_with_raw": False,
            "only_degraded": True,
            "limit": 2,
            "expected_selected_ids": [20, 21],
        },
    }
    payload = {"sample_id": sample_id, **samples[sample_id]}
    for row in payload["rows"]:
        row_id = int(row["id"])
        row.update(
            {
                "case_number": f"115年度台上字第{row_id}號",
                "document_name": f"fixture-{row_id}.pdf",
                "court_reference": f"TPSM,115,台上,{row_id},20260717,1",
                "court_type": "最高法院",
                "insight_type": "法院見解",
                "case_reason": "損害賠償",
                "source_file": f"fixture-{row_id}.pdf",
            }
        )
    return payload


def _product_input(job_id: str, sample_id: int) -> dict[str, Any]:
    if job_id == "job_business_module_live_check":
        return _business_input(sample_id)
    if job_id == "job_heavy_translation_quality_live":
        return {
            "sample_id": sample_id,
            "pdf": f"source-{sample_id}.pdf",
            "provider": "heavy-provider.json",
        }
    if job_id == "job_distill_train_gemma":
        good_count, rejected_count = {1: (3, 1), 2: (4, 1), 3: (4, 2)}[sample_id]
        return {
            "sample_id": sample_id,
            "raw_pairs": "raw_pairs.jsonl",
            "training_profile": "training-profile.json",
            "expected_counts": {
                "raw": good_count + rejected_count,
                "usable": good_count,
                "skipped": rejected_count,
            },
        }
    if job_id == "job_insight_sync":
        return _insight_input(sample_id)
    if job_id == "job_reprocess_insights":
        return _reprocess_input(sample_id)
    raise ValueError(f"unsupported product fixture job: {job_id}")


def populate_product_fixture(
    fixture_root: Path, *, job_id: str, sample_id: int
) -> dict[str, Any]:
    if job_id not in JOBS or type(sample_id) is not int or not 1 <= sample_id <= 3:
        raise ValueError("product fixture job/sample is invalid")
    root = fixture_root.resolve(strict=True)
    marker = root / ".magi-v3-schedule-fixture"
    if not marker.exists():
        marker.write_text(job_id + "\n", encoding="utf-8")
    inputs = root / "inputs"
    inputs.mkdir(mode=0o700, exist_ok=False)
    product_input = _product_input(job_id, sample_id)
    if job_id == "job_business_module_live_check":
        runtime = root / "workspace" / "runtime"
        drive_runtime = runtime / "drive_sync"
        drive_runtime.mkdir(parents=True, exist_ok=True)
        (inputs / "business-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.business-probe-provider/v1",
                    "nas_shares": {
                        "cases": {"available": True, "mounted": True, "mode": "fixture"},
                        "archive": {"available": True, "mounted": True, "mode": "fixture"},
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (inputs / "laf-provider.json").write_text(
            json.dumps(
                {
                    "portal_cases": [
                        {
                            "case_number": "1150529-W-002",
                            "client_name": "隔離當事人",
                            "file_list": ["接案通知書.pdf"],
                        }
                    ],
                    "pending_drafts": {
                        "case_status": [
                            {
                                "applyno": "1150529-W-002",
                                "reply_type": "結案回報",
                                "status": "暫存",
                                "row_text": "1150529-W-002 fixture",
                            }
                        ][:sample_id],
                        "closing": [],
                        "condition": [],
                        "go_live": [],
                        "progress": [],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (inputs / "review-batch.pdf").write_bytes(
            b"%PDF-1.4\nfixture file review batch\n%%EOF\n"
        )
        (inputs / "file-review-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.file-review-scheduled-fixture/v1",
                    "emails": [
                        {
                            "kind": "downloadable",
                            "case_number": f"FIXTURE-{sample_id:03d}",
                        }
                    ],
                    "portal_files": ["inputs/review-batch.pdf"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        transcript_config = {
            "schema": "magi.transcript-probe-fixture/v1",
            "sample_id": sample_id,
        }
        (inputs / "transcript-config.json").write_text(
            json.dumps(transcript_config, sort_keys=True) + "\n", encoding="utf-8"
        )
        (root / "transcript-config.json").write_text(
            json.dumps(transcript_config, sort_keys=True) + "\n", encoding="utf-8"
        )
        (drive_runtime / "drive_case_sync_worker_status_latest.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "success": True,
                    "status": "completed",
                    "worker_kind": "priority",
                    "summary": {"matched_case_folders": 10 + sample_id},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (runtime / "osc_events_refresh_latest.json").write_text(
            json.dumps(
                {
                    "calendar_audit": {
                        "ok": True,
                        "summary": {
                            "checked_primary_events": sample_id + 1,
                            "checked_source_events": sample_id,
                        },
                    },
                    "calendar_import": {
                        "ok": True,
                        "imported": sample_id,
                        "skipped": 0,
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif job_id == "job_heavy_translation_quality_live":
        from scripts.ops.heavy_translation_quality_live import write_generated_fixture

        write_generated_fixture(inputs / str(product_input["pdf"]))
        (inputs / "heavy-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.heavy-translation-provider/v1",
                    "route": "nvidia_nim",
                    "model": f"bounded-nim-sample-{sample_id}",
                    "quality_certified": False,
                    "route_response": "司法通譯",
                    "title_response": "司法通譯語言風格如何影響國民法官對被告的印象",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif job_id == "job_distill_train_gemma":
        good_count = int(product_input["expected_counts"]["usable"])
        rejected_count = int(product_input["expected_counts"]["skipped"])
        rows = [
            _distill_row(f"sample-{sample_id}-good-{index}")
            for index in range(good_count)
        ] + [
            _distill_row(f"sample-{sample_id}-bad-{index}", rejected=True)
            for index in range(rejected_count)
        ]
        (inputs / "raw_pairs.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        (inputs / "training-profile.json").write_text(
            json.dumps(
                {
                    "schema": "magi.gemma-bounded-training/v1",
                    "sample_id": sample_id,
                    "optimizer_steps": sample_id + 2,
                    "learning_rate": 0.05,
                    "initial_weight": 0.0,
                    "initial_bias": 0.0,
                    "deploy": "forbidden",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif job_id == "job_insight_sync":
        (inputs / "embedding-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.insight-embedding-provider/v1",
                    "model": "bounded-legal-embedding",
                    "dimensions": 16,
                    "salt": f"insight-sync-sample-{sample_id}",
                    "quality_certified": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    elif job_id == "job_reprocess_insights":
        documents: dict[str, str] = {}
        summaries: dict[str, str] = {}
        for row in product_input["rows"]:
            raw_text = str(row.get("raw_text") or "")
            if len(raw_text) <= 100:
                raw_text = (
                    f"法院認為本件第{row['id']}號損害賠償爭議，應由請求人就權利受侵害、損害及相當因果關係負舉證責任。"
                    "審理法院仍應依卷內證據、當事人攻擊防禦方法與民法規定，逐項判斷請求是否有理由。"
                    "如未能提出足以證明損害範圍之資料，即不得僅憑抽象主張認定賠償數額。"
                )
                documents[str(row["court_reference"])] = raw_text
            summaries[hashlib.sha256(raw_text.encode("utf-8")).hexdigest()] = (
                "## 實務見解\n"
                + raw_text[:180]
                + "\n\n## 適用法條\n民法第184條。"
            )
        (inputs / "reprocess-provider.json").write_text(
            json.dumps(
                {
                    "schema": "magi.v3.reprocess-insights-provider/v1",
                    "api_documents": documents,
                    "model_summaries": summaries,
                    "model": "bounded-nvidia-nim",
                    "quality_certified": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    manifest = {
        "schema": "magi.schedule-product-fixture/v1",
        "job_id": job_id,
        "product_input": product_input,
    }
    (root / "fixture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = ["JOBS", "adapter_proposals", "populate_product_fixture"]
