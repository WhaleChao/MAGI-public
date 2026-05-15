# -*- coding: utf-8 -*-
"""
LAF Folder Builder
==================
Creates standardized case folders on Synology Drive (local path)
and returns the canonical Z: path for DB storage.

Uses mac_path_mappings from config.json for bidirectional translation.

Usage:
    from laf_folder_builder import LAFFolderBuilder
    builder = LAFFolderBuilder()
    db_path = builder.create_case_folder(case_info)
"""

import os
import re
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_config_path
from api.case_path_mapper import (
    preferred_case_roots,
    translate_case_path_to_local,
    translate_local_path_to_canonical,
)
from api.laf_case_classifier import is_administrative_laf_reason, normalize_laf_case_type

CONFIG_PATH = str(get_config_path("config.json"))

# Standard subfolders for every LAF case
STANDARD_SUBFOLDERS = [
    "01_法扶資料",
    "02_開辦資料",
    "03_結案資料",
    "04_我方歷次書狀",
    "05_對方歷次書狀",
    "06_閱卷資料",
    "07_證據資料",
    "08_筆錄",
    "09_法院通知或程序裁定",
    "10_判決書",
    "11_回執",
    "12_信件往返",
]


class LAFFolderBuilder:
    """
    Creates case folders on SynologyDrive and returns Z: canonical paths for DB.

    Path flow:
        1. Build folder name from case info
        2. Create on local SynologyDrive path (macOS SMB mount)
        3. Convert to Z: canonical path for DB storage
        4. DB read: Z: → local via translate_path_to_local()
    """

    def __init__(self, config_path: str = CONFIG_PATH, experiment_base_dir: Optional[str] = None):
        self.config = self._load_config(config_path)
        # 實驗環境覆寫：避免動到 SynologyDrive，改在本機指定資料夾建立測試結構
        env_exp = os.environ.get("LAF_EXPERIMENT_BASE_DIR", "").strip()
        self.experiment_base_dir = (experiment_base_dir or env_exp).strip() or None
        self._init_path_mappings()

    def _load_config(self, config_path: str) -> dict:
        """Load config.json."""
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Failed to load config: %s", e)
            return {}

    def _init_path_mappings(self):
        """Extract path mappings from config."""
        # 實驗環境：跳過 Synology 掛載偵測與 Z: 轉換
        if self.experiment_base_dir:
            self.windows_base = None
            self.mac_smb_base = None
            self.mac_local_base = os.path.abspath(os.path.expanduser(self.experiment_base_dir))
            try:
                os.makedirs(self.mac_local_base, exist_ok=True)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 92, exc_info=True)
            self.laf_target = "法扶案件"
            logger.info("FolderBuilder (experiment): mac_local=%s", self.mac_local_base)
            return

        mappings = self.config.get("mac_path_mappings", [])

        # Active cases mapping (Z: → smb://)
        self.windows_base = None
        self.mac_smb_base = None
        self.mac_local_base = None

        for m in mappings:
            comment = m.get("comment", "")
            if "進行中" in comment or "Active" in comment:
                self.windows_base = m.get("windows_prefix", "")  # Z:/<active-share>/01_案件
                self.mac_smb_base = m.get("mac_smb_prefix", "")  # smb://MAGI_NAS_HOST/homes/...

        # Derive local mount path from smb path
        # smb://MAGI_NAS_HOST/homes/<user>/01_案件 → /Users/ai/SynologyDrive/01_案件
        # But the actual local path depends on how the user has mounted it.
        # We'll try a few common patterns:
        self._detect_local_mount()

        # LAF subfolder under the case base
        laf_config = self.config.get("laf", {})
        self.laf_target = laf_config.get("target_folder", "")
        # target_folder: canonical active-case root + 法扶案件
        # We just need the relative part: 法扶案件

        logger.info("FolderBuilder: windows_base=%s, mac_local=%s",
                     self.windows_base, self.mac_local_base)

    def _detect_local_mount(self):
        """Detect the local mount point for SynologyDrive."""
        candidates = list(preferred_case_roots(include_closed=False))

        # Also check if the SMB path is mounted
        if self.mac_smb_base:
            # Try to find the mount point via /Volumes
            import subprocess
            try:
                result = subprocess.run(
                    ["mount"], capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.splitlines():
                    try:
                        from api.nas_mount_guard import resolve_nas_host
                        _nas_ip = resolve_nas_host()
                    except Exception:
                        _nas_ip = os.environ.get("MAGI_NAS_HOST", "")
                    active_share = (os.environ.get("MAGI_NAS_HOME_USER") or os.environ.get("MAGI_NAS_USER") or "home").strip().strip("/\\") or "home"
                    if (_nas_ip and _nas_ip in line) or (active_share and active_share in line):
                        # Extract mount point
                        parts = line.split(" on ")
                        if len(parts) >= 2:
                            mount_point = parts[1].split(" (")[0].strip()
                            candidates.insert(0, os.path.join(mount_point, "01_案件"))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 145, exc_info=True)

        for path in candidates:
            if os.path.isdir(path):
                self.mac_local_base = path
                logger.info("Detected local mount: %s", path)
                return

        # Fallback: use first candidate regardless
        self.mac_local_base = candidates[0] if candidates else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")
        logger.warning("No mount detected, using fallback: %s", self.mac_local_base)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_case_folder(self, case_info: Dict) -> Optional[str]:
        """
        Create case folder structure and return canonical Z: path for DB.

        Args:
            case_info: Dict with keys:
                - client_name: str (e.g., "高弘軒")
                - case_type: str (e.g., "消費者債務清理事件")
                - case_reason: str (e.g., "消債更生")
                - laf_case_number: str (optional, e.g., "1141121-E-006")

        Returns:
            Canonical Z: path string for DB storage, or None on failure.
        """
        folder_name = self._build_folder_name(case_info)
        local_path = self._get_local_path(folder_name, case_info)
        canonical_path = self._local_to_canonical(local_path)

        # Create the folder + subfolders
        try:
            os.makedirs(local_path, exist_ok=True)
            for sub in STANDARD_SUBFOLDERS:
                os.makedirs(os.path.join(local_path, sub), exist_ok=True)

            logger.info("✅ Created case folder: %s", local_path)
            logger.info("   DB canonical path: %s", canonical_path)
            return canonical_path

        except Exception as e:
            logger.error("❌ Failed to create folder %s: %s", local_path, e)
            return None

    def get_local_path_from_canonical(self, canonical_path: str) -> str:
        """
        Convert Z: canonical path to local macOS path.
        Z:/<active-share>/01_案件/法扶案件/... → /Users/ai/SynologyDrive/01_案件/法扶案件/...
        """
        return translate_case_path_to_local(canonical_path)

    def folder_exists(self, case_info: Dict) -> bool:
        """Check if the case folder already exists locally."""
        folder_name = self._build_folder_name(case_info)
        local_path = self._get_local_path(folder_name, case_info)
        return os.path.isdir(local_path)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _build_folder_name(self, case_info: Dict) -> str:
        """
        Build standardized folder name from case info.
        Format:
            一般案件: {case_number}-{client_name}-{case_stage_or_type}-{case_reason}
            消債案件: {case_number}-{client_name}-{case_type}-{case_reason}
        """
        case_number = str(case_info.get("case_number", "") or "").strip()
        name = case_info.get("client_name", "未命名")
        case_type = case_info.get("case_type", "")
        case_stage = case_info.get("case_stage", "")
        reason = case_info.get("case_reason", "")

        parts = [name]
        case_category = self._resolve_case_category(case_info)

        if case_category == "消費者債務清理":
            # 消費者債務清理派案，資料夾案由必須帶「更生」
            parts.append("消費者債務清理")
            if "更生" not in reason and "清算" not in reason:
                reason = "更生"
            if reason:
                parts.append(reason)
        else:
            stage_or_type = case_stage or case_type
            if stage_or_type:
                parts.append(stage_or_type)
            if reason and reason != stage_or_type:
                parts.append(reason)

        if case_number:
            parts.insert(0, case_number)

        folder_name = "-".join(parts)

        # Sanitize: remove characters that are invalid in folder names
        folder_name = re.sub(r'[<>:"/\\|?*]', '', folder_name)
        return folder_name

    def _shorten(self, text: str, max_len: int = 10) -> str:
        """Shorten text while keeping meaning."""
        if not text:
            return ""
        # Remove common suffixes
        text = text.replace("事件", "").replace("案件", "").strip()
        if len(text) <= max_len:
            return text
        return text[:max_len]

    def _resolve_case_category(self, case_info: Dict) -> str:
        reason = str(case_info.get("case_reason") or "").strip()
        stage = str(case_info.get("case_stage") or "").strip()

        text = " ".join(
            str(case_info.get(k, "") or "").strip()
            for k in ("case_type", "case_stage", "case_reason")
        )

        # 消費者債務清理是案由/程序，但資料夾仍需歸在 OSC 的消債根目錄。
        if "消費者債務清理" in text or "更生" in text or "清算" in text:
            return "消費者債務清理"

        # 優先採用明確 case_type，但仍允許社會保險等實體行政事件覆寫
        # LAF portal/信件有時把「勞工保險爭議」標為民事程序。
        explicit_type = str(case_info.get("case_type") or "").strip()
        normalized_type, _ = normalize_laf_case_type(explicit_type, stage, reason)
        if normalized_type in ("民事", "刑事", "家事", "消費者債務清理", "少年", "行政"):
            return normalized_type
        if explicit_type in ("民事", "刑事", "家事", "消費者債務清理", "少年", "行政"):
            return explicit_type

        # 家事 / 少年
        if any(token in text for token in ("離婚", "收養", "監護", "扶養", "遺產")):
            return "家事"
        if "少年" in text:
            return "少年"
        if "行政" in text or is_administrative_laf_reason(text):
            return "行政"
        if "非訟" in text:
            return "非訟"
        # 刑事附帶民事本質上是民事求償／移送民事庭處理，資料夾不可因「刑事」兩字建到刑事根目錄。
        if any(token in text for token in ("刑事附帶民事", "附帶民事", "附民")):
            return "民事"
        # 刑事獨有關鍵字（已剔除「上訴」「執行」等民刑共用詞）
        _CRIMINAL_ONLY = (
            "刑事", "偵查", "自訴", "起訴", "公訴", "交保", "羈押",
            "毒品", "殺人", "強盜", "竊盜", "傷害", "詐欺", "侵占",
            "背信", "貪污", "賄賂", "妨害性自主", "公共危險",
            "過失致死", "非常上訴",
        )
        if any(token in text for token in _CRIMINAL_ONLY):
            return "刑事"
        return "民事"

    def _get_local_path(self, folder_name: str, case_info: Dict) -> str:
        """Get full local path for a case folder under 法扶案件/<案件類型>."""
        case_category = self._resolve_case_category(case_info)
        return os.path.join(self.mac_local_base, "法扶案件", case_category, folder_name)

    def _local_to_canonical(self, local_path: str) -> str:
        """
        Convert local macOS path to canonical Z: path for DB.
        /Users/ai/SynologyDrive/01_案件/法扶案件/... → Z:/<active-share>/01_案件/法扶案件/...
        """
        # 實驗環境：直接回傳本機路徑（避免寫入 DB 的 Z: 路徑）
        if self.experiment_base_dir:
            return local_path
        canonical = translate_local_path_to_canonical(os.path.abspath(local_path))
        if canonical and len(canonical) >= 2 and canonical[1] == ":":
            return canonical
        if not self.mac_local_base or not self.windows_base:
            logger.warning("Path mappings not configured, returning as-is")
            return local_path
        if local_path.startswith(self.mac_local_base):
            suffix = local_path[len(self.mac_local_base):]
            canonical = self.windows_base.replace("\\", "/") + suffix
            return canonical.replace("/", "\\")  # DB uses backslash (Windows convention)
        return local_path


# ==============================================================================
# CLI / Self-Test
# ==============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    builder = LAFFolderBuilder()

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_case = {
            "client_name": "測試用戶",
            "case_type": "消費者債務清理事件",
            "case_reason": "消債更生",
            "laf_case_number": "1141121-E-999",
        }

        print(f"Folder name: {builder._build_folder_name(test_case)}")
        print(f"Local path: {builder._get_local_path(builder._build_folder_name(test_case))}")
        print(f"Exists: {builder.folder_exists(test_case)}")

        # Don't actually create in test mode
        print("\n(Dry run — no folders created)")

    elif len(sys.argv) > 1 and sys.argv[1] == "create":
        test_case = {
            "client_name": "測試用戶",
            "case_type": "消費者債務清理事件",
            "case_reason": "消債更生",
        }
        result = builder.create_case_folder(test_case)
        print(f"DB path: {result}")

    else:
        print("Usage:")
        print("  python laf_folder_builder.py test     # 測試路徑轉換 (不建立)")
        print("  python laf_folder_builder.py create   # 建立測試資料夾")
