from __future__ import annotations

import glob as _glob
import json
import logging
import os
import time
from pathlib import Path

from api.runtime_paths import get_config_path

_logger = logging.getLogger("magi.case_path_mapper")

_HOME = Path.home()
_NAS_HOME_USER = (
    os.environ.get("MAGI_NAS_HOME_USER")
    or os.environ.get("MAGI_NAS_USER")
    or "home"
).strip().strip("/\\") or "home"
_NAS_CLOSED_SHARE_NAME = (
    os.environ.get("MAGI_NAS_CLOSED_SHARE_NAME")
    or os.environ.get("MAGI_NAS_ARCHIVE_SHARE")
    or "archive"
).strip().strip("/\\") or "archive"
_CANONICAL_ACTIVE_SHARE_PREFIX = (
    os.environ.get("MAGI_CANONICAL_ACTIVE_SHARE_PREFIX")
    or f"Z:/{_NAS_HOME_USER}"
).replace("\\", "/").rstrip("/")
_CANONICAL_CLOSED_SHARE_PREFIX = (
    os.environ.get("MAGI_CANONICAL_CLOSED_SHARE_PREFIX")
    or "Y:/archive"
).replace("\\", "/").rstrip("/")
_CANONICAL_ACTIVE_CASE_PREFIX = (
    os.environ.get("MAGI_CANONICAL_ACTIVE_CASE_PREFIX")
    or f"{_CANONICAL_ACTIVE_SHARE_PREFIX}/01_案件"
).replace("\\", "/").rstrip("/")
_CANONICAL_CLOSED_CASE_PREFIX = (
    os.environ.get("MAGI_CANONICAL_CLOSED_CASE_PREFIX")
    or f"{_CANONICAL_CLOSED_SHARE_PREFIX}/03_工作資料/10_結案"
).replace("\\", "/").rstrip("/")


def _is_dir_accessible(path: str) -> bool:
    """檢查路徑是否真正可存取（防止 stale mount 誤判）。

    用 os.stat 而非 os.listdir — SMB over Tailscale 的 listdir 可能要 10-30 秒，
    stat 通常 <0.1 秒。stat 失敗代表掛載已 stale。
    SMB hang 時 stat 會無限卡住（kernel uninterruptible sleep），
    所以 /Volumes/ 路徑用 thread + timeout 保護。
    """
    # 本機路徑（/Users/、SynologyDrive）不需 timeout
    if path.startswith("/Users/") or not path.startswith("/Volumes/"):
        try:
            st = os.stat(path)
            return st.st_mode & 0o40000 != 0
        except OSError:
            return False

    # /Volumes/ SMB 路徑：用 thread timeout 保護，防止 NAS hang 卡住整個 process
    import threading
    _result = [False]
    def _check():
        try:
            st = os.stat(path)
            _result[0] = st.st_mode & 0o40000 != 0
        except OSError:
            _result[0] = False
    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout=2)  # 最多等 2 秒
    if t.is_alive():
        _logger.debug("_is_dir_accessible timeout (2s): %s — SMB 可能 hang", path)
        return False
    return _result[0]


def _discover_volume(base: str, subdir: str = "") -> str:
    """動態偵測 SMB 掛載路徑（macOS 可能掛成 -1, -2 等後綴）。

    Args:
        base: 預設掛載名稱，如 "homes" 或 archive share
        subdir: 掛載點下的子目錄，例如使用者 home 目錄或共享資料夾名
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


# SynologyDrive 優先（本機 CloudStation 同步，不走 SMB，不會 hang）
# SMB mount 放後面做 lazy fallback，避免 import 時 stat /Volumes/ 卡住整個 process
_DEFAULT_ACTIVE_SHARE_ROOTS = [
    str(_HOME / "Library/CloudStorage/SynologyDrive-homes"),
    str(_HOME / "SynologyDrive/homes"),
    str(_HOME / "SynologyDrive"),
    _discover_volume("homes", _NAS_HOME_USER),
]


# 動態偵測結案歸檔根目錄 — 每 N 秒重新掃描一次，不 cache 在 module-level，
# 因為使用者可能把 Seagate 外接硬碟從本機改掛到 NAS（反之亦然）。
_CLOSED_ROOT_CACHE: dict = {"roots": None, "expires": 0.0}
_CLOSED_ROOT_TTL = float(os.environ.get("MAGI_CLOSED_ROOT_TTL_SEC", "60"))


def _probe_external_closed_roots() -> list[str]:
    """
    動態掃描所有可能的結案歸檔位置，按優先序返回：
      1. MAGI_CLOSED_CASE_ROOT env var（顯式指定）
      2. MAGI_CLOSED_VOLUME env var（只指定 /Volumes/<名稱>）
      3. 本機/NAS 上任一含 <archive-share>/03_工作資料/10_結案 的掛載點（自動掃 /Volumes/）
      4. SynologyDrive 雲同步變體
      5. NAS archive share 的 canonical/-1/-2 suffix
    每個候選都經 _is_dir_accessible 驗證（2 秒 timeout），避免 stale SMB 誤判。
    """
    candidates: list[str] = []

    # 1. 環境變數
    env_root = os.environ.get("MAGI_CLOSED_CASE_ROOT", "").strip()
    if env_root:
        candidates.append(env_root)
    env_vol = os.environ.get("MAGI_CLOSED_VOLUME", "").strip()
    if env_vol:
        candidates.append(os.path.join(
            env_vol if env_vol.startswith("/Volumes/") else f"/Volumes/{env_vol}",
            _NAS_CLOSED_SHARE_NAME,
        ))

    # 2. 自動掃 /Volumes/ — 跳過系統 volume 和 homes share（防止誤判）
    try:
        for entry in sorted(os.listdir("/Volumes")):
            if entry in ("Macintosh HD", "homes", "homes-1", "homes-2"):
                continue
            root = os.path.join("/Volumes", entry, _NAS_CLOSED_SHARE_NAME)
            probe = os.path.join(root, "03_工作資料", "10_結案")
            if _is_dir_accessible(probe):
                candidates.append(root)
    except OSError:
        pass

    # 3. SynologyDrive 雲同步變體
    synology_variants = [
        str(_HOME / "Library/CloudStorage/SynologyDrive-homes" / _NAS_CLOSED_SHARE_NAME),
        str(_HOME / "SynologyDrive/homes" / _NAS_CLOSED_SHARE_NAME),
        str(_HOME / "SynologyDrive" / _NAS_CLOSED_SHARE_NAME),
    ]
    for v in synology_variants:
        probe = os.path.join(v, "03_工作資料", "10_結案")
        if _is_dir_accessible(probe):
            candidates.append(v)

    # 4. NAS archive share fallback（macOS automount 可能加 -1/-2 後綴）
    nas_discovered = _discover_volume(_NAS_CLOSED_SHARE_NAME, _NAS_CLOSED_SHARE_NAME)
    nas_probe = os.path.join(nas_discovered, "03_工作資料", "10_結案")
    if _is_dir_accessible(nas_probe):
        candidates.append(nas_discovered)

    # 去重保序（inline 不依賴 _dedupe_keep_order 避免 import 時 forward ref）
    seen: set = set()
    out: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _get_default_closed_share_roots() -> list[str]:
    """取得結案歸檔根目錄列表（TTL cached 60 秒，免重複 stat）。

    2026-05-03 加：cache miss 時若 probe 全失敗（可能 NAS 還沒掛起來），
    觸發一次 ensure_nas_mounts() 再 probe，避免 60s window 內所有請求都
    看到 stale empty cache。
    """
    now = time.time()
    if _CLOSED_ROOT_CACHE["roots"] is not None and now < _CLOSED_ROOT_CACHE["expires"]:
        # 若 cache 是「全 inaccessible」狀態，每 5 秒（短 TTL）再試一次，
        # 不要等 60s。避免 NAS 剛 mount 起來但 cache 還在 stale window 內
        cached = list(_CLOSED_ROOT_CACHE["roots"])
        if cached and any(_is_dir_accessible(r) for r in cached):
            return cached
        # cache 全 inaccessible → 給機會 re-probe（每 5 秒）
        if now < _CLOSED_ROOT_CACHE.get("retry_after", 0):
            return cached
    roots = _probe_external_closed_roots()
    # 第一次 probe 全空 → 嘗試觸發 NAS mount 再 probe 一次
    if not roots:
        try:
            from api.nas_mount_guard import ensure_nas_mounts
            ensure_nas_mounts()
            roots = _probe_external_closed_roots()
        except Exception:
            pass
    # 若仍空 → 保底回傳 canonical 路徑
    if not roots:
        roots = [
            str(_HOME / "Library/CloudStorage/SynologyDrive-homes" / _NAS_CLOSED_SHARE_NAME),
            _discover_volume(_NAS_CLOSED_SHARE_NAME, _NAS_CLOSED_SHARE_NAME),
        ]
        # 全空 → 5 秒後就可以 re-probe（不等 60s）
        _CLOSED_ROOT_CACHE["retry_after"] = now + 5
    else:
        _CLOSED_ROOT_CACHE.pop("retry_after", None)
    _CLOSED_ROOT_CACHE["roots"] = tuple(roots)
    _CLOSED_ROOT_CACHE["expires"] = now + _CLOSED_ROOT_TTL
    return list(roots)


def _get_default_closed_roots() -> list[str]:
    """取得結案歸檔 /03_工作資料/10_結案 完整路徑列表。"""
    return [r.rstrip("/") + "/03_工作資料/10_結案" for r in _get_default_closed_share_roots()]


# 保留名稱相容（module 級 list 導出，但避免使用 — 改用 _get_* 函式取得最新）
# 這些只在 import 時產生一次。不要在 import 階段 probe / 掛載結案 NAS，
# 否則單純開進行中案件也可能被離線的結案磁碟拖住。
_DEFAULT_CLOSED_SHARE_ROOTS = [
    str(_HOME / "Library/CloudStorage/SynologyDrive-homes" / _NAS_CLOSED_SHARE_NAME),
    str(_HOME / "SynologyDrive/homes" / _NAS_CLOSED_SHARE_NAME),
    str(_HOME / "SynologyDrive" / _NAS_CLOSED_SHARE_NAME),
    f"/Volumes/{_NAS_CLOSED_SHARE_NAME}/{_NAS_CLOSED_SHARE_NAME}",
]
_DEFAULT_ACTIVE_ROOTS = [
    root.rstrip("/") + "/01_案件" for root in _DEFAULT_ACTIVE_SHARE_ROOTS
]
_DEFAULT_CLOSED_ROOTS = [
    root.rstrip("/") + "/03_工作資料/10_結案" for root in _DEFAULT_CLOSED_SHARE_ROOTS
]
_ACTIVE_PREFIXES = [
    _CANONICAL_ACTIVE_CASE_PREFIX,
    "K:/SynologyDrive/01_案件",
]
_CLOSED_PREFIXES = [
    _CANONICAL_CLOSED_CASE_PREFIX,
]
_ACTIVE_SHARE_PREFIXES = [
    _CANONICAL_ACTIVE_SHARE_PREFIX,
    "K:/SynologyDrive",
]
_CLOSED_SHARE_PREFIXES = [
    _CANONICAL_CLOSED_SHARE_PREFIX,
]


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
        # 動態查最新結案根（Seagate 可能在本機或 NAS 上，TTL 60 秒）
        roots.extend(_get_default_closed_share_roots())
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
    # 本機 SynologyDrive 檔案可能只是 macOS File Provider 的 dataless 佔位檔。
    # 將任何已知進行中案件根路徑互相映射，讓下載端點可改讀 /Volumes/homes 的 SMB 真檔案。
    candidates.extend(_expand_from_prefix(s, active_roots, active_roots))
    import re as _re
    needs_closed_roots = (
        s.startswith("Y:/")
        or s.startswith("Y:\\")
        or "/03_工作資料/10_結案" in s
        or "\\03_工作資料\\10_結案" in s
        or bool(_re.match(r"^/Volumes/" + _re.escape(_NAS_CLOSED_SHARE_NAME) + r"(?:-\d+)?/" + _re.escape(_NAS_CLOSED_SHARE_NAME) + r"/", s))
    )
    closed_roots: list[str] = []
    if needs_closed_roots:
        all_roots = default_synology_share_roots(include_closed=True, cfg=cfg)
        closed_roots = all_roots[len(active_roots) :]

    candidates.extend(_expand_from_prefix(s, _ACTIVE_SHARE_PREFIXES, active_roots))
    candidates.extend(_expand_from_prefix(s, _CLOSED_SHARE_PREFIXES, closed_roots))

    # 支援 /Volumes/homes/<user>/ 和 /Volumes/homes-N/<user>/
    _homes_pattern = r"^/Volumes/homes(?:-\d+)?/" + _re.escape(_NAS_HOME_USER) + r"/(.*)$"
    _homes_match = _re.match(_homes_pattern, s)
    if _homes_match:
        rel = _homes_match.group(1).lstrip("/")
        for root in _DEFAULT_ACTIVE_SHARE_ROOTS[1:]:
            candidates.append(_join_root_rel(root, rel))
    # 支援 /Volumes/<archive>/<archive>/ 和 /Volumes/<archive>-N/<archive>/ 結案路徑
    _closed_vol_pattern = r"^/Volumes/" + _re.escape(_NAS_CLOSED_SHARE_NAME) + r"(?:-\d+)?/" + _re.escape(_NAS_CLOSED_SHARE_NAME) + r"/(.*)$"
    _closed_vol_match = _re.match(_closed_vol_pattern, s)
    if _closed_vol_match:
        rel = _closed_vol_match.group(1).lstrip("/")
        # 動態查最新結案根；略過第一個（通常是當下 canonical 的 input 路徑本身）
        for root in _get_default_closed_share_roots()[1:]:
            candidates.append(_join_root_rel(root, rel))

    # 動態 fallback: user-level mount (nas_mount_guard 掛到 ~/.magi_mounts/)
    _user_mount_root = str(_HOME / ".magi_mounts")
    for prefix in [_CANONICAL_CLOSED_SHARE_PREFIX + "/"]:
        if s.startswith(prefix):
            rel = s[len(prefix):]
            share_rel = prefix.split(":/", 1)[-1].strip("/")
            share_parts = [part for part in share_rel.split("/") if part]
            user_candidate = os.path.join(_user_mount_root, *share_parts, rel)
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
