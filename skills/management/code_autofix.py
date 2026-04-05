import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("CodeAutoFix")

from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, legacy_code_enabled

MAGI_ROOT = str(get_magi_root_dir())
LEGACY_CODE_ROOT = str(get_legacy_code_root())
ALLOWED_ROOTS = [MAGI_ROOT]
if legacy_code_enabled():
    ALLOWED_ROOTS.append(LEGACY_CODE_ROOT)

TARGET_ALIASES = {
    "code": MAGI_ROOT,
    "magi": MAGI_ROOT,
    "workspace": MAGI_ROOT,
}
if legacy_code_enabled():
    TARGET_ALIASES["legacy"] = LEGACY_CODE_ROOT
    TARGET_ALIASES["legacy_code"] = LEGACY_CODE_ROOT
    TARGET_ALIASES["archive"] = LEGACY_CODE_ROOT

IGNORE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "venv",
    ".venv",
    "llama.cpp",
    "logs",
}

MAX_FILE_BYTES = 400_000

FORBIDDEN_PATCH_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bsubprocess\.(?:Popen|run|call)\s*\(\s*[\"'].*\|",
]


def _extract_python_block(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"```python\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"```(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _resolve_target(target: str) -> Optional[str]:
    raw = (target or "").strip()
    if not raw:
        return TARGET_ALIASES["magi"]
    if raw.lower() in TARGET_ALIASES:
        return TARGET_ALIASES[raw.lower()]

    abs_path = os.path.abspath(raw)
    for root in ALLOWED_ROOTS:
        if abs_path == root or abs_path.startswith(root + os.sep):
            return abs_path
    return None


def _iter_python_files(root: str, max_files: int = 80, include_tests: bool = False) -> List[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        if not include_tests:
            lower = dirpath.lower()
            if "/tests" in lower or lower.endswith("/test"):
                continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except Exception:
                continue
            found.append(full)
            if len(found) >= max_files:
                return sorted(found)
    return sorted(found)


def _compile_check(source: str, filename: str) -> Tuple[bool, str]:
    try:
        compile(source, filename, "exec")
        return True, ""
    except SyntaxError as e:
        err = f"{e.msg} (line {e.lineno}, col {e.offset})"
        return False, err
    except Exception as e:
        return False, str(e)


def _safe_patch(candidate: str) -> Tuple[bool, str]:
    text = candidate or ""
    for pattern in FORBIDDEN_PATCH_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return False, f"forbidden pattern detected: {pattern}"
    return True, ""


def _build_fix_prompt(path: str, source: str, error_message: str, task_hint: str, round_index: int) -> str:
    return f"""You are fixing a Python file in an automated repair loop.
Rules:
1) Return ONLY full corrected Python code.
2) Keep behavior unchanged except what is needed to fix this failure.
3) Do not add destructive operations.
4) Prefer minimal edits.

Round: {round_index}
File: {path}
Task hint: {task_hint or "none"}
Failure:
{error_message}

Current code:
```python
{source}
```
"""


def _llm_repair_code(path: str, source: str, error_message: str, task_hint: str, round_index: int) -> Dict:
    prompt = _build_fix_prompt(path, source, error_message, task_hint, round_index)
    try:
        import requests
    except Exception as e:
        return {"success": False, "error": f"requests unavailable: {e}"}

    candidates = [
        (
            "http://localhost:8080/v1/chat/completions",
            {
                "model": "qwen2.5-coder:7b",
                "messages": [
                    {"role": "system", "content": "You are a precise Python code fixer."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "temperature": 0.0,
                "max_tokens": 4096,
            },
            ("choices", 0, "message", "content"),
        ),
    ]
    try:
        from skills.evolution.skill_genesis import MELCHIOR_HOST, get_available_melchior_model

        candidates.append(
            (
                f"{MELCHIOR_HOST}/api/generate",
                {
                    "model": get_available_melchior_model([os.environ.get("MAGI_OMLX_CODE_MODEL", ""), os.environ.get("MAGI_MAIN_MODEL", "")]),
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_ctx": 8192},
                },
                ("response",),
            )
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 180, exc_info=True)

    for url, payload, response_path in candidates:
        try:
            resp = requests.post(url, json=payload, timeout=120)
            if resp.status_code != 200:
                continue
            data = resp.json()
            text = data
            for key in response_path:
                text = text[key] if isinstance(key, str) else text[key]
            fixed = _extract_python_block(text)
            if not fixed:
                continue
            ok, reason = _safe_patch(fixed)
            if not ok:
                return {"success": False, "error": reason}
            return {"success": True, "code": fixed, "source_url": url}
        except Exception:
            continue
    return {"success": False, "error": "no repair candidate generated"}


def _create_backup(file_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = f"{file_path}.bak.magi.{stamp}"
    shutil.copy2(file_path, backup)
    return backup


def _verify_tree_compile(files: List[str]) -> Dict:
    errors = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                src = f.read()
            ok, err = _compile_check(src, path)
            if not ok:
                errors.append({"file": path, "error": err})
        except Exception as e:
            errors.append({"file": path, "error": str(e)})
    return {"success": len(errors) == 0, "errors": errors}


def autofix_codebase(
    target: str = "magi",
    max_files: int = 80,
    max_rounds: int = 2,
    dry_run: bool = False,
    include_tests: bool = False,
    task_hint: str = "",
    internalize_skill: bool = False,
    internalize_name: str = "",
) -> Dict:
    """
    Autonomous Python syntax repair loop for allowed code roots.
    """
    root = _resolve_target(target)
    if not root:
        return {"success": False, "error": f"unsupported target: {target}"}
    if not os.path.exists(root):
        return {"success": False, "error": f"target does not exist: {root}"}

    files = _iter_python_files(root, max_files=max(1, int(max_files)), include_tests=bool(include_tests))
    if not files:
        return {"success": False, "error": f"no python files found under {root}"}

    fixes = []
    failures = []
    scanned = 0
    syntax_issues = 0
    autoskill = None
    try:
        from skills.management.auto_skill import AutoSkill

        autoskill = AutoSkill()
    except Exception:
        autoskill = None

    for path in files:
        scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
        except Exception as e:
            failures.append({"file": path, "error": f"read failed: {e}"})
            continue

        ok, err = _compile_check(source, path)
        if ok:
            continue

        syntax_issues += 1
        current = source
        repaired = False
        backup_file = ""
        rounds_log = []
        final_err = err

        for r in range(1, max(1, int(max_rounds)) + 1):
            rounds_log.append({"round": r, "error": final_err})
            patch = _llm_repair_code(path, current, final_err, task_hint, r)
            if not patch.get("success"):
                final_err = patch.get("error", final_err)
                continue

            candidate = patch.get("code", "")
            valid, compile_err = _compile_check(candidate, path)
            if not valid:
                final_err = compile_err
                current = candidate
                continue

            if not dry_run:
                try:
                    backup_file = _create_backup(path)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(candidate)
                except Exception as e:
                    final_err = f"write failed: {e}"
                    break
            repaired = True
            fixes.append(
                {
                    "file": path,
                    "backup": backup_file,
                    "rounds": r,
                    "error_before": err,
                }
            )
            if autoskill:
                try:
                    rel = path.replace(root, "").lstrip("/")
                    autoskill.learn(
                        keywords=["autofix", "python", "syntax", os.path.basename(path)],
                        tip=f"Auto-fixed `{rel}`: {err}",
                        context="autofix",
                        source="casper-autofix",
                        metadata={"file": path, "rounds": r},
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 321, exc_info=True)
            break

        if not repaired:
            failures.append(
                {
                    "file": path,
                    "error": final_err,
                    "error_before": err,
                    "rounds": rounds_log,
                }
            )

    verify = _verify_tree_compile(files)
    internalized = None
    if internalize_skill and autoskill:
        try:
            internalized = autoskill.internalize_as_skill(
                skill_name=internalize_name or "casper-autofix-knowledge",
                description="Auto-learned code repair patterns from CASPER.",
                keywords=["autofix", "syntax", "python"],
                max_tips=60,
                auto_activate=True,
            )
        except Exception as e:
            internalized = {"success": False, "message": str(e)}

    result = {
        "success": len(failures) == 0,
        "target": root,
        "scanned_files": scanned,
        "syntax_issue_files": syntax_issues,
        "fixed_files": len(fixes),
        "failed_files": len(failures),
        "dry_run": bool(dry_run),
        "fixes": fixes[:30],
        "failures": failures[:30],
        "verify": verify,
    }
    if internalized is not None:
        result["internalized"] = internalized

    logger.info(
        "CodeAutoFix finished target=%s scanned=%s syntax=%s fixed=%s failed=%s",
        root,
        scanned,
        syntax_issues,
        len(fixes),
        len(failures),
    )
    return result


if __name__ == "__main__":
    print(json.dumps(autofix_codebase(target="code", max_files=20, max_rounds=1, dry_run=True), ensure_ascii=False, indent=2))
