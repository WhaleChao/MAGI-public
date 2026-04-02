
import json
import logging
import os
import re
import hashlib
import ast
import shutil
import subprocess
import tempfile
from string import Template
from datetime import datetime
from typing import Dict, List, Optional

from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, legacy_code_enabled

# Knowledge Base Paths
KB_DIR = os.path.expanduser("~/.magi/auto_skill")
KB_FILE = os.path.join(KB_DIR, "knowledge.json")
CODE_INDEX_FILE = os.path.join(KB_DIR, "code_internalization_index.json")
MAGI_ROOT = str(get_magi_root_dir())
SKILLS_ROOT = f"{MAGI_ROOT}/skills"
LEGACY_CODE_ROOT = str(get_legacy_code_root())
ALLOWED_READ_ROOTS = [MAGI_ROOT]
if legacy_code_enabled():
    ALLOWED_READ_ROOTS.append(LEGACY_CODE_ROOT)
CODE_IGNORE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "llama.cpp",
}

logger = logging.getLogger("AutoSkill")
logging.basicConfig(level=logging.INFO)

AUTOSKILL_MIRROR_TO_VECTOR = (
    os.environ.get("MAGI_AUTOSKILL_MIRROR_TO_VECTOR", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)


def _slugify(text: str, fallback: str = "casper-learned") -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (text or "").strip().lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned or fallback


def _uniq_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        key = (item or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((item or "").strip())
    return out


class AutoSkill:
    def __init__(self):
        self._ensure_kb()
        self.knowledge = self._load_kb()
        self.code_index = self._load_code_index()

    def _ensure_kb(self):
        os.makedirs(KB_DIR, exist_ok=True)
        if not os.path.exists(KB_FILE):
            with open(KB_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

    def _load_kb(self) -> List[Dict]:
        try:
            with open(KB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.error(f"Failed to load KB: {e}")
        return []

    def _save_kb(self):
        try:
            with open(KB_FILE, "w", encoding="utf-8") as f:
                json.dump(self.knowledge, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save KB: {e}")

    def _load_code_index(self) -> Dict:
        try:
            if not os.path.exists(CODE_INDEX_FILE):
                return {}
            with open(CODE_INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.error(f"Failed to load code index: {e}")
        return {}

    def _save_code_index(self):
        try:
            with open(CODE_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(self.code_index, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save code index: {e}")

    def _extract_keywords(self, text: str, max_keywords: int = 24) -> List[str]:
        words = re.findall(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{2,}", (text or "").lower())
        stop = {
            "the", "and", "for", "with", "this", "that", "from", "have", "help",
            "please", "will", "what", "when", "where", "your", "you", "are",
            "我", "你", "我們", "需要", "可以", "請", "幫我", "如何", "這個", "那個",
        }
        picked = []
        for w in words:
            if w in stop:
                continue
            picked.append(w)
            if len(picked) >= max_keywords:
                break
        return _uniq_keep_order(picked)

    def _is_duplicate(self, tip: str) -> Optional[Dict]:
        target = (tip or "").strip().lower()
        for entry in self.knowledge:
            if (entry.get("tip", "").strip().lower()) == target:
                return entry
        return None

    def stats(self) -> Dict:
        return {
            "success": True,
            "count": len(self.knowledge),
            "kb_file": KB_FILE,
        }

    def learn(
        self,
        keywords: Optional[List[str]],
        tip: str,
        context: str = "manual",
        source: str = "user",
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """Learn a new tip with dedupe and metadata."""
        tip = (tip or "").strip()
        if not tip:
            return {"success": False, "message": "空白內容無法記住。"}

        # Iron Dome safety: never ingest unsafe operational content into learned KB.
        try:
            from skills.evolution.skill_genesis import validate_skill_safety

            ok, violations = validate_skill_safety(tip)
            if not ok:
                return {
                    "success": False,
                    "created": False,
                    "message": "🛡️ 已拒絕學習：內容觸發鐵穹限制。",
                    "violations": violations,
                }
        except Exception as e:
            # If safety validator is unavailable, prefer fail-closed for external imports.
            if (context or "").startswith("toolsai-auto-skill"):
                return {
                    "success": False,
                    "created": False,
                    "message": f"🛡️ 已拒絕學習：鐵穹驗證不可用（{e}）。",
                    "violations": ["validator_unavailable"],
                }

        dup = self._is_duplicate(tip)
        if dup:
            return {
                "success": True,
                "created": False,
                "message": "🧠 這條經驗已存在，我已保留原本版本。",
                "entry": dup,
            }

        kw = _uniq_keep_order((keywords or []) + self._extract_keywords(tip))
        timestamp = datetime.now().isoformat()
        entry_id = hashlib.sha1(f"{tip}|{source}|{timestamp}".encode("utf-8")).hexdigest()[:16]
        entry = {
            "id": entry_id,
            "keywords": kw[:24],
            "tip": tip,
            "context": (context or "manual").strip(),
            "source": (source or "user").strip(),
            "timestamp": timestamp,
            "metadata": metadata or {},
        }
        self.knowledge.append(entry)
        self._save_kb()
        logger.info(f"🧠 Learned: {tip[:120]}")

        # Mirror learned tips into vector memory so CASPER can retrieve semantically.
        # codebase-ingest 存入獨立 source namespace，recall 時預設不搜尋，
        # 只有明確技術查詢才會帶 source_contains="codebase-ingest" 搜到。
        if AUTOSKILL_MIRROR_TO_VECTOR:
            try:
                from skills.memory.mem_bridge import remember

                _ctx = entry.get('context', '')
                _src_tag = entry.get('source', '')
                src = f"autoskill|ctx={_ctx}|source={_src_tag}|id={entry_id}"
                # codebase-ingest 加獨立 namespace prefix，不干擾一般 recall
                if _ctx == "codebase-ingest":
                    src = f"codebase-ingest|{src}"
                remember(tip, source=src)
            except Exception as e:
                logger.warning(f"Vector mirror skipped: {e}")

        return {
            "success": True,
            "created": True,
            "message": f"🧠 已記住新經驗（關鍵詞 {len(entry['keywords'])} 個）。",
            "entry": entry,
        }

    def teach(self, lesson: str, context: str = "user-teach", source: str = "user") -> Dict:
        lesson = (lesson or "").strip()
        if not lesson:
            return {"success": False, "message": "教學內容不可空白。"}
        keywords = self._extract_keywords(lesson)
        return self.learn(keywords, lesson, context=context, source=source)

    def mirror_knowledge_to_vector(self, context_prefix: str = "", max_items: int = 1200) -> Dict:
        """
        Backfill/mirror AutoSkill KB entries into Keeper vector memory.
        This is required for entries learned before we introduced vector mirroring.
        Uses metadata flags to avoid repeated inserts.
        """
        if not AUTOSKILL_MIRROR_TO_VECTOR:
            return {"success": True, "mirrored": 0, "skipped": 0, "message": "vector mirroring disabled"}

        prefix = (context_prefix or "").strip()
        mirrored = 0
        skipped = 0
        touched = False

        try:
            from skills.evolution.skill_genesis import validate_skill_safety
            from skills.memory.mem_bridge import remember
        except Exception as e:
            return {"success": False, "error": f"deps unavailable: {e}"}

        for entry in self.knowledge:
            if mirrored >= int(max_items):
                break
            ctx = str(entry.get("context", "") or "")
            if prefix and (not ctx.startswith(prefix)):
                continue
            tip = (entry.get("tip") or "").strip()
            if not tip:
                skipped += 1
                continue
            md = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
            if md.get("vector_mirrored") is True:
                skipped += 1
                continue
            ok, _violations = validate_skill_safety(tip)
            if not ok:
                md["vector_mirrored"] = False
                md["vector_mirror_error"] = "iron_dome_blocked"
                entry["metadata"] = md
                touched = True
                skipped += 1
                continue
            src = f"autoskill|ctx={ctx}|source={entry.get('source','')}|id={entry.get('id','')}"
            try:
                remember(tip, source=src)
                md["vector_mirrored"] = True
                md["vector_mirrored_at"] = datetime.now().isoformat()
                entry["metadata"] = md
                touched = True
                mirrored += 1
            except Exception:
                md["vector_mirrored"] = False
                md["vector_mirror_error"] = "remember_failed"
                entry["metadata"] = md
                touched = True
                skipped += 1

        if touched:
            self._save_kb()

        return {
            "success": True,
            "mirrored": mirrored,
            "skipped": skipped,
            "message": f"mirrored={mirrored} skipped={skipped}",
        }

    def learn_from_file(
        self,
        file_path: str,
        context: str = "user-file-teach",
        source: str = "user-file",
        max_lines: int = 200,
    ) -> Dict:
        raw_path = (file_path or "").strip()
        if not raw_path:
            return {"success": False, "message": "缺少檔案路徑。"}

        abs_path = os.path.abspath(raw_path)
        if not any(abs_path.startswith(root + os.sep) or abs_path == root for root in ALLOWED_READ_ROOTS):
            return {"success": False, "message": f"禁止讀取路徑: {abs_path}"}
        if not os.path.exists(abs_path):
            return {"success": False, "message": f"檔案不存在: {abs_path}"}
        if os.path.isdir(abs_path):
            return {"success": False, "message": f"需要檔案，不接受資料夾: {abs_path}"}

        learned = 0
        skipped = 0
        samples = []
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                for idx, line in enumerate(f, start=1):
                    if idx > max_lines:
                        break
                    text = line.strip()
                    if len(text) < 6 or text.startswith("#"):
                        skipped += 1
                        continue
                    res = self.learn(
                        keywords=self._extract_keywords(text),
                        tip=text,
                        context=context,
                        source=f"{source}:{abs_path}",
                        metadata={"line": idx, "file": abs_path},
                    )
                    if res.get("success") and res.get("created"):
                        learned += 1
                        if len(samples) < 5:
                            samples.append(text[:80])
                    else:
                        skipped += 1
        except Exception as e:
            return {"success": False, "message": f"讀取教學檔失敗: {e}"}

        return {
            "success": True,
            "message": f"📘 已從檔案學習 {learned} 條經驗（略過 {skipped} 行）。",
            "file": abs_path,
            "learned": learned,
            "skipped": skipped,
            "samples": samples,
        }

    def recall(self, context: str, limit: int = 5) -> List[str]:
        """Retrieve relevant tips based on keyword overlap."""
        context = (context or "").strip()
        context_lower = context.lower()
        query_terms = set(self._extract_keywords(context_lower, max_keywords=32))
        ranked = []

        for entry in self.knowledge:
            tip = (entry.get("tip") or "").strip()
            if not tip:
                continue
            keywords = [str(k).lower() for k in entry.get("keywords", []) if str(k).strip()]
            overlap = len(query_terms.intersection(set(keywords)))
            if overlap <= 0 and not any(k in context_lower for k in keywords[:5]):
                continue
            ranked.append((overlap, entry))

        ranked.sort(key=lambda x: (x[0], x[1].get("timestamp", "")), reverse=True)
        tips = [x[1].get("tip", "") for x in ranked[: max(1, limit)] if x[1].get("tip")]

        # Built-in fallback experience
        if "address already in use" in context_lower or ("port" in context_lower and "5002" in context_lower):
            tips.append("💡 [Self-Healing]: Port 5002 is busy. Use `skills.ops.process_cleaner.check_and_kill(5002)`.")
        if "invalid signature" in context_lower:
            tips.append("🔑 [Auth Error]: LINE Channel Secret is missing or invalid. Check `os.environ` or `config.json`.")
        if "405" in context_lower or "method not allowed" in context_lower:
            tips.append("🔁 [Network]: POST request rejected. Ensure server accepts POST on `/callback` and root if needed.")
        if "modulenotfounderror" in context_lower and "skills" in context_lower:
            tips.append("📦 [Import Error]: Ensure project root is inserted into `sys.path` before importing skills.")
        if "nameerror" in context_lower and "loop_counter" in context_lower:
            tips.append("🔄 [Logic Error]: Initialize `loop_counter = 0` before entering the loop.")
        if "expected hello command" in context_lower:
            tips.append("🤝 [RPC Protocol]: Server expects HELLO handshake; verify client/server version compatibility.")
        if "epipe" in context_lower and "electron" in context_lower:
            tips.append("⚡ [Electron Conflict]: Close conflicting Electron instance before starting manual RPC server.")

        return _uniq_keep_order(tips)[: max(1, limit)]

    def internalize_as_skill(
        self,
        skill_name: str = "",
        description: str = "",
        keywords: Optional[List[str]] = None,
        max_tips: int = 40,
        auto_activate: bool = True,
    ) -> Dict:
        """
        Materialize learned tips into a runnable skill folder under MAGI/skills.
        """
        selected = self.knowledge[:]
        filter_keywords = [k.lower().strip() for k in (keywords or []) if str(k).strip()]
        if filter_keywords:
            selected = [
                e for e in self.knowledge
                if any(fk in [str(k).lower() for k in e.get("keywords", [])] for fk in filter_keywords)
            ]
        selected = sorted(selected, key=lambda e: e.get("timestamp", ""), reverse=True)[: max(1, int(max_tips))]
        if not selected:
            return {"success": False, "message": "沒有可內化的知識，請先教我內容。"}

        slug = _slugify(skill_name or "casper-learned-skill")
        skill_dir = os.path.join(SKILLS_ROOT, slug)
        os.makedirs(skill_dir, exist_ok=True)

        skill_desc = (description or "Internalized skill generated from CASPER learned experiences.").strip()
        now = datetime.now().strftime("%Y-%m-%d")

        skill_md = f"""---
name: {slug}
description: {skill_desc}
author: CASPER-AUTOSKILL
created: {now}
---

# {slug}

This skill serves learned operational knowledge from CASPER's AutoSkill KB.

## Runtime Contract
- Execute with `python3 action.py --task "<user request>"`.
- Fallback invoke: `python3 action.py "<user request>"`.

## Examples
- `python3 action.py --task "line invalid signature"`
- `python3 action.py --task "port 5002 already in use"`

## Safety Constraints
- Read-only lookup and stdout output only.
- No destructive commands.
"""

        seed_path = os.path.join(skill_dir, "knowledge_seed.json")
        action_path = os.path.join(skill_dir, "action.py")
        skill_path = os.path.join(skill_dir, "SKILL.md")

        seed_payload = []
        for e in selected:
            seed_payload.append(
                {
                    "keywords": e.get("keywords", []),
                    "tip": e.get("tip", ""),
                    "source": e.get("source", ""),
                    "timestamp": e.get("timestamp", ""),
                }
            )

        action_code = """#!/usr/bin/env python3
import argparse
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_FILE = os.path.join(BASE_DIR, "knowledge_seed.json")

def _extract_terms(text):
    return set(re.findall(r"[a-zA-Z0-9_\\-\\u4e00-\\u9fff]{2,}", (text or "").lower()))

def main():
    parser = argparse.ArgumentParser(description="CASPER internalized skill responder")
    parser.add_argument("--task", default="", help="Task or question")
    parser.add_argument("task_fallback", nargs="*", help="Fallback task words")
    args = parser.parse_args()
    task = (args.task or " ".join(args.task_fallback)).strip()
    if not task:
        print("請提供問題：python3 action.py --task \\"<text>\\"")
        return 0

    if not os.path.exists(SEED_FILE):
        print("knowledge_seed.json not found")
        return 1

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed = json.load(f)
    terms = _extract_terms(task)
    ranked = []
    for item in seed:
        kws = set([str(k).lower() for k in item.get("keywords", []) if str(k).strip()])
        score = len(terms.intersection(kws))
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        print("目前沒有精準匹配的內化經驗，請再提供更多上下文。")
        return 0

    print("CASPER Internalized Tips:")
    for _, item in ranked[:5]:
        tip = (item.get("tip") or "").strip()
        if tip:
            print(f"- {tip}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
"""

        try:
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(skill_md)
            with open(seed_path, "w", encoding="utf-8") as f:
                json.dump(seed_payload, f, ensure_ascii=False, indent=2)
            with open(action_path, "w", encoding="utf-8") as f:
                f.write(action_code)
            os.chmod(action_path, 0o755)
        except Exception as e:
            return {"success": False, "message": f"寫入內化技能失敗: {e}"}

        activation = None
        if auto_activate:
            try:
                from skills.evolution.skill_genesis import _register_skill_tool_definition

                activation = _register_skill_tool_definition(slug, skill_desc)
            except Exception as e:
                activation = {"success": False, "error": str(e)}

        return {
            "success": True,
            "message": f"🧬 已內化為技能 `{slug}`（共 {len(seed_payload)} 條知識）。",
            "skill_folder": slug,
            "skill_path": skill_path,
            "action_path": action_path,
            "seed_path": seed_path,
            "activation": activation,
        }

    def _is_allowed_path(self, abs_path: str) -> bool:
        path = os.path.abspath(abs_path)
        for root in ALLOWED_READ_ROOTS:
            if path == root or path.startswith(root + os.sep):
                return True
        return False

    def _hash_file(self, file_path: str) -> str:
        h = hashlib.sha1()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _iter_python_files(self, root: str, max_files: int = 80) -> List[str]:
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in CODE_IGNORE_DIRS and not d.startswith(".")]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                found.append(full)
                if len(found) >= max_files:
                    return sorted(found)
        return sorted(found)

    def _extract_python_facts(self, file_path: str) -> Dict:
        result = {"module_doc": "", "imports": [], "functions": [], "classes": []}
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            root = ast.parse(source, filename=file_path)
            result["module_doc"] = (ast.get_docstring(root) or "").strip()[:400]
            imports = []
            for node in ast.walk(root):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias and alias.name:
                            imports.append(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module.split(".")[0])
                elif isinstance(node, ast.FunctionDef):
                    result["functions"].append(node.name)
                elif isinstance(node, ast.AsyncFunctionDef):
                    result["functions"].append(node.name)
                elif isinstance(node, ast.ClassDef):
                    result["classes"].append(node.name)
            result["imports"] = _uniq_keep_order(imports)[:20]
            result["functions"] = _uniq_keep_order(result["functions"])[:30]
            result["classes"] = _uniq_keep_order(result["classes"])[:20]
            return result
        except Exception as e:
            result["module_doc"] = f"parse_failed: {e}"
            return result

    def _write_code_wrapper_skill(
        self,
        source_file: str,
        source_root: str,
        skill_name: str,
        facts: Dict,
        auto_activate: bool = True,
    ) -> Dict:
        rel = os.path.relpath(source_file, source_root)
        rel_display = rel.replace("\\", "/")
        skill_dir = os.path.join(SKILLS_ROOT, skill_name)
        os.makedirs(skill_dir, exist_ok=True)

        skill_desc = (
            f"Functional skill generated from CODE file `{rel_display}`. "
            "Supports function-call mode, module execution mode, and self-test mode."
        )
        now = datetime.now().strftime("%Y-%m-%d")
        doc = (facts.get("module_doc") or "").strip()
        funcs = facts.get("functions") or []
        classes = facts.get("classes") or []
        imports = facts.get("imports") or []

        skill_md = f"""---
name: {skill_name}
description: {skill_desc}
author: CASPER-CODE-INGEST
created: {now}
---

# {skill_name}

Source file: `{source_file}`

## Module Summary
{doc or "No module docstring found."}

## Exposed Symbols
- Functions: {", ".join(funcs[:15]) if funcs else "n/a"}
- Classes: {", ".join(classes[:10]) if classes else "n/a"}
- Imports: {", ".join(imports[:12]) if imports else "n/a"}

        ## Runtime Contract
        - Metadata/help: `python3 action.py --task "summary"`
        - Function call: `python3 action.py --task "call <function> [json_args]"`
        - Module run: `python3 action.py --task "<task>" --execute 1`
        - Self test: `python3 action.py --task "self test"`

        ## Safety Constraints
        - Default mode is metadata/function-call.
        - Full module execution requires explicit `--execute 1`.
"""

        action_tpl = Template(
            r"""#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys

SOURCE_FILE = $SOURCE_FILE
SYMBOLS = {
    "functions": $FUNCS,
    "classes": $CLASSES,
    "imports": $IMPORTS,
}

# Small, safe mapping for auto-install attempts (best-effort).
_PIP_MAP = {
    "docx": "python-docx",
    "PyPDF2": "PyPDF2",
    "pdf2image": "pdf2image",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "bs4": "beautifulsoup4",
    "playwright": "playwright",
    "pyperclip": "pyperclip",
    "pymysql": "pymysql",
    "mysql": "mysql-connector-python",
    "mysql.connector": "mysql-connector-python",
    "PIL": "Pillow",
    "watchdog": "watchdog",
    "docx2txt": "docx2txt",
    "numpy": "numpy",
}

def _uniq(seq):
    out = []
    seen = set()
    for x in seq or []:
        s = (x or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out

def _candidate_pythons():
    candidates = [sys.executable]
    sys_py = "/usr/bin/python3"
    if os.path.exists(sys_py) and sys_py not in candidates:
        candidates.append(sys_py)
    extra = os.environ.get("MAGI_CODE_SKILL_PYTHONS", "")
    for item in (extra or "").split(","):
        item = item.strip()
        if item and item not in candidates and os.path.exists(item):
            candidates.append(item)
    return _uniq(candidates)[:4]

def _run_python(py, code, payload=None, timeout=60):
    try:
        r = subprocess.run(
            [py, "-c", code],
            input=(json.dumps(payload, ensure_ascii=False) if payload is not None else None),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"rc": r.returncode, "stdout": (r.stdout or ""), "stderr": (r.stderr or "")}
    except Exception as e:
        return {"rc": 1, "stdout": "", "stderr": str(e)}

def _pip_install(py, pkg):
    pkg = (pkg or "").strip()
    if not pkg:
        return {"ok": False, "error": "empty pkg"}
    try:
        r = subprocess.run([py, "-m", "pip", "install", "--user", pkg], capture_output=True, text=True, timeout=600)
        ok = (r.returncode == 0)
        return {"ok": ok, "stdout": (r.stdout or "")[-600:], "stderr": (r.stderr or "")[-600:]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def _missing_mod_from_error(err_text: str) -> str:
    s = (err_text or "").strip()
    key = "No module named"
    i = s.find(key)
    if i < 0:
        return ""
    q1 = s.find("'", i)
    if q1 < 0:
        return ""
    q2 = s.find("'", q1 + 1)
    if q2 < 0:
        return ""
    name = s[q1 + 1 : q2].strip()
    return name

def _import_test(py, auto_install=True):
    code = r'''
import importlib.util, json, sys
p = json.loads(sys.stdin.read() or '{}') or {}
src = p.get('source')
try:
  spec = importlib.util.spec_from_file_location('code_source_module', src)
  mod = importlib.util.module_from_spec(spec)
  sys.modules['code_source_module'] = mod
  spec.loader.exec_module(mod)
except SystemExit as e:
  print(json.dumps({"success": False, "error": f"SystemExit: {e.code}"}, ensure_ascii=False))
  raise SystemExit(2)
except BaseException as e:
  print(json.dumps({"success": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}, ensure_ascii=False))
  raise SystemExit(2)
print(json.dumps({"success": True}, ensure_ascii=False))
raise SystemExit(0)
'''.strip()
    r = _run_python(py, code, payload={"source": SOURCE_FILE}, timeout=35)
    try:
        out = (r.get("stdout") or "").strip()
        last = out.splitlines()[-1].strip() if out else ""
        data = json.loads(last or "{}")
    except Exception:
        data = {"success": False, "error": (r.get("stderr") or r.get("stdout") or "").strip()[:500]}
    ok = bool(data.get("success"))
    err = data.get("error", "")
    installed = []

    if ok or (not auto_install):
        return ok, err, installed

    # Best-effort auto-install based on the import error text, with a small retry loop.
    for _ in range(3):
        missing = _missing_mod_from_error(err)
        if not missing or missing == "_tkinter":
            break
        pkg = _PIP_MAP.get(missing) or _PIP_MAP.get((missing or "").split(".")[0])
        if not pkg:
            break
        if pkg in installed:
            break
        res = _pip_install(py, pkg)
        if not res.get("ok"):
            break
        installed.append(pkg)
        r2 = _run_python(py, code, payload={"source": SOURCE_FILE}, timeout=35)
        try:
            data2 = json.loads((r2.get("stdout") or "").strip() or "{}")
        except Exception:
            data2 = {"success": False, "error": (r2.get("stderr") or r2.get("stdout") or "").strip()[:500]}
        ok = bool(data2.get("success"))
        err = data2.get("error", "")
        if ok:
            break

    return ok, err, installed

def _choose_runtime_python():
    # Prefer an interpreter that can import the module, but do not hard-fail.
    candidates = _candidate_pythons()
    for py in candidates:
        ok, _err, _installed = _import_test(py, auto_install=True)
        if ok:
            return py
    return candidates[0] if candidates else sys.executable

def _self_test(auto_install=True):
    report = {
        "success": True,
        "mode": "self_test",
        "source_file": SOURCE_FILE,
        "python_candidates": _candidate_pythons(),
        "compile": {"ok": False, "error": ""},
        "imports": {"missing": [], "auto_installed": []},
        "import_tests": [],
        "runtime_python": "",
    }

    # 1) Syntax compile (fast, deterministic)
    try:
        import py_compile
        py_compile.compile(SOURCE_FILE, doraise=True)
        report["compile"]["ok"] = True
    except BaseException as e:
        report["compile"]["ok"] = False
        report["compile"]["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        report["success"] = False
        return report

    # 2) Best-effort missing import detection (non-fatal)
    missing = []
    try:
        import importlib.util as _iu
        for name in SYMBOLS.get("imports") or []:
            base = (name or "").split(".")[0].strip()
            if not base:
                continue
            if _iu.find_spec(base) is None:
                missing.append(name)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 851, exc_info=True)
    missing = _uniq(missing)
    report["imports"]["missing"] = missing

    # 3) Optional auto-install for known small deps (non-fatal)
    if auto_install and missing:
        py = sys.executable
        for mod in missing:
            pkg = _PIP_MAP.get(mod) or _PIP_MAP.get((mod or "").split(".")[0])
            if not pkg:
                continue
            res = _pip_install(py, pkg)
            if res.get("ok"):
                report["imports"]["auto_installed"].append(pkg)

    # 4) Import test matrix (non-fatal; used to pick best runtime python)
    for py in report["python_candidates"]:
        ok, err, installed = _import_test(py, auto_install=True)
        report["import_tests"].append({"python": py, "ok": ok, "error": err, "auto_installed": installed})

    report["runtime_python"] = _choose_runtime_python()
    return report

def main():
    parser = argparse.ArgumentParser(description="CODE functional skill for $REL_DISPLAY")
    parser.add_argument("--task", default="summary", help="task text")
    parser.add_argument("--execute", default="0", help="set 1/true to execute source file")
    args = parser.parse_args()

    execute = str(args.execute).strip().lower() in {"1", "true", "yes", "on"}
    task = (args.task or "").strip() or "summary"

    if not os.path.exists(SOURCE_FILE):
        print(json.dumps({"success": False, "error": f"source file missing: {SOURCE_FILE}"}, ensure_ascii=False))
        return 1

    # Metadata/help path (never executes source)
    if task in {"summary", "help", "list"} and not execute:
        print(json.dumps({
            "success": True,
            "mode": "metadata",
            "source_file": SOURCE_FILE,
            "task": task,
            "symbols": SYMBOLS,
            "next": "Use --task \"self test\" or --task \"call <function> [json_args]\"; module run requires --execute 1",
        }, ensure_ascii=False))
        return 0

    if task in {"self test", "selftest", "self_test"} and not execute:
        report = _self_test(auto_install=True)
        # IMPORTANT: CI smoke test expects exit code 0 when syntax is valid.
        # Only fail hard on missing source or syntax compile error.
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("compile", {}).get("ok") else 1

    if task.startswith("call "):
        parts = task.split(" ", 2)
        if len(parts) < 2:
            print(json.dumps({"success": False, "error": "missing function name"}, ensure_ascii=False))
            return 1
        fn_name = parts[1].strip()
        raw_args = parts[2].strip() if len(parts) > 2 else ""

        py = _choose_runtime_python()
        runner = r'''
import asyncio, importlib.util, inspect, json, sys
p = json.loads(sys.stdin.read() or '{}') or {}
source = p.get('source')
fn_name = p.get('fn')
raw = p.get('raw','')

def _decode_args(text):
  if not text or not str(text).strip():
    return (), {}
  t = str(text).strip()
  try:
    payload = json.loads(t)
  except Exception:
    return (t,), {}
  if isinstance(payload, dict):
    return (), payload
  if isinstance(payload, list):
    return tuple(payload), {}
  return (payload,), {}

try:
  spec = importlib.util.spec_from_file_location('code_source_module', source)
  mod = importlib.util.module_from_spec(spec)
  sys.modules['code_source_module'] = mod
  spec.loader.exec_module(mod)
  fn = getattr(mod, fn_name, None)
  if not callable(fn):
    print(json.dumps({"success": False, "error": f"function not found: {fn_name}"}, ensure_ascii=False))
    raise SystemExit(2)
  args, kwargs = _decode_args(raw)
  if inspect.iscoroutinefunction(fn):
    out = asyncio.run(fn(*args, **kwargs))
  else:
    out = fn(*args, **kwargs)
  print(json.dumps({"success": True, "function": fn_name, "result": out}, ensure_ascii=False))
  raise SystemExit(0)
except BaseException as e:
  print(json.dumps({"success": False, "error": f"{type(e).__name__}: {str(e)[:800]}"}, ensure_ascii=False))
  raise SystemExit(2)
'''.strip()
        r = _run_python(py, runner, payload={"source": SOURCE_FILE, "fn": fn_name, "raw": raw_args}, timeout=120)
        out = (r.get("stdout") or "").strip()
        if out:
            print(out[:4000])
        else:
            print(json.dumps({"success": False, "error": (r.get("stderr") or "call failed")[:800]}, ensure_ascii=False))
        return 0 if r.get("rc") == 0 else 1

    if not execute:
        print(json.dumps({
            "success": True,
            "mode": "metadata",
            "task": task,
            "hint": "set --execute 1 to run source module as script",
        }, ensure_ascii=False))
        return 0

    # Module run mode (best-effort, uses the best runtime python)
    py = _choose_runtime_python()
    cmds = [
        [py, SOURCE_FILE, "--task", task],
        [py, SOURCE_FILE, task],
        [py, SOURCE_FILE, "--help"],
    ]
    last = None
    for cmd in cmds:
        try:
            rr = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if rr.returncode == 0:
                print((rr.stdout or "").strip()[:4000])
                return 0
            last = (rr.stderr or rr.stdout or "").strip()[:600]
        except Exception as e:
            last = str(e)
    print(json.dumps({"success": False, "error": last or "execution failed"}, ensure_ascii=False))
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
"""
        )
        action_code = action_tpl.safe_substitute(
            REL_DISPLAY=rel_display,
            SOURCE_FILE=repr(source_file),
            FUNCS=repr(funcs),
            CLASSES=repr(classes),
            IMPORTS=repr(imports),
        )

        try:
            skill_path = os.path.join(skill_dir, "SKILL.md")
            action_path = os.path.join(skill_dir, "action.py")
            facts_path = os.path.join(skill_dir, "code_facts.json")
            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(skill_md)
            with open(action_path, "w", encoding="utf-8") as f:
                f.write(action_code)
            with open(facts_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "source_file": source_file,
                        "relative_path": rel_display,
                        "facts": facts,
                        "updated": datetime.now().isoformat(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.chmod(action_path, 0o755)
        except Exception as e:
            return {"success": False, "error": f"write skill failed: {e}"}

        # ── Syntax validation (py_compile fast check before registration) ──────
        ci_result: dict = {}
        try:
            import py_compile as _pyc
            _pyc.compile(action_path, doraise=True)
            ci_result["syntax_ok"] = True
        except Exception as _ce:
            ci_result["syntax_ok"] = False
            ci_result["syntax_error"] = str(_ce)
            logger.error("auto_skill: generated action.py has syntax error in %s: %s", skill_name, _ce)
            # Do NOT register a skill with broken syntax
            return {"success": False, "error": f"generated action.py syntax error: {_ce}",
                    "skill_folder": skill_name, "action_path": action_path, "ci": ci_result}

        # ── Full CI (safety + smoke run) via skill_genesis if available ──────────
        try:
            from skills.evolution.skill_genesis import run_skill_ci as _run_ci
            ci_result.update(_run_ci(skill_name, task="help", attempt_repair=False))
        except Exception as _e:
            logger.debug("auto_skill: skill_genesis CI skipped for %s: %s", skill_name, _e)

        activation = None
        if auto_activate:
            try:
                from skills.evolution.skill_genesis import _register_skill_tool_definition

                activation = _register_skill_tool_definition(skill_name, skill_desc)
            except Exception as e:
                activation = {"success": False, "error": str(e)}

        return {
            "success": True,
            "skill_folder": skill_name,
            "skill_path": os.path.join(skill_dir, "SKILL.md"),
            "action_path": os.path.join(skill_dir, "action.py"),
            "activation": activation,
            "ci": ci_result,
        }

    def internalize_codebase_as_skills(
        self,
        source_dir: str = MAGI_ROOT,
        max_files: int = 50,
        force: bool = False,
        auto_activate: bool = True,
        enable_release: bool = True,
        canary_percent: int = 20,
        promote_min_runs: int = 12,
        promote_max_failure_rate: float = 0.2,
    ) -> Dict:
        """
        Convert code folder Python files into indexed, functional skills.
        For updated skills: run CI and attach stable/canary release flow.
        Only changed files are re-internalized unless force=True.
        """
        raw = (source_dir or "").strip()
        if not raw:
            return {"success": False, "message": "缺少 source_dir。"}
        root = os.path.abspath(raw)
        if not self._is_allowed_path(root):
            return {"success": False, "message": f"禁止讀取路徑: {root}"}
        if not os.path.isdir(root):
            return {"success": False, "message": f"資料夾不存在: {root}"}

        files = self._iter_python_files(root, max_files=max(1, int(max_files)))
        if not files:
            return {"success": False, "message": f"找不到 Python 檔案: {root}"}

        created = []
        skipped = []
        learned_count = 0

        for path in files:
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                sha1 = self._hash_file(path)
            except Exception as e:
                skipped.append({"file": rel, "reason": str(e)})
                continue

            idx = self.code_index.get(rel, {})
            if (not force) and idx.get("sha1") == sha1:
                skipped.append({"file": rel, "reason": "unchanged"})
                continue

            facts = self._extract_python_facts(path)
            module_tip = f"[CODE內化] {rel} functions={len(facts.get('functions', []))} classes={len(facts.get('classes', []))}"
            res = self.learn(
                keywords=["code", "internalize", os.path.basename(path)] + (facts.get("functions") or [])[:6],
                tip=module_tip,
                context="codebase-ingest",
                source=f"code:{path}",
                metadata={"file": path, "sha1": sha1},
            )
            if res.get("success") and res.get("created"):
                learned_count += 1

            skill_name = _slugify(f"code-{rel.replace('/', '-').replace('.py', '')}", fallback="code-module")
            write = self._write_code_wrapper_skill(
                source_file=path,
                source_root=root,
                skill_name=skill_name,
                facts=facts,
                auto_activate=auto_activate,
            )
            if not write.get("success"):
                skipped.append({"file": rel, "reason": write.get("error", "write failed")})
                continue

            release = {}
            if enable_release:
                try:
                    from skills.evolution.skill_genesis import (
                        _snapshot_skill_version,
                        get_skill_release_state,
                        run_skill_ci,
                        set_stable_skill_version,
                        start_canary_release,
                    )

                    ci = run_skill_ci(skill_name, task=f"self test for {rel}", attempt_repair=True)
                    release["ci"] = ci
                    if ci.get("success"):
                        skill_dir = os.path.join(SKILLS_ROOT, skill_name)
                        snap = _snapshot_skill_version(skill_dir, reason=f"code_ingest_candidate:{rel}")
                        release["snapshot"] = snap
                        candidate_version = snap.get("version_id", "")
                        state = get_skill_release_state(skill_name)
                        stable_exists = bool((state.get("state") or {}).get("stable_version"))
                        if stable_exists and candidate_version:
                            canary = start_canary_release(
                                skill_name,
                                candidate_version,
                                canary_percent=max(1, min(100, int(canary_percent))),
                                min_runs=max(1, int(promote_min_runs)),
                                fail_threshold=3,
                                max_failure_rate=0.5,
                                auto_promote=True,
                                promote_min_runs=max(1, int(promote_min_runs)),
                                promote_max_failure_rate=float(max(0.0, min(1.0, promote_max_failure_rate))),
                            )
                            release["canary"] = canary
                        else:
                            stable = set_stable_skill_version(
                                skill_name,
                                version_id=candidate_version,
                                enforce=True,
                            )
                            release["stable"] = stable
                    else:
                        release["error"] = "ci_failed"
                except Exception as e:
                    release["error"] = str(e)

            self.code_index[rel] = {
                "sha1": sha1,
                "skill": skill_name,
                "updated": datetime.now().isoformat(),
            }
            created.append({"file": rel, "skill": skill_name, "release": release})

        self._save_code_index()
        return {
            "success": True,
            "message": f"🧬 CODE 內化完成：新增/更新 {len(created)} 個技能，略過 {len(skipped)}。",
            "source_dir": root,
            "scanned_files": len(files),
            "created_skills": len(created),
            "skipped_files": len(skipped),
            "learned_tips": learned_count,
            "items": created[:30],
            "skipped": skipped[:30],
            "release_enabled": bool(enable_release),
        }

    def _safe_load_json(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _learn_markdown_snippets(
        self,
        md_path: str,
        base_keywords: Optional[List[str]] = None,
        context: str = "external-knowledge",
        source_prefix: str = "external",
        max_lines: int = 80,
    ) -> int:
        if not os.path.exists(md_path) or os.path.isdir(md_path):
            return 0
        learned = 0
        try:
            with open(md_path, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, start=1):
                    if i > max_lines:
                        break
                    text = line.strip()
                    if len(text) < 8:
                        continue
                    if text.startswith("#") or text.startswith("```"):
                        continue
                    kws = _uniq_keep_order((base_keywords or []) + self._extract_keywords(text))
                    res = self.learn(
                        keywords=kws[:24],
                        tip=text[:500],
                        context=context,
                        source=f"{source_prefix}:{md_path}",
                        metadata={"file": md_path, "line": i},
                    )
                    if res.get("success") and res.get("created"):
                        learned += 1
        except Exception:
            return learned
        return learned

    def import_toolsai_auto_skill(
        self,
        repo_url: str = "https://github.com/Toolsai/auto-skill.git",
        local_path: str = "",
        cleanup: bool = True,
        notify_dc: bool = False,
    ) -> Dict:
        """
        Import Toolsai/auto-skill knowledge-base + experience into CASPER AutoSkill KB.
        """
        temp_dir = ""
        root = ""
        if local_path and os.path.isdir(local_path):
            root = os.path.abspath(local_path)
        else:
            try:
                temp_dir = tempfile.mkdtemp(prefix="toolsai_auto_skill_")
                cmd = ["git", "clone", "--depth", "1", repo_url, temp_dir]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    return {"success": False, "message": f"git clone failed: {(r.stderr or r.stdout)[:300]}"}
                root = temp_dir
            except Exception as e:
                return {"success": False, "message": f"clone failed: {e}"}

        if not root or not os.path.isdir(root):
            return {"success": False, "message": "repo path not found"}

        learned = 0
        imported_files = []
        skipped = []

        kb_root = os.path.join(root, "knowledge-base")
        exp_root = os.path.join(root, "experience")

        kb_index = self._safe_load_json(os.path.join(kb_root, "_index.json")) or {}
        categories = kb_index.get("categories", []) if isinstance(kb_index, dict) else []
        if isinstance(categories, list) and categories:
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                file_name = (cat.get("file") or "").strip()
                if not file_name:
                    continue
                path = os.path.join(kb_root, file_name)
                if not os.path.exists(path):
                    skipped.append({"file": file_name, "reason": "missing"})
                    continue
                keywords = [str(x) for x in (cat.get("keywords") or []) if str(x).strip()]
                count = self._learn_markdown_snippets(
                    path,
                    base_keywords=["toolsai", "auto-skill", "knowledge-base"] + keywords,
                    context="toolsai-auto-skill-kb",
                    source_prefix="toolsai-kb",
                    max_lines=120,
                )
                learned += count
                imported_files.append({"file": path, "learned": count})
        elif os.path.isdir(kb_root):
            for name in sorted(os.listdir(kb_root)):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(kb_root, name)
                count = self._learn_markdown_snippets(
                    path,
                    base_keywords=["toolsai", "auto-skill", "knowledge-base"],
                    context="toolsai-auto-skill-kb",
                    source_prefix="toolsai-kb",
                    max_lines=120,
                )
                learned += count
                imported_files.append({"file": path, "learned": count})

        exp_index = self._safe_load_json(os.path.join(exp_root, "_index.json")) or {}
        exp_entries = exp_index.get("skills", []) if isinstance(exp_index, dict) else []
        if not isinstance(exp_entries, list):
            exp_entries = []
        seen_exp = set()
        for entry in exp_entries:
            if not isinstance(entry, dict):
                continue
            file_name = (entry.get("file") or "").strip()
            if not file_name:
                continue
            path = os.path.join(exp_root, file_name)
            if not os.path.exists(path):
                skipped.append({"file": file_name, "reason": "missing"})
                continue
            if path in seen_exp:
                continue
            seen_exp.add(path)
            skill_id = (entry.get("skillId") or "").strip()
            keywords = [str(x) for x in (entry.get("keywords") or []) if str(x).strip()]
            count = self._learn_markdown_snippets(
                path,
                base_keywords=["toolsai", "auto-skill", "experience", skill_id] + keywords,
                context="toolsai-auto-skill-exp",
                source_prefix="toolsai-exp",
                max_lines=120,
            )
            learned += count
            imported_files.append({"file": path, "learned": count})

        # Fallback scan for experience markdown files (repo example has placeholder invalid index).
        if os.path.isdir(exp_root):
            for name in sorted(os.listdir(exp_root)):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(exp_root, name)
                if path in seen_exp:
                    continue
                count = self._learn_markdown_snippets(
                    path,
                    base_keywords=["toolsai", "auto-skill", "experience"],
                    context="toolsai-auto-skill-exp",
                    source_prefix="toolsai-exp",
                    max_lines=120,
                )
                learned += count
                imported_files.append({"file": path, "learned": count})

        if cleanup and temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1371, exc_info=True)

        result = {
            "success": True,
            "message": f"已導入 Toolsai auto-skill 經驗，共新增 {learned} 條知識。",
            "repo": repo_url,
            "root": root,
            "learned": learned,
            "imported_files": imported_files[:40],
            "skipped": skipped[:20],
        }
        # Ensure CASPER can semantically retrieve the imported knowledge even if it was learned earlier.
        try:
            mirror = self.mirror_knowledge_to_vector(context_prefix="toolsai-auto-skill", max_items=2400)
            result["vector_mirror"] = mirror
        except Exception as e:
            result["vector_mirror"] = {"success": False, "error": str(e)}
        if notify_dc:
            try:
                from skills.ops.red_phone import alert_admin

                summary = (
                    "Auto-Skill Daily Import Summary\n"
                    f"repo: {repo_url}\n"
                    f"learned: {learned}\n"
                    f"files: {len(imported_files)}\n"
                    f"skipped: {len(skipped)}\n"
                    f"time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                notify = alert_admin(summary, severity="info", topic_key="nightly")
                result["dc_notify"] = notify
            except Exception as e:
                result["dc_notify"] = {"line": False, "discord": False, "error": str(e)}
        return result
