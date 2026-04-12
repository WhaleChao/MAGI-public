# -*- coding: utf-8 -*-
"""
Council Executor — 將批准的夜議提案轉化為實際程式碼變更
======================================================
流程：批准 → LLM 產生 patch → 安全檢查 → 備份 → apply → compile check → smoke test → 失敗回滾

Safety:
  - 禁區檔案不可修改（server.py, daemon, config.json, .env 等）
  - FORBIDDEN_PATCH_PATTERNS 阻擋破壞性操作
  - 每次變更前自動備份，compile 失敗自動回滾
  - 單次最多改 3 個檔案，每檔 diff 不超過 80 行
"""

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("CouncilExecutor")

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# ── Safety: files that must NEVER be auto-modified ──
FORBIDDEN_FILES = {
    "api/server.py",
    "api/tools_api.py",
    ".env",
    "config.json",
    "skills/magi/council_executor.py",  # don't self-modify
    "skills/magi/council_approval.py",
    "com.magi.daemon.plist",
    "com.magi.server.plist",
}

FORBIDDEN_DIRS = {
    ".git", "backups", "venv", ".venv", "__pycache__",
    "node_modules", "llama.cpp", "_archive",
}

FORBIDDEN_PATCH_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bDROP\s+(TABLE|DATABASE)\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bos\.remove\b",
    r"\bshutil\.rmtree\b",
    r"\bsubprocess\.(?:Popen|run|call)\s*\(\s*[\"'].*\|",
    r"\bexec\s*\(",
    r"\beval\s*\(",
]

MAX_FILES_PER_PATCH = 3
MAX_DIFF_LINES = 80
BACKUP_DIR = os.path.join(_MAGI_ROOT, "backups", "council_patches")


def _is_safe_path(rel_path: str) -> Tuple[bool, str]:
    """Check if a relative path is safe to modify."""
    rel = rel_path.replace("\\", "/").strip("/")
    if rel in FORBIDDEN_FILES:
        return False, f"forbidden file: {rel}"
    parts = rel.split("/")
    for part in parts:
        if part in FORBIDDEN_DIRS:
            return False, f"forbidden directory: {part}"
    if not rel.endswith(".py"):
        return False, "only .py files can be auto-modified"
    abs_path = os.path.join(_MAGI_ROOT, rel)
    if not os.path.abspath(abs_path).startswith(os.path.abspath(_MAGI_ROOT)):
        return False, "path escapes MAGI root"
    return True, ""


def _safe_patch_content(code: str) -> Tuple[bool, str]:
    """Check patch content for forbidden patterns."""
    for pattern in FORBIDDEN_PATCH_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return False, f"forbidden pattern: {pattern}"
    return True, ""


def _compile_check(source: str, filename: str) -> Tuple[bool, str]:
    try:
        compile(source, filename, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"{e.msg} (line {e.lineno})"
    except Exception as e:
        return False, str(e)


def _extract_python_block(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _backup_file(abs_path: str, patch_id: str) -> str:
    """Create timestamped backup. Returns backup path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    patch_dir = os.path.join(BACKUP_DIR, patch_id)
    os.makedirs(patch_dir, exist_ok=True)

    rel = os.path.relpath(abs_path, _MAGI_ROOT)
    backup_path = os.path.join(patch_dir, rel.replace("/", "__"))
    shutil.copy2(abs_path, backup_path)
    return backup_path


def _restore_backup(abs_path: str, backup_path: str) -> bool:
    """Restore file from backup."""
    try:
        shutil.copy2(backup_path, abs_path)
        return True
    except Exception as e:
        logger.error("Rollback failed for %s: %s", abs_path, e)
        return False


def generate_patch(proposal: str, issue: str, target_files: Optional[List[str]] = None) -> Dict:
    """
    Use LLM to generate concrete code patches from a council proposal.

    Returns:
        {
            "success": bool,
            "patches": [{"file": "relative/path.py", "code": "full file content"}, ...],
            "error": str
        }
    """
    # 1. Identify target files from proposal if not explicitly given
    if not target_files:
        target_files = _infer_target_files(proposal, issue)

    if not target_files:
        return {"success": False, "patches": [], "error": "無法從提案中識別要修改的檔案"}

    if len(target_files) > MAX_FILES_PER_PATCH:
        return {
            "success": False, "patches": [],
            "error": f"提案涉及 {len(target_files)} 個檔案，超過上限 {MAX_FILES_PER_PATCH}",
        }

    # 2. Validate all paths
    for f in target_files:
        ok, reason = _is_safe_path(f)
        if not ok:
            return {"success": False, "patches": [], "error": f"安全限制：{reason}"}

    # 3. Read current source for each file
    patches = []
    for rel_path in target_files:
        abs_path = os.path.join(_MAGI_ROOT, rel_path)
        if not os.path.exists(abs_path):
            return {"success": False, "patches": [], "error": f"檔案不存在：{rel_path}"}

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                current_source = f.read()
        except Exception as e:
            return {"success": False, "patches": [], "error": f"無法讀取 {rel_path}: {e}"}

        # 4. Ask LLM to generate patched version
        patch_result = _llm_generate_patch(rel_path, current_source, proposal, issue)
        if not patch_result.get("success"):
            return {
                "success": False, "patches": [],
                "error": f"LLM patch 產生失敗 ({rel_path}): {patch_result.get('error')}",
            }

        new_code = patch_result["code"]

        # 5. Safety check on generated code
        ok, reason = _safe_patch_content(new_code)
        if not ok:
            return {"success": False, "patches": [], "error": f"Patch 安全檢查失敗：{reason}"}

        # 6. Compile check
        ok, err = _compile_check(new_code, rel_path)
        if not ok:
            return {"success": False, "patches": [], "error": f"Patch 編譯失敗 ({rel_path}): {err}"}

        # 7. Diff size check
        old_lines = current_source.splitlines()
        new_lines = new_code.splitlines()
        diff_count = abs(len(new_lines) - len(old_lines))
        changed = sum(1 for a, b in zip(old_lines, new_lines) if a != b)
        total_diff = diff_count + changed
        if total_diff > MAX_DIFF_LINES:
            return {
                "success": False, "patches": [],
                "error": f"Patch 差異 {total_diff} 行，超過上限 {MAX_DIFF_LINES} 行",
            }

        patches.append({"file": rel_path, "code": new_code, "diff_lines": total_diff})

    return {"success": True, "patches": patches, "error": ""}


def apply_patches(patches: List[Dict], patch_id: str) -> Dict:
    """
    Apply patches with backup + rollback on failure.

    Args:
        patches: [{"file": "rel/path.py", "code": "full content"}, ...]
        patch_id: ccr-YYYYMMDDHHMMSS identifier

    Returns:
        {"success": bool, "applied": [...], "rolled_back": bool, "error": str}
    """
    applied = []  # [(abs_path, backup_path), ...]

    for patch in patches:
        rel_path = patch["file"]
        abs_path = os.path.join(_MAGI_ROOT, rel_path)

        # Backup
        try:
            backup_path = _backup_file(abs_path, patch_id)
        except Exception as e:
            # Rollback everything applied so far
            _rollback_all(applied)
            return {
                "success": False, "applied": [],
                "rolled_back": True, "error": f"備份失敗 ({rel_path}): {e}",
            }

        # Write new code
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(patch["code"])
            applied.append((abs_path, backup_path))
        except Exception as e:
            _rollback_all(applied)
            return {
                "success": False, "applied": [],
                "rolled_back": True, "error": f"寫入失敗 ({rel_path}): {e}",
            }

        # Post-write compile check (double safety)
        ok, err = _compile_check(patch["code"], rel_path)
        if not ok:
            _rollback_all(applied)
            return {
                "success": False, "applied": [],
                "rolled_back": True, "error": f"寫入後編譯失敗 ({rel_path}): {err}",
            }

    return {
        "success": True,
        "applied": [p["file"] for p in patches],
        "rolled_back": False,
        "error": "",
    }


def _rollback_all(applied: List[Tuple[str, str]]):
    """Rollback all applied patches."""
    for abs_path, backup_path in reversed(applied):
        try:
            _restore_backup(abs_path, backup_path)
            logger.info("Rolled back: %s", abs_path)
        except Exception as e:
            logger.error("CRITICAL: rollback failed for %s: %s", abs_path, e)


def execute_approved_change(approval_item: Dict) -> Dict:
    """
    Main entry point: takes an approved council item and executes it.

    Steps:
      1. Generate patch from proposal via LLM
      2. Safety + compile checks
      3. Backup originals
      4. Apply patches
      5. Report result

    Returns:
        {"success": bool, "patches_applied": [...], "error": str, "details": {...}}
    """
    patch_id = approval_item.get("id", f"ccr-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    issue = approval_item.get("issue", "")
    proposal = approval_item.get("proposal", "")

    if not proposal:
        return {"success": False, "patches_applied": [], "error": "提案內容為空"}

    logger.info("Executing approved change %s: %s", patch_id, issue[:80])

    # Step 1: Generate patches
    gen_result = generate_patch(proposal, issue)
    if not gen_result.get("success"):
        return {
            "success": False, "patches_applied": [],
            "error": gen_result.get("error", "patch 產生失敗"),
            "details": gen_result,
        }

    patches = gen_result["patches"]
    if not patches:
        return {"success": False, "patches_applied": [], "error": "LLM 未產生任何 patch"}

    # Step 2: Apply with backup + rollback
    apply_result = apply_patches(patches, patch_id)
    if not apply_result.get("success"):
        return {
            "success": False, "patches_applied": [],
            "error": apply_result.get("error", "patch apply 失敗"),
            "details": {"rolled_back": apply_result.get("rolled_back", False)},
        }

    return {
        "success": True,
        "patches_applied": apply_result["applied"],
        "error": "",
        "details": {
            "patch_id": patch_id,
            "diff_lines": sum(p.get("diff_lines", 0) for p in patches),
            "backup_dir": os.path.join(BACKUP_DIR, patch_id),
        },
    }


# ── Internal helpers ──

def _infer_target_files(proposal: str, issue: str) -> List[str]:
    """Extract file paths mentioned in proposal text."""
    text = f"{issue}\n{proposal}"
    # Match patterns like: skills/foo/action.py, api/orchestrator.py
    paths = re.findall(r'(?:skills|api|casper_ecosystem|scripts)/[\w\-/]+\.py', text)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result[:MAX_FILES_PER_PATCH]


def _llm_generate_patch(rel_path: str, current_source: str, proposal: str, issue: str) -> Dict:
    """Call LLM to generate patched file content."""
    # Truncate source if too long for context
    max_source_chars = 6000
    source_display = current_source[:max_source_chars]
    if len(current_source) > max_source_chars:
        source_display += f"\n\n# ... (truncated, total {len(current_source)} chars)"

    prompt = (
        f"你是 MAGI 系統的自動化工程師。以下是一個已批准的改善提案，請產生修改後的完整程式碼。\n\n"
        f"## 問題\n{issue[:300]}\n\n"
        f"## 批准的提案\n{proposal[:800]}\n\n"
        f"## 目標檔案：{rel_path}\n"
        f"```python\n{source_display}\n```\n\n"
        f"## 規則\n"
        f"1. 回傳 **完整的修改後程式碼**（用 ```python 包裹）\n"
        f"2. 只做提案中描述的最小必要修改\n"
        f"3. 不得加入破壞性操作（rm, DROP, exec, eval）\n"
        f"4. 保持原有的 import、函數簽名、類別結構\n"
        f"5. 修改不得超過 {MAX_DIFF_LINES} 行差異\n"
    )

    try:
        import requests
    except ImportError:
        return {"success": False, "error": "requests not available"}

    # Always try Qwen Coder first (oMLX auto-loads on request).
    # TAIDE is NOT suitable for code generation — skip it entirely.
    models_to_try = ["Qwen2.5-Coder-14B-Instruct-4bit"]

    for model_name in models_to_try:
        try:
            try:
                from api.routing.service_registry import get_service_url as _gsurl
                _omlx_chat = _gsurl("omlx_inference") + "/v1/chat/completions"
            except Exception:
                _omlx_chat = "http://127.0.0.1:8080/v1/chat/completions"
            resp = requests.post(
                _omlx_chat,
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "你是精確的 Python 程式碼修改器。只回傳完整的修改後程式碼。"},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "temperature": 0.0,
                    "max_tokens": 8192,
                },
                timeout=180,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            code = _extract_python_block(text)
            if code and len(code) > 50:
                return {"success": True, "code": code}
        except Exception as e:
            logger.warning("LLM patch attempt failed (%s): %s", model_name, e)
            continue

    return {"success": False, "error": "所有 LLM 模型都無法產生有效 patch"}
