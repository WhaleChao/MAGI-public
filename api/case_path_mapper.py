from __future__ import annotations

import glob as _glob
import json
import logging
import os
from pathlib import Path

from api.runtime_paths import get_config_path

_logger = logging.getLogger("magi.case_path_mapper")

_HOME = Path.home()


def _is_dir_accessible(path: str) -> bool:
    """檢查路徑是否真正可存取（防止 stale mount 誤判）。

    用 os.stat 而非 os.listdir — SMB over Tailscale 的 listdir 可能要 10-30 秒，
    stat 通常 <0.1 秒。stat 失敗代表掛載已 stale。
    """
    try:
        st = os.stat(path)
        return st.st_mode & 0o40000 != 0  # S_ISDIR
    except OSError:
        return False


def _discover_volume(base: str, subdir: str = "") -> str:
    """動態偵測 SMB 掛載路徑（macOS 可能掛成 -1, -2 等後綴）。

    Args:
        base: 預設掛載名稱，如 "homes" 或 "lumi"
        subdir: 掛載點下的子目錄，如 "lumi63181107" 或 "lumi"
    Returns:
        實際可存取的完整路徑，或 canonical 路徑供上層判斷
    """
    canonical = f"/Volumes/{base}/{subdir}" if subdir else f"/Volumes/{base}"
    if _is_dir_accessible(canonical):
        return canonical
    for candidate in sorted(_glob.glob(f"/Volumes/{base}-*/{subdir}" if subdir else f"/Volumes/{base}-*")):
        if _is_dir_accessible(candidate):
            _logger.debug("SMB 掛載使用替代路徑: %s（原 %s）", candidate, canonical)
            return candidate
    # fallback: user-level mount (nas_mount_guard 在 /Volumes/ 無法建目錄時掛到這裡)
    user_mount = str(_HOME / f".magi_mounts/{base}/{subdir}") if subdir else str(_HOME / f".magi_mounts/{base}")
    if _is_dir_accessible(user_mount):
        _logger.debug("SMB 掛載使用 user-level 路徑: %s（原 %s）", user_mount, canonical)
        return user_mount
    return canonical


_DEFAULT_ACTIVE_SHARE_ROOTS = [
    _discover_volume("homes", "lumi63181107"),
    str(_HOME / "Library/CloudStorage/SynologyDrive-homes"),
    str(_HOME / "SynologyDrive/homes"),
    str(_HOME / "SynologyDrive"),
]
_DEFAULT_CLOSED_SHARE_ROOTS = [
    _discover_volume("lumi", "lumi"),
    str(_HOME / "Library/CloudStorage/SynologyDrive-homes/lumi"),
]
_DEFAULT_ACTIVE_ROOTS = [
    root.rstrip("/") + "/01_案件" for root in _DEFAULT_ACTIVE_SHARE_ROOTS
]
_DEFAULT_CLOSED_ROOTS = [
    root.rstrip("/") + "/03_工作資料/10_結案" for root in _DEFAULT_CLOSED_SHARE_ROOTS
]
_ACTIVE_PREFIXES = [
    "Z:/lumi63181107/01_案件",
    "K:/SynologyDrive/01_案件",
]
_CLOSED_PREFIXES = [
    "Y:/lumi/03_工作資料/10_結案",
    "Y:/lumi63181107/03_工作資料/10_結案",
]
_ACTIVE_SHARE_PREFIXES = [
    "Z:/lumi63181107",
    "K:/SynologyDrive",
]
_CLOSED_SHARE_PREFIXES = [
    "Y:/lumi",
    "Y:/lumi63181107",
]
_CANONICAL_ACTIVE_CASE_PREFIX = "Z:/lumi63181107/01_案件"
_CANONICAL_CLOSED_CASE_PREFIX = "Y:/lumi/03_工作資料/10_結案"
_CANONICAL_ACTIVE_SHARE_PREFIX = "Z:/lumi63181107"
_CANONICAL_CLOSED_SHARE_PREFIX = "Y:/lumi"


def load_path_config(config_path: Optional[str] = None) -> dict:
    p = (config_path or "").strip() or str(get_config_path("config.json"))
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _norm(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _dedupe_keep_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        p = _norm(raw).rstrip("/")
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _join_root_rel(root: str, rel: str) -> str:
    base = _norm(root).rstrip("/")
    tail = _norm(rel).lstrip("/")
    while tail.startswith("01_案件/01_案件/"):
        tail = tail[len("01_案件/") :]
    while tail.startswith("03_工作資料/10_結案/03_工作資料/10_結案/"):
        tail = tail[len("03_工作資料/10_結案/") :]
    if not tail:
        return base
    if base.endswith("/01_案件"):
        if tail == "01_案件":
            tail = ""
        elif tail.startswith("01_案件/"):
            tail = tail[len("01_案件/") :]
    if base.endswith("/03_工作資料/10_結案"):
        if tail == "03_工作資料/10_結案":
            tail = ""
        elif tail.startswith("03_工作資料/10_結案/"):
            tail = tail[len("03_工作資料/10_結案/") :]
    return base if not tail else f"{base}/{tail}"


def _derive_volume_prefix_from_smb(smb_prefix: str) -> str:
    s = _norm(smb_prefix).rstrip("/")
    if not s.lower().startswith("smb://"):
        return ""
    rest = s[6:]
    parts = [seg for seg in rest.split("/") if seg]
    if len(parts) < 2:
        return ""
    share = parts[1]
    tail = "/".join(parts[2:])
    return f"/Volumes/{share}" + (f"/{tail}" if tail else "")


def _candidate_roots_from_config(cfg: dict, *, closed: bool) -> list[str]:
    roots: list[str] = []
    for rule in cfg.get("mac_path_mappings") or []:
        if not isinstance(rule, dict):
            continue
        win_prefix = _norm(rule.get("windows_prefix") or "").rstrip("/")
        smb_prefix = _norm(rule.get("mac_smb_prefix") or "").rstrip("/")
        if not win_prefix:
            continue
        is_closed_rule = win_prefix.startswith("Y:/") or "/03_工作資料/10_結案" in win_prefix
        if bool(is_closed_rule) != bool(closed):
            continue
        volume_prefix = _derive_volume_prefix_from_smb(smb_prefix)
        if volume_prefix:
            roots.append(volume_prefix)
    return roots


def _candidate_share_roots_from_config(cfg: dict, *, closed: bool) -> list[str]:
    roots: list[str] = []
    for root in _candidate_roots_from_config(cfg, closed=closed):
        norm = _norm(root).rstrip("/")
        if closed and norm.endswith("/03_工作資料/10_結案"):
            roots.append(norm[: -len("/03_工作資料/10_結案")])
        elif (not closed) and norm.endswith("/01_案件"):
            roots.append(norm[: -len("/01_案件")])
    return roots


def default_synology_share_roots(*, include_closed: bool = False, cfg: Optional[dict] = None) -> list[str]:
    cfg = cfg or load_path_config()
    roots: list[str] = []
    roots.extend(_candidate_share_roots_from_config(cfg, closed=False))
    roots.extend(_DEFAULT_ACTIVE_SHARE_ROOTS)
    if include_closed:
        roots.extend(_candidate_share_roots_from_config(cfg, closed=True))
        roots.extend(_DEFAULT_CLOSED_SHARE_ROOTS)
    return _dedupe_keep_order(roots)


def preferred_synology_share_roots(*, include_closed: bool = False, cfg: Optional[dict] = None) -> list[str]:
    all_roots = default_synology_share_roots(include_closed=include_closed, cfg=cfg)
    active_roots = default_synology_share_roots(include_closed=False, cfg=cfg)
    closed_roots = all_roots[len(active_roots) :]
    out: list[str] = []
    if active_roots:
        # _is_dir_accessible: 實際 listdir 測試，防止 stale SMB mount 誤判
        out.append(next((p for p in active_roots if _is_dir_accessible(p)), active_roots[0]))
    if include_closed and closed_roots:
        out.append(next((p for p in closed_roots if _is_dir_accessible(p)), closed_roots[0]))
    return _dedupe_keep_order(out)


def default_case_roots(*, include_closed: bool = False, cfg: Optional[dict] = None) -> list[str]:
    roots: list[str] = []
    share_roots = default_synology_share_roots(include_closed=include_closed, cfg=cfg)
    active_count = len(default_synology_share_roots(include_closed=False, cfg=cfg))
    for root in share_roots[:active_count]:
        roots.append(_join_root_rel(root, "01_案件"))
    if include_closed:
        for root in share_roots[active_count:]:
            roots.append(_join_root_rel(root, "03_工作資料/10_結案"))
    return _dedupe_keep_order(roots)


def preferred_case_roots(*, include_closed: bool = False, cfg: Optional[dict] = None) -> list[str]:
    roots = preferred_synology_share_roots(include_closed=include_closed, cfg=cfg)
    out: list[str] = []
    if roots:
        out.append(_join_root_rel(roots[0], "01_案件"))
    if include_closed and len(roots) > 1:
        out.append(_join_root_rel(roots[1], "03_工作資料/10_結案"))
    return _dedupe_keep_order(out)


def canonical_case_roots(*, include_closed: bool = False, cfg: Optional[dict] = None) -> list[str]:
    cfg = cfg or load_path_config()
    active = [
        _norm(rule.get("windows_prefix") or "").rstrip("/")
        for rule in cfg.get("mac_path_mappings") or []
        if isinstance(rule, dict)
        and not (
            _norm(rule.get("windows_prefix") or "").startswith("Y:/")
            or "/03_工作資料/10_結案" in _norm(rule.get("windows_prefix") or "")
        )
        and _norm(rule.get("windows_prefix") or "").rstrip("/")
    ]
    out: list[str] = active or [_CANONICAL_ACTIVE_CASE_PREFIX]
    if include_closed:
        closed = [
            _norm(rule.get("windows_prefix") or "").rstrip("/")
            for rule in cfg.get("mac_path_mappings") or []
            if isinstance(rule, dict)
            and (
                _norm(rule.get("windows_prefix") or "").startswith("Y:/")
                or "/03_工作資料/10_結案" in _norm(rule.get("windows_prefix") or "")
            )
            and _norm(rule.get("windows_prefix") or "").rstrip("/")
        ]
        out.extend(closed or [_CANONICAL_CLOSED_CASE_PREFIX])
    return _dedupe_keep_order(out)


def _expand_from_prefix(path: str, prefixes: list[str], roots: list[str]) -> list[str]:
    norm = _norm(path).rstrip("/")
    out: list[str] = []
    for prefix in prefixes:
        pfx = _norm(prefix).rstrip("/")
        if norm.lower() == pfx.lower():
            rel = ""
        elif norm.lower().startswith((pfx + "/").lower()):
            rel = norm[len(pfx):].lstrip("/")
        else:
            continue
        for root in roots:
            out.append(_join_root_rel(root, rel))
    return out


def local_synology_path_candidates(path: str, cfg: Optional[dict] = None) -> list[str]:
    s = _norm(path)
    if not s:
        return []

    cfg = cfg or load_path_config()
    candidates: list[str] = []

    if s.startswith("/Volumes/") or s.startswith("/Users/"):
        candidates.append(s)
    if s.lower().startswith("smb://"):
        volume = _derive_volume_prefix_from_smb(s)
        if volume:
            candidates.append(volume)

    active_roots = default_synology_share_roots(include_closed=False, cfg=cfg)
    all_roots = default_synology_share_roots(include_closed=True, cfg=cfg)
    closed_roots = all_roots[len(active_roots) :]

    candidates.extend(_expand_from_prefix(s, _ACTIVE_SHARE_PREFIXES, active_roots))
    candidates.extend(_expand_from_prefix(s, _CLOSED_SHARE_PREFIXES, closed_roots))

    import re as _re
    # 支援 /Volumes/homes/lumi63181107/ 和 /Volumes/homes-N/lumi63181107/
    _homes_match = _re.match(r"^/Volumes/homes(?:-\d+)?/lumi63181107/(.*)$", s)
    if _homes_match:
        rel = _homes_match.group(1).lstrip("/")
        for root in _DEFAULT_ACTIVE_SHARE_ROOTS[1:]:
            candidates.append(_join_root_rel(root, rel))
    # 支援 /Volumes/lumi/lumi/ 和 /Volumes/lumi-N/lumi/ 結案路徑
    _lumi_match = _re.match(r"^/Volumes/lumi(?:-\d+)?/lumi/(.*)$", s)
    if _lumi_match:
        rel = _lumi_match.group(1).lstrip("/")
        for root in _DEFAULT_CLOSED_SHARE_ROOTS[1:]:
            candidates.append(_join_root_rel(root, rel))

    # 動態 fallback: user-level mount (nas_mount_guard 掛到 ~/.magi_mounts/)
    _user_mount_root = str(_HOME / ".magi_mounts")
    for prefix, subpath_start in [("Y:/lumi/", "lumi/"), ("Y:/lumi63181107/", "lumi63181107/")]:
        if s.startswith(prefix):
            rel = s[len(prefix):]
            user_candidate = os.path.join(_user_mount_root, "lumi", subpath_start.rstrip("/"), rel)
            candidates.append(user_candidate)

    return _dedupe_keep_order(candidates or [s])


def local_case_path_candidates(path: str, cfg: Optional[dict] = None) -> list[str]:
    return local_synology_path_candidates(path, cfg=cfg)


def translate_case_path_to_local(path: str, cfg: Optional[dict] = None, *, require_existing: bool = False) -> str:
    candidates = local_case_path_candidates(path, cfg=cfg)
    if not candidates:
        return _norm(path)

    for cand in candidates:
        if cand.startswith("/Users/") or cand.startswith("/Volumes/"):
            if os.path.exists(cand):
                return cand

    if require_existing:
        return ""

    for cand in candidates:
        if cand.startswith("/Users/") or cand.startswith("/Volumes/"):
            return cand
    return candidates[0]


def translate_synology_path_to_local(path: str, cfg: Optional[dict] = None, *, require_existing: bool = False) -> str:
    candidates = local_synology_path_candidates(path, cfg=cfg)
    if not candidates:
        return _norm(path)

    for cand in candidates:
        if cand.startswith("/Users/") or cand.startswith("/Volumes/"):
            if os.path.exists(cand):
                return cand

    if require_existing:
        return ""

    for cand in candidates:
        if cand.startswith("/Users/") or cand.startswith("/Volumes/"):
            return cand
    return candidates[0]


def translate_local_path_to_canonical(path: str, cfg: Optional[dict] = None) -> str:
    s = _norm(path)
    if not s:
        return ""

    cfg = cfg or load_path_config()
    if s.lower().startswith("smb://"):
        volume = _derive_volume_prefix_from_smb(s)
        if volume:
            s = volume

    # Already a Windows-like canonical/local path; normalize separator only.
    if len(s) >= 2 and s[1] == ":":
        return s.replace("/", "\\")

    active_local_roots = default_case_roots(include_closed=False, cfg=cfg)
    active_canonical_roots = canonical_case_roots(include_closed=False, cfg=cfg) or [_CANONICAL_ACTIVE_CASE_PREFIX]
    for local_root in active_local_roots:
        root = _norm(local_root).rstrip("/")
        if s.lower() == root.lower() or s.lower().startswith((root + "/").lower()):
            rel = s[len(root):].lstrip("/")
            canon = active_canonical_roots[0].rstrip("/")
            return (canon if not rel else f"{canon}/{rel}").replace("/", "\\")

    closed_local_roots = default_case_roots(include_closed=True, cfg=cfg)[len(active_local_roots):]
    closed_canonical_roots = canonical_case_roots(include_closed=True, cfg=cfg)[len(active_canonical_roots):] or [_CANONICAL_CLOSED_CASE_PREFIX]
    for local_root in closed_local_roots:
        root = _norm(local_root).rstrip("/")
        if s.lower() == root.lower() or s.lower().startswith((root + "/").lower()):
            rel = s[len(root):].lstrip("/")
            canon = closed_canonical_roots[0].rstrip("/")
            return (canon if not rel else f"{canon}/{rel}").replace("/", "\\")

    active_share_roots = default_synology_share_roots(include_closed=False, cfg=cfg)
    for share_root in active_share_roots:
        root = _norm(share_root).rstrip("/")
        if s.lower() == root.lower() or s.lower().startswith((root + "/").lower()):
            rel = s[len(root):].lstrip("/")
            canon = _CANONICAL_ACTIVE_SHARE_PREFIX
            return (canon if not rel else f"{canon}/{rel}").replace("/", "\\")

    closed_share_roots = default_synology_share_roots(include_closed=True, cfg=cfg)[len(active_share_roots):]
    for share_root in closed_share_roots:
        root = _norm(share_root).rstrip("/")
        if s.lower() == root.lower() or s.lower().startswith((root + "/").lower()):
            rel = s[len(root):].lstrip("/")
            canon = _CANONICAL_CLOSED_SHARE_PREFIX
            return (canon if not rel else f"{canon}/{rel}").replace("/", "\\")

    return s.replace("/", "\\")


def default_scan_roots(cfg: Optional[dict] = None) -> list[str]:
    roots = preferred_synology_share_roots(include_closed=False, cfg=cfg) or default_synology_share_roots(include_closed=False, cfg=cfg)
    out: list[str] = []
    for root in roots:
        out.append(_join_root_rel(root, "02_掃描檔案/01_掃描檔放置區"))
        out.append(_join_root_rel(root, "02_掃描檔案"))
    return _dedupe_keep_order(out)
