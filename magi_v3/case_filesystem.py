"""Fail-closed native V3 case-folder creation and closed-case archiving."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from api.osc.folder_utils import build_full_case_path, create_folder_structure
from api.case_path_mapper import (
    is_authoritative_case_storage_path,
    is_authoritative_nas_write_path,
)
from .case_lifecycle import CaseLifecyclePhase, case_lifecycle_phase, requires_closed_storage

from .osc_cases import CaseTransaction, CreateResult, OscCasesError


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _tree_signature(root: Path) -> tuple[tuple[str, str, int, str], ...]:
    rows: list[tuple[str, str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise OscCasesError(f"case tree contains a symlink: {relative}")
        if path.is_dir():
            rows.append((relative, "directory", 0, ""))
        elif path.is_file():
            data = path.read_bytes()
            rows.append((relative, "file", len(data), hashlib.sha256(data).hexdigest()))
        else:
            raise OscCasesError(f"case tree contains a special file: {relative}")
    return tuple(rows)


class NativeCaseFilesystemEffects:
    """Perform only configured case-root writes with transaction compensation."""

    def __init__(
        self,
        *,
        case_root: Path,
        archive_root: Path,
        canonicalize: Callable[[str], str],
        localize: Callable[[str], str],
        require_authoritative_storage: bool = False,
    ) -> None:
        self.case_root = self._root(case_root, "active case root")
        self.archive_root = self._root(archive_root, "archive root")
        if self.case_root == self.archive_root:
            raise OscCasesError("active and archive roots must differ")
        self.canonicalize = canonicalize
        self.localize = localize
        self.require_authoritative_storage = bool(require_authoritative_storage)
        self._assert_authoritative_bindings()

    def _assert_authoritative_bindings(self) -> None:
        if not self.require_authoritative_storage:
            return
        if not is_authoritative_nas_write_path(str(self.case_root)):
            raise OscCasesError("active case root is not a mounted authoritative SMB path")
        if not is_authoritative_case_storage_path(str(self.archive_root)):
            raise OscCasesError("archive root is not mounted authoritative storage")

    @staticmethod
    def _root(path: Path, description: str) -> Path:
        raw = path.expanduser()
        if not raw.is_absolute() or raw.is_symlink():
            raise OscCasesError(f"{description} must be an absolute non-symlink directory")
        try:
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise OscCasesError(f"{description} is unavailable: {exc}") from exc
        if not resolved.is_dir():
            raise OscCasesError(f"{description} must be a directory")
        return resolved

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        canonicalize: Callable[[str], str],
        localize: Callable[[str], str],
    ) -> "NativeCaseFilesystemEffects":
        case_raw = str(environ.get("MAGI_V3_CASE_ROOT") or "").strip()
        archive_raw = str(environ.get("MAGI_V3_ARCHIVE_ROOT") or "").strip()
        disposable_raw = str(environ.get("MAGI_V3_DISPOSABLE_NAS_ROOT") or "").strip()
        if not case_raw or not archive_raw:
            raise OscCasesError("native V3 case and archive roots are required")
        external_writes = _truthy(environ.get("MAGI_V3_EXTERNAL_WRITES_ENABLED"))
        if not external_writes:
            if not disposable_raw:
                raise OscCasesError("native case filesystem writes are disabled")
            disposable = Path(disposable_raw).expanduser().resolve(strict=True)
            case_path = Path(case_raw).expanduser().resolve(strict=True)
            archive_path = Path(archive_raw).expanduser().resolve(strict=True)
            if not (_inside(case_path, disposable) and _inside(archive_path, disposable)):
                raise OscCasesError("disposable case roots escape the declared sandbox")
        return cls(
            case_root=Path(case_raw),
            archive_root=Path(archive_raw),
            canonicalize=canonicalize,
            localize=localize,
            require_authoritative_storage=external_writes,
        )

    def __call__(
        self,
        transaction: CaseTransaction,
        result: CreateResult,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        effects: dict[str, Any] = {}
        auto_folder = _truthy(payload.get("auto_create_folder"))
        if auto_folder:
            effects["folder"] = self._create_folder(transaction, result, payload)
        if requires_closed_storage(payload):
            effects["archive"] = self._archive(transaction, result, payload)
        return effects

    def _create_folder(
        self,
        transaction: CaseTransaction,
        result: CreateResult,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_authoritative_bindings()
        target = Path(
            build_full_case_path(
                str(self.case_root),
                result.case_number,
                str(payload.get("client_name") or payload.get("name") or payload.get("client") or "").strip(),
                case_type=str(payload.get("case_type") or payload.get("type") or "").strip(),
                case_category=str(payload.get("case_category") or payload.get("category") or "一般案件").strip(),
                case_stage=str(payload.get("case_stage") or "").strip(),
                case_reason=str(payload.get("case_reason") or "").strip(),
            )
        )
        resolved_target = target.resolve(strict=False)
        if not _inside(resolved_target, self.case_root) or target.is_symlink():
            raise OscCasesError("generated case folder escapes the configured active root")
        current = self.case_root
        for part in resolved_target.relative_to(self.case_root).parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise OscCasesError("generated case folder traverses a symlink")
        existed = target.exists()
        if existed and not target.is_dir():
            raise OscCasesError("generated case folder collides with a non-directory")
        created = create_folder_structure(
            str(target),
            str(payload.get("case_category") or payload.get("category") or "一般案件").strip(),
        )
        if created.get("ok") is not True:
            if not existed and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise OscCasesError(str(created.get("error") or "case folder creation failed"))
        self._assert_authoritative_bindings()
        if not existed:
            transaction.register_rollback_hook(lambda: shutil.rmtree(target, ignore_errors=False))
        canonical = self.canonicalize(str(target)) or str(target)
        transaction.update_case(result.row_id, {"folder_path": canonical})
        return {
            "ok": True,
            "path": str(target),
            "canonical": canonical,
            "subfolders": list(created.get("subfolders") or []),
            "reason": "already_exists" if existed else "created",
        }

    def _archive(
        self,
        transaction: CaseTransaction,
        result: CreateResult,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_authoritative_bindings()
        row = transaction.find_existing(result.case_number, result.row_id)
        if not row:
            raise OscCasesError("closed case disappeared before archive")
        raw_source = str(row.get("folder_path") or "").strip()
        source_text = self.localize(raw_source) or raw_source
        source = Path(source_text).expanduser()
        if not source.is_absolute() or source.is_symlink():
            raise OscCasesError("closed case source path is missing or unsafe")
        try:
            source = source.resolve(strict=True)
        except OSError as exc:
            raise OscCasesError(f"closed case source is unavailable: {exc}") from exc
        if not source.is_dir() or not _inside(source, self.case_root):
            raise OscCasesError("closed case source escapes the configured active root")
        relative = source.relative_to(self.case_root)
        if len(relative.parts) < 3:
            raise OscCasesError("closed case source lacks category/type/case layout")
        target = self.archive_root / relative
        target_parent = target.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        incoming = target_parent / ".archive_incoming"
        incoming.mkdir(exist_ok=True)
        if target.exists() or target.is_symlink():
            raise OscCasesError("closed case archive target already exists")
        temporary = incoming / f"{target.name}_{uuid.uuid4().hex[:12]}"
        try:
            shutil.copytree(source, temporary, copy_function=shutil.copy2)
            if _tree_signature(source) != _tree_signature(temporary):
                raise OscCasesError("closed case archive copy verification failed")
            os.replace(temporary, target)
            shutil.rmtree(source)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

        def restore_source() -> None:
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.exists():
                raise OscCasesError("cannot roll archive back over an existing active source")
            os.replace(target, source)

        transaction.register_rollback_hook(restore_source)
        canonical = self.canonicalize(str(target)) or str(target)
        phase = case_lifecycle_phase(payload)
        status = "已結案" if phase is CaseLifecyclePhase.CLOSED else "結案中"
        transaction.update_case(result.row_id, {"folder_path": canonical, "status": status})
        return {
            "ok": True,
            "id": result.row_id,
            "case_number": result.case_number,
            "from": str(source),
            "to": str(target),
            "reason": "moved",
        }
