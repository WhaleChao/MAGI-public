#!/usr/bin/env python3
"""Generate implementation status from source manifests and the active marker."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from scripts.ops.function_health_index import discover_api_routes, discover_skills


SCHEMA = "magi.v3.generated-implementation-status/v1"
DEFAULT_MARKER = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MAGI"
    / "runtime"
    / "active-release.json"
)


class ImplementationStatusError(ValueError):
    """A source manifest or active-release marker is not trustworthy."""


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ImplementationStatusError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImplementationStatusError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise ImplementationStatusError(f"{label} must be an object")
    return value


def _source_commit(root: Path) -> str:
    release_manifest = root / "release-manifest.json"
    if release_manifest.is_file() and not release_manifest.is_symlink():
        value = _load_json(release_manifest, "source release manifest")
        commit = str(value.get("commit") or "").lower()
        if len(commit) == 40:
            return commit
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ImplementationStatusError("source commit is unavailable")
    return commit


def _source_dirty(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ImplementationStatusError("source worktree state is unavailable")
    return bool(completed.stdout.strip())


def _active_release(marker: Path) -> dict[str, Any]:
    if not marker.exists():
        return {"status": "unavailable", "reason": "active_release_marker_missing"}
    value = _load_json(marker, "active release marker")
    release_id = str(value.get("release_id") or "")
    release_root = Path(str(value.get("release_root") or "")).expanduser()
    if (
        value.get("schema") != "magi.v3.active-release/v1"
        or not release_id
        or not release_root.is_absolute()
        or release_root.is_symlink()
        or not release_root.is_dir()
        or release_root.name != release_id
    ):
        raise ImplementationStatusError("active release marker identity is invalid")
    manifest_path = release_root / "release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = _load_json(manifest_path, "active release manifest")
    if (
        manifest.get("release_id") != release_id
        or value.get("release_manifest_sha256") != manifest_sha256
    ):
        raise ImplementationStatusError("active marker and release manifest mismatch")
    return {
        "status": "active",
        "release_id": release_id,
        "release": str(value.get("release") or "v3"),
        "source_commit": str(manifest.get("commit") or ""),
        "committed_at": str(value.get("committed_at") or ""),
        "manifest_sha256": manifest_sha256,
    }


def _quality_inventory(manifest: Mapping[str, Any]) -> dict[str, Any]:
    legacy = manifest.get("legacy_v2_validation")
    if not isinstance(legacy, Mapping) or legacy.get("mode") != "disabled":
        raise ImplementationStatusError("legacy V2 validation must be disabled")
    paths: list[str] = []
    for section in ("v3_suites", "quality_contract_groups", "golden_sets"):
        groups = manifest.get(section)
        if not isinstance(groups, Mapping):
            raise ImplementationStatusError(f"quality manifest {section} is invalid")
        for values in groups.values():
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise ImplementationStatusError(f"quality manifest {section} entries are invalid")
            paths.extend(values)
    side_effects = manifest.get("side_effect_test_targets")
    if not isinstance(side_effects, list) or any(not isinstance(item, str) for item in side_effects):
        raise ImplementationStatusError("quality manifest side-effect targets are invalid")
    paths.extend(side_effects)
    return {
        "legacy_v2_validation": "disabled",
        "declared_test_reference_count": len(paths),
        "unique_test_file_count": len(set(paths)),
    }


def _release_policy(manifest: Mapping[str, Any]) -> dict[str, Any]:
    policy = manifest.get("policy")
    if not isinstance(policy, Mapping):
        raise ImplementationStatusError("V3 capability release policy is invalid")
    rollback = str(policy.get("rollback_floor_release_id") or "").strip()
    if not rollback or policy.get("rollback_floor_must_remain_immutable") is not True:
        raise ImplementationStatusError("immutable rollback floor is not declared")
    if policy.get("production_mutation_before_candidate_seal_forbidden") is not True:
        raise ImplementationStatusError("pre-seal production mutation must be forbidden")
    order = policy.get("promotion_order")
    expected = [
        "focused_tests",
        "sealed_candidate",
        "single_full_campaign",
        "single_active_cutover",
        "bounded_live_observation",
    ]
    if order != expected:
        raise ImplementationStatusError("promotion order is invalid")
    return {"rollback_floor_release_id": rollback, "promotion_order": expected}


def build_status(
    root: Path,
    *,
    active_marker: Path = DEFAULT_MARKER,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    routes = discover_api_routes(root)
    skills = discover_skills(root)
    schedule = _load_json(
        root / "config" / "v3_schedule_body_adapter_registry.json",
        "schedule adapter registry",
    )
    adapters = schedule.get("new_safe_adapters")
    if not isinstance(adapters, list):
        raise ImplementationStatusError("schedule adapter registry is invalid")
    quality = _quality_inventory(
        _load_json(root / "config" / "v3_release_quality_suites.json", "quality manifest")
    )
    release_policy = _release_policy(
        _load_json(root / "config" / "v3_capability_manifest.json", "capability manifest")
    )
    active = _active_release(active_marker.expanduser())
    source_commit = _source_commit(root)
    if generated_at is None and active.get("committed_at"):
        generated_at = datetime.fromisoformat(
            str(active["committed_at"]).replace("Z", "+00:00")
        )
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_dirty = _source_dirty(root)
    return {
        "schema": SCHEMA,
        "generated_at": generated.isoformat(),
        "source_commit": source_commit,
        "active_release": active,
        "source_worktree_dirty": source_dirty,
        "source_matches_active_release": (
            active.get("source_commit") == source_commit and not source_dirty
        ),
        "inventory": {
            "api_routes": routes["total"],
            "api_route_domains": len(routes["domains"]),
            "skills": skills["total"],
            "skill_issues": sum(
                len(skills.get(key) or [])
                for key in ("missing_skill_md", "missing_action", "duplicate_canonical")
            ),
            "schedule_body_adapters": len(adapters),
            "quality": quality,
        },
        "contracts": {
            "production_generation": "v3",
            "v2_active_matrix": False,
            "promotion_flow": [
                "focused_tests",
                "sealed_candidate",
                "single_full_campaign",
                "single_active_cutover",
                "bounded_live_observation",
            ],
            "rollback_floor_release_id": release_policy["rollback_floor_release_id"],
            "rollback_floor_immutable": True,
            "pre_seal_production_mutation": False,
            "evidence_envelope": "magi.evidence-envelope/v2",
            "external_routes_compatible": True,
        },
    }


def render_markdown(status: Mapping[str, Any]) -> str:
    active = status["active_release"]
    inventory = status["inventory"]
    quality = inventory["quality"]
    active_text = (
        f"{active['release_id']}（V3）"
        if active.get("status") == "active"
        else f"無法由本機 marker 驗證（{active.get('reason', 'unknown')}）"
    )
    matches = "一致" if status.get("source_matches_active_release") else "不同；目前 source 是候選變更"
    lines = [
        "# MAGI V3 實作與驗證狀態",
        "",
        "> 本文件由 `scripts/docs/generate_implementation_status.py` 從 active-release marker、release manifest 與 source manifests 產生；不得手動維護版本與數量。",
        "",
        f"- 產生時間：{status['generated_at']}",
        f"- active production release：{active_text}",
        f"- source 與 active release：{matches}",
        "- production generation：V3；V2 已退出 active validation matrix。",
        "",
        "## 自動盤點",
        "",
        f"- API routes：{inventory['api_routes']}（{inventory['api_route_domains']} 個 domain）",
        f"- skills：{inventory['skills']}（結構問題 {inventory['skill_issues']}）",
        f"- schedule body adapters：{inventory['schedule_body_adapters']}",
        f"- release-quality test files：{quality['unique_test_file_count']}（宣告引用 {quality['declared_test_reference_count']}）",
        "- legacy V2 validation：disabled",
        "",
        "## 發布與證據契約",
        "",
        "- 發布順序固定為 focused tests → sealed candidate → 一次完整 campaign → single-active cutover → bounded LIVE observation。",
        f"- rollback floor：`{status['contracts']['rollback_floor_release_id']}`；候選封裝、測試與切換不得修改或覆寫它。",
        "- V3→V3 rotation drill 必須使用隔離 marker 連跑三次；production marker 全程唯讀。",
        "- sealed candidate 完成前禁止 production mutation；切換前後均須驗證 r59 rollback artifact 與 manifest hash。",
        "- 共享外部資料的 payload receipt 不隨新 release 改寫；候選版本以 deployment-local receipt 另行綁定 release 身分。",
        "- `EvidenceEnvelope v2` 綁定 release、source commit、producer/validator、失效時間、狀態類別、reason code、trace 與 receipt。",
        "- 發布驗收、LIVE 健康、業務 backlog、人工待辦是四種不同狀態；舊 release 證據只可作歷史查詢。",
        "- 現有 web routes、OSC 路徑及法律業務外部契約維持相容；source 測試不等同 LIVE 認證。",
        "",
        "## 判讀限制",
        "",
        "本文件只證明 manifest 與 active marker 的目前觀測，不宣稱尚未執行的 campaign 或 LIVE 驗收已通過。正式結果必須寫入 release-bound Evidence Ledger。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate MAGI V3 implementation status")
    parser.add_argument("--root", type=Path, default=MAGI_ROOT)
    parser.add_argument("--active-marker", type=Path, default=DEFAULT_MARKER)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    status = build_status(args.root, active_marker=args.active_marker)
    outputs = []
    if args.json_out:
        outputs.append((args.json_out, json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2) + "\n"))
    if args.markdown_out:
        outputs.append((args.markdown_out, render_markdown(status)))
    if args.check:
        stale = [str(path) for path, expected in outputs if not path.is_file() or path.read_text(encoding="utf-8") != expected]
        if stale:
            print(json.dumps({"ok": False, "stale": stale}, ensure_ascii=False))
            return 1
    else:
        for path, value in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
