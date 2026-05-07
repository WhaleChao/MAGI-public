"""
MAGI Skill Plugin System
========================
可擴展的技能插件框架，支援：
- In-process Python 技能（無 subprocess 開銷）
- 自動從 SKILL.md frontmatter 發現技能
- 宣告式路由（keywords/pattern 取代硬編碼）
- 與現有 subprocess 技能完全相容

用法：
    from skills.plugin import SkillPlugin, skill_registry

    class JudgmentPlugin(SkillPlugin):
        name = "judgment_search"
        description = "查判決"
        keywords = ["查判決", "判決搜尋"]

        def execute(self, message, **ctx):
            return "判決結果..."

        def capability_guide(self):
            return "✅ 我可以幫您查判決！"

    skill_registry.register_plugin(JudgmentPlugin())
"""

from __future__ import annotations

import json
import logging
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from skills.catalog import iter_top_level_skill_dirs

logger = logging.getLogger("SkillPlugin")


# ── Skill metadata (parsed from SKILL.md or definitions.json) ─────────

@dataclass
class SkillMeta:
    """Unified skill metadata from any source."""
    name: str
    folder: str  # directory name under skills/
    description: str = ""
    sage: str = "casper"
    version: str = "1.0"
    author: str = ""
    keywords: list[str] = field(default_factory=list)
    has_action_py: bool = False
    source: str = "magi"  # magi | openclaw
    dispatch_mode: str = "subprocess"  # subprocess | plugin | direct


# ── Base plugin class ─────────────────────────────────────────────────

class SkillPlugin:
    """
    Base class for in-process skill plugins.

    Subclass and implement execute() to create a skill that runs
    in the orchestrator process without subprocess overhead.
    """
    name: str = ""
    description: str = ""
    keywords: list[str] = []
    pattern: str = ""  # regex pattern for matching
    admin_only: bool = False
    priority: int = 100  # lower = checked first

    def execute(self, message: str, *, user_id: str = "",
                role: str = "", platform: str = "",
                orchestrator: object = None) -> Optional[str]:
        """
        Execute this skill. Return response text, or None if not handled.
        Override in subclass.
        """
        raise NotImplementedError

    def capability_guide(self) -> Optional[str]:
        """
        Return a user-friendly guide for this skill's capabilities.
        Shown when user asks "你會什麼" or similar.
        Return None to skip.
        """
        return None

    def health_check(self) -> bool:
        """Optional health check. Return True if skill is operational."""
        return True


# ── Skill Registry ────────────────────────────────────────────────────

class SkillRegistry:
    """
    Central registry for all skills (plugin + subprocess + direct handler).

    Responsibilities:
    - Auto-discover skills from SKILL.md frontmatter
    - Register in-process SkillPlugin instances
    - Register direct handler functions (replaces hardcoded dict)
    - Unified dispatch: plugin → direct handler → subprocess fallback
    """

    def __init__(self, skills_dirs: Optional[list[str]] = None):
        self._plugins: dict[str, SkillPlugin] = {}
        self._direct_handlers: dict[str, callable] = {}
        self._capability_guides: dict[str, str] = {}
        self._skill_meta: dict[str, SkillMeta] = {}
        self._compiled_patterns: dict[str, re.Pattern] = {}
        self._skills_dirs = skills_dirs or [f"{_MAGI_ROOT}/skills"]
        self._discovered = False

    # ── Registration ──────────────────────────────────────────────

    def register_plugin(self, plugin: SkillPlugin) -> None:
        """Register an in-process skill plugin."""
        name = plugin.name
        if not name:
            raise ValueError("SkillPlugin.name must be set")
        self._plugins[name] = plugin
        if plugin.pattern:
            self._compiled_patterns[name] = re.compile(plugin.pattern, re.IGNORECASE)
        if plugin.capability_guide():
            self._capability_guides[name] = plugin.capability_guide()
        logger.info("Registered plugin: %s", name)

    def register_handler(self, skill_name: str, handler: callable,
                         capability_guide: Optional[str] = None,
                         aliases: Optional[list[str]] = None) -> None:
        """
        Register a direct handler function for a skill.
        Replaces hardcoded direct_handlers dict in orchestrator.
        """
        self._direct_handlers[skill_name] = handler
        if capability_guide:
            self._capability_guides[skill_name] = capability_guide
        for alias in (aliases or []):
            self._direct_handlers[alias] = handler
            if capability_guide:
                self._capability_guides[alias] = capability_guide

    def register_capability_guide(self, skill_name: str, guide: str,
                                  aliases: Optional[list[str]] = None) -> None:
        """Register a capability guide string for a skill."""
        self._capability_guides[skill_name] = guide
        for alias in (aliases or []):
            self._capability_guides[alias] = guide

    @staticmethod
    def _get_hook_bus(orchestrator: object):
        ensure = getattr(orchestrator, "_ensure_runtime_foundations", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:
                logger.debug("runtime foundation bootstrap failed", exc_info=True)
        return getattr(orchestrator, "_hook_bus", None)

    @staticmethod
    def _get_permission_enforcer(orchestrator: object):
        ensure = getattr(orchestrator, "_ensure_runtime_foundations", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:
                logger.debug("runtime foundation bootstrap failed", exc_info=True)
        return getattr(orchestrator, "_permission_enforcer", None)

    @staticmethod
    def _get_correlation_id(orchestrator: object) -> str:
        getter = getattr(orchestrator, "_current_correlation_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:
                logger.debug("correlation lookup failed", exc_info=True)
        return ""

    def _emit_pre_dispatch(
        self,
        skill_name: str,
        *,
        dispatch_mode: str,
        message: str,
        user_id: str,
        platform: str,
        orchestrator: object,
    ) -> None:
        hook_bus = self._get_hook_bus(orchestrator)
        if hook_bus is None:
            return
        hook_bus.pre_tool(
            f"skill:{skill_name}",
            input_data={"message_preview": (message or "")[:200]},
            user_id=str(user_id or ""),
            platform=str(platform or ""),
            correlation_id=self._get_correlation_id(orchestrator),
            metadata={"dispatch_mode": dispatch_mode, "skill_name": skill_name},
        )

    def _emit_post_dispatch(
        self,
        skill_name: str,
        *,
        dispatch_mode: str,
        orchestrator: object,
        started_at: float,
        ok: bool,
        status: str,
        output_data=None,
        error: str = "",
    ) -> None:
        hook_bus = self._get_hook_bus(orchestrator)
        if hook_bus is None:
            return
        hook_bus.post_tool(
            f"skill:{skill_name}",
            output_data=output_data,
            ok=ok,
            status=status,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=str(error or ""),
            correlation_id=self._get_correlation_id(orchestrator),
            metadata={"dispatch_mode": dispatch_mode, "skill_name": skill_name},
        )

    def _check_dispatch_permission(
        self,
        skill_name: str,
        *,
        action_path: str = "",
        orchestrator: object = None,
    ) -> tuple[bool, str]:
        enforcer = self._get_permission_enforcer(orchestrator)
        if enforcer is None:
            return True, ""
        command_decision = enforcer.evaluate_command(f"skill:{skill_name}")
        if not command_decision.allowed:
            return False, f"⚠️ 權限策略已阻擋技能執行：{command_decision.reason}"
        if action_path:
            path_decision = enforcer.evaluate_path(action_path)
            if not path_decision.allowed:
                return False, f"⚠️ 權限策略已阻擋技能執行：{path_decision.reason}"
        return True, ""

    # ── Discovery ─────────────────────────────────────────────────

    def discover(self, force: bool = False) -> int:
        """
        Walk skills directories, parse SKILL.md frontmatter,
        build unified skill metadata registry.
        Returns count of skills discovered.
        """
        if self._discovered and not force:
            return len(self._skill_meta)

        count = 0
        for skills_dir in self._skills_dirs:
            if not os.path.isdir(skills_dir):
                continue
            source = "openclaw" if "openclaw" in skills_dir else "magi"
            try:
                for entry in iter_top_level_skill_dirs(skills_dir):
                    folder = entry.name
                    skill_md = os.path.join(str(entry), "SKILL.md")
                    meta = self._parse_skill_md(skill_md, folder, source)
                    if meta:
                        # Check dispatch mode
                        if meta.name in self._plugins:
                            meta.dispatch_mode = "plugin"
                        elif meta.name in self._direct_handlers:
                            meta.dispatch_mode = "direct"
                        self._skill_meta[meta.name] = meta
                        count += 1
            except Exception as e:
                logger.warning("Error scanning %s: %s", skills_dir, e)

        self._discovered = True
        logger.info("Discovered %d skills across %d directories", count, len(self._skills_dirs))
        return count

    def _parse_skill_md(self, path: str, folder: str, source: str) -> Optional[SkillMeta]:
        """Parse SKILL.md YAML frontmatter without PyYAML dependency."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read(4096)  # frontmatter is always near top
        except Exception:
            return None

        name = folder
        description = ""
        sage = "casper"
        version = "1.0"
        author = ""
        keywords = []

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    line = line.strip()
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("author:"):
                        author = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("version:") or line.startswith('  version:'):
                        version = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("sage:") or line.startswith('  sage:'):
                        sage = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("keywords:"):
                        # Handle inline list: keywords: [查判決, 判決搜尋]
                        val = line.split(":", 1)[1].strip()
                        if val.startswith("["):
                            keywords = [k.strip().strip("'\"") for k in val.strip("[]").split(",") if k.strip()]

        has_action = os.path.exists(os.path.join(os.path.dirname(path), "action.py"))

        return SkillMeta(
            name=name,
            folder=folder,
            description=description,
            sage=sage,
            version=version,
            author=author,
            keywords=keywords,
            has_action_py=has_action,
            source=source,
        )

    # ── Dispatch ──────────────────────────────────────────────────

    def dispatch(self, skill_name: str, message: str, *,
                 user_id: str = "", role: str = "",
                 platform: str = "", orchestrator: object = None,
                 ) -> tuple[bool, str]:
        """
        Unified dispatch: plugin → direct handler → subprocess fallback.
        Returns (handled, response_text).
        """
        # 1. Try registered plugin
        plugin = self._plugins.get(skill_name)
        if plugin:
            started = time.perf_counter()
            self._emit_pre_dispatch(
                skill_name,
                dispatch_mode="plugin",
                message=message,
                user_id=user_id,
                platform=platform,
                orchestrator=orchestrator,
            )
            allowed, blocked_reason = self._check_dispatch_permission(
                skill_name,
                orchestrator=orchestrator,
            )
            if not allowed:
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="plugin",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="denied",
                    error=blocked_reason,
                )
                return True, blocked_reason
            try:
                result = plugin.execute(
                    message, user_id=user_id, role=role,
                    platform=platform, orchestrator=orchestrator,
                )
                if result is not None:
                    self._emit_post_dispatch(
                        skill_name,
                        dispatch_mode="plugin",
                        orchestrator=orchestrator,
                        started_at=started,
                        ok=True,
                        status="handled",
                        output_data=result,
                    )
                    return True, result
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="plugin",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="not_handled",
                )
            except Exception as e:
                logger.error("Plugin '%s' failed: %s", skill_name, e)
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="plugin",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="error",
                    error=str(e),
                )
                return True, f"⚠️ 技能執行失敗：{e}"

        # 2. Try registered direct handler
        handler = self._direct_handlers.get(skill_name)
        if handler:
            started = time.perf_counter()
            self._emit_pre_dispatch(
                skill_name,
                dispatch_mode="direct",
                message=message,
                user_id=user_id,
                platform=platform,
                orchestrator=orchestrator,
            )
            allowed, blocked_reason = self._check_dispatch_permission(
                skill_name,
                orchestrator=orchestrator,
            )
            if not allowed:
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="direct",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="denied",
                    error=blocked_reason,
                )
                return True, blocked_reason
            try:
                result = handler()
                if result:
                    self._emit_post_dispatch(
                        skill_name,
                        dispatch_mode="direct",
                        orchestrator=orchestrator,
                        started_at=started,
                        ok=True,
                        status="handled",
                        output_data=result,
                    )
                    return True, result
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="direct",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="not_handled",
                )
            except Exception as e:
                logger.warning("Direct handler '%s' failed: %s", skill_name, e)
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="direct",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=False,
                    status="error",
                    error=str(e),
                )
                return False, ""

        # 3. Subprocess fallback (via skill_genesis)
        return self._subprocess_dispatch(
            skill_name,
            message,
            user_id=user_id,
            platform=platform,
            orchestrator=orchestrator,
        )

    def get_capability_guide(self, skill_name: str) -> Optional[str]:
        """Get capability guide for a skill (if registered)."""
        return self._capability_guides.get(skill_name)

    def match_by_keywords(self, message_lower: str) -> Optional[str]:
        """
        Try to match a message to a skill by keywords/patterns.
        Returns skill name or None.
        """
        # Check plugin keywords
        for name, plugin in self._plugins.items():
            if plugin.keywords:
                for kw in plugin.keywords:
                    if kw in message_lower:
                        return name
            if name in self._compiled_patterns:
                if self._compiled_patterns[name].search(message_lower):
                    return name

        # Check SKILL.md keywords
        for name, meta in self._skill_meta.items():
            if meta.keywords:
                for kw in meta.keywords:
                    if kw in message_lower:
                        return name
        return None

    # ── Subprocess fallback ───────────────────────────────────────

    def _subprocess_dispatch(
        self,
        skill_name: str,
        message: str,
        *,
        user_id: str = "",
        platform: str = "",
        orchestrator: object = None,
    ) -> tuple[bool, str]:
        """Run skill via action.py subprocess (backward compat)."""
        try:
            from skills.evolution.skill_genesis import run_skill_action
        except ImportError:
            return False, ""

        # Resolve folder name from skill_name
        folder = self._resolve_folder(skill_name)
        if not folder:
            return False, ""

        started = time.perf_counter()
        self._emit_pre_dispatch(
            skill_name,
            dispatch_mode="subprocess",
            message=message,
            user_id=user_id,
            platform=platform,
            orchestrator=orchestrator,
        )
        action_path = self._resolve_action_path(folder)
        allowed, blocked_reason = self._check_dispatch_permission(
            skill_name,
            action_path=action_path,
            orchestrator=orchestrator,
        )
        if not allowed:
            self._emit_post_dispatch(
                skill_name,
                dispatch_mode="subprocess",
                orchestrator=orchestrator,
                started_at=started,
                ok=False,
                status="denied",
                error=blocked_reason,
            )
            return True, blocked_reason

        logger.info("Subprocess dispatch: %s → %s", skill_name, folder)
        try:
            result = run_skill_action(
                folder, message,
                timeout_sec=60,
                auto_repair=False,
                auto_install_deps=True,
            )
            if result.get("success"):
                output = (result.get("output") or "").strip()
                self._emit_post_dispatch(
                    skill_name,
                    dispatch_mode="subprocess",
                    orchestrator=orchestrator,
                    started_at=started,
                    ok=True,
                    status="handled",
                    output_data=output or "✅ 技能執行完成。",
                )
                return True, output or "✅ 技能執行完成。"
            self._emit_post_dispatch(
                skill_name,
                dispatch_mode="subprocess",
                orchestrator=orchestrator,
                started_at=started,
                ok=False,
                status="not_handled",
                error=str(result.get("error") or ""),
            )
            return False, ""
        except Exception as e:
            logger.warning("Subprocess dispatch error for %s: %s", skill_name, e)
            self._emit_post_dispatch(
                skill_name,
                dispatch_mode="subprocess",
                orchestrator=orchestrator,
                started_at=started,
                ok=False,
                status="error",
                error=str(e),
            )
            return False, ""

    def _resolve_folder(self, skill_name: str) -> Optional[str]:
        """Resolve skill name to folder name."""
        # Check registered meta
        meta = self._skill_meta.get(skill_name)
        if meta and meta.has_action_py:
            return meta.folder

        # Try name variants
        candidates = [
            skill_name.replace("_", "-"),
            skill_name,
            re.sub(r"^run[_-]+", "", skill_name.replace("_", "-")),
            re.sub(r"^run[_-]+", "", skill_name),
        ]
        for skills_dir in self._skills_dirs:
            for candidate in candidates:
                d = os.path.join(skills_dir, candidate)
                if os.path.isdir(d) and os.path.exists(os.path.join(d, "action.py")):
                    return candidate
        return None

    def _resolve_action_path(self, folder: str) -> str:
        for skills_dir in self._skills_dirs:
            action_path = os.path.join(skills_dir, folder, "action.py")
            if os.path.exists(action_path):
                return action_path
        return ""

    # ── Introspection ─────────────────────────────────────────────

    def list_skills(self) -> list[dict]:
        """Return all discovered skills for display."""
        self.discover()
        return [
            {
                "name": m.name,
                "folder": m.folder,
                "description": m.description[:80] + ("..." if len(m.description) > 80 else ""),
                "dispatch_mode": m.dispatch_mode,
                "has_action_py": m.has_action_py,
                "source": m.source,
            }
            for m in sorted(self._skill_meta.values(), key=lambda m: m.name)
        ]

    def generate_definitions(self) -> dict:
        """
        Auto-generate definitions.json content from discovered skills.
        Merges with existing definitions.json entries for backward compat.
        """
        self.discover()
        # Load existing definitions for tool entries we can't auto-generate
        existing_tools = {}
        for skills_dir in self._skills_dirs:
            defs_path = os.path.join(skills_dir, "definitions.json")
            if os.path.exists(defs_path):
                try:
                    with open(defs_path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                    for tool in data.get("tools") or []:
                        existing_tools[tool.get("name", "")] = tool
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 395, exc_info=True)

        tools = []
        for meta in sorted(self._skill_meta.values(), key=lambda m: m.name):
            if meta.name in existing_tools:
                tools.append(existing_tools[meta.name])
            elif meta.has_action_py:
                tools.append({
                    "name": meta.name,
                    "description": meta.description,
                    "sage": meta.sage,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill": {
                                "type": "string",
                                "default": meta.folder,
                                "enum": [meta.folder],
                            },
                            "task": {
                                "type": "string",
                                "description": "Task to execute",
                            },
                        },
                    },
                })

        return {
            "_meta": {
                "version": "2.0.0",
                "description": "Auto-generated by SkillRegistry",
                "skills_count": len(tools),
            },
            "tools": tools,
        }

    @property
    def plugin_count(self) -> int:
        return len(self._plugins)

    @property
    def handler_count(self) -> int:
        return len(self._direct_handlers)

    @property
    def total_count(self) -> int:
        self.discover()
        return len(self._skill_meta)


# ── Module-level singleton ────────────────────────────────────────────

skill_registry = SkillRegistry()
