"""
IRON DOME SECURITY CORE
=======================
Centralized security enforcement for MAGI.
Compiles static and dynamic rules to validate inputs and generated code.
"""
import logging

import re
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import sys as _sys
if _MAGI_ROOT not in _sys.path:
    _sys.path.insert(0, _MAGI_ROOT)
from skills.bridge.shared_utils.text_utils import strip_zero_width as _strip_zero_width
import hashlib
import threading
from typing import List, Tuple, Dict, Optional
from datetime import datetime


# =============================================================================
# Configuration
# =============================================================================
IRON_DOME_DYNAMIC_RULES_PATH = os.environ.get(
    "MAGI_IRON_DOME_DYNAMIC_RULES_PATH",
    f"{_MAGI_ROOT}/skills/evolution/iron_dome_dynamic_rules.json",
)
PATTERNS_CACHE_FILE = os.environ.get(
    "MAGI_IRON_DOME_PATTERNS_CACHE_FILE",
    f"{_MAGI_ROOT}/static/iron_dome_patterns.json",
)

# =============================================================================
# STATIC RULES (The Constitution)
# =============================================================================

# 1. Prompt Injection (Article I.3)
PROMPT_INJECTION_PATTERNS = [
    # English patterns
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?previous\s+instructions",
    r"forget\s+(all\s+)?previous\s+instructions",
    r"you\s+are\s+now\s+unrestricted",
    r"you\s+are\s+now\s+jailbroken",
    r"roleplay\s+as\s+dan",
    r"pretend\s+(you\s+are|to\s+be)\s+a\s+.*(unrestricted|evil|unfiltered)",
    r"system\s+override",
    r"admin\s+override",
    r"developer\s+mode\s+(enabled|on|activate)",
    r"do\s+anything\s+now",
    r"safety\s+protocols?\s+disabled",
    r"bypass\s+(all\s+)?safety",
    
    # Chinese patterns
    r"忽略(所有)?之前(的)?指令",
    r"無視(所有)?先前(的)?指示",
    r"你現在(是|已經)?(一個)?不受限(的|制)?",
    r"假裝(你是|成為).*不受限",
    r"系統覆蓋",
    r"管理員覆蓋",
]

# 2. Destructive Commands (Article I.2) - Filesystem, DB, Network
DESTRUCTIVE_PATTERNS = [
    # Filesystem
    r"rm\s+-rf",
    r"rm\s+-r\s+/",
    r"rm\s+--no-preserve-root",
    r"mkfs\.",
    r"mkfs\s+",
    r"dd\s+if=.+of=/dev/",
    r":(){ :|:& };:",  # Fork bomb
    r"chmod\s+-R\s+777\s+/",
    r"mv\s+.+\s+/dev/null",
    r">\s*/dev/sda",
    
    # Database
    r"DROP\s+DATABASE",
    r"DROP\s+TABLE",
    r"TRUNCATE\s+TABLE",
    r"DELETE\s+FROM\s+\w+\s*(;|$)",  # DELETE without WHERE check
    
    # Execution / Injection
    r"eval\s*\(",
    r"exec\s*\(",
    r"os\.system\s*\(",
    r"subprocess\.call",
    r"subprocess\.Popen",
    
    # Network / Reverse Shell
    r"nc\s+-e\s+/bin/",
    r"bash\s+-i\s+>&\s*/dev/tcp",
    r"/dev/tcp/",
    r"python\s+-c\s+.+socket",
]

# 3. Sensitive Data (Secrets)
SENSITIVE_PATTERNS = [
    r"password\s*=\s*['\"][^'\"]+['\"]",
    r"api_key\s*=\s*['\"][^'\"]+['\"]",
    r"secret\s*=\s*['\"][^'\"]+['\"]",
    r"token\s*=\s*['\"][^'\"]+['\"]",
    r"AWS_SECRET",
    r"PRIVATE_KEY",
]

# 4. Supply Chain Attack (Article I.4) — npm / pip 供應鏈攻擊防護
#    Detects known malicious packages, suspicious install flags, post-install
#    dropper patterns, invisible Unicode obfuscation, and RAT C2 signatures.
SUPPLY_CHAIN_PATTERNS = [
    # --- Known malicious npm packages (2026-03 axios incident + typosquats) ---
    r"plain-crypto-js",                            # RAT dropper used in axios@1.14.1
    r"(?:npm|yarn|pnpm)\s+(?:install|add|i)\s+.*axios@(?:1\.14\.1|0\.30\.4)\b",
    r"(?:npm|yarn|pnpm)\s+(?:install|add|i)\s+.*(?:plain-crypto-js|crypto-js-esm|cryptojs-esm)",
    # --- Suspicious npm install patterns ---
    r"npm\s+install\s+--ignore-scripts\s*=\s*false",
    r"(?:npm|yarn|pnpm)\s+(?:install|add|i)\s+.*--registry\s+http://",
    r"npm\s+set\s+registry\s+http://",
    # --- Suspicious pip install patterns ---
    r"pip\s+install\s+.*--index-url\s+http://",
    r"pip\s+install\s+.*--extra-index-url\s+http://",
    r"pip\s+install\s+.*--trusted-host\s+",
    # --- Post-install RAT dropper signatures ---
    r"\"(?:pre|post)install\"\s*:\s*\"[^\"]*(?:curl|wget|powershell|certutil)\b",
    r"\"(?:pre|post)install\"\s*:\s*\"[^\"]*(?:bash|sh|cmd|node)\s+-[ec]",
    r"child_process.*\.\s*(?:exec|spawn)\s*\(\s*['\"](?:curl|wget|bash|sh|powershell)",
    r"os\.(?:system|popen)\s*\(\s*['\"](?:curl|wget|bash|sh)\s",
    # --- Package manifest tampering ---
    r"\"resolved\"\s*:\s*\"http://(?!registry\.npmjs\.org)",
    r"\"integrity\"\s*:\s*\"sha1-",
    # --- Axios RAT C2 IOCs (2026-03-31 incident) ---
    r"sfrclak\.com",                               # axios RAT C2 domain
    r"packages\.npm\.org/product[012]",            # axios RAT payload download path
    r"mozilla/4\.0\s*\(compatible;\s*msie\s*8\.0;\s*windows\s*nt\s*5\.1",  # Fake IE8/WinXP User-Agent
    # --- Invisible Unicode obfuscation (GlassWorm campaign) ---
    r"[\uFE00-\uFE0F]{3,}",                       # 3+ consecutive variation selectors
    r"[\U000E0100-\U000E01EF]{3,}",               # 3+ consecutive tag characters (Private Use Area)
    r"String\.fromCharCode\s*\([^)]*0x[eE]01",    # JS decode of Unicode PUA chars
    r"\\u[eE]0[0-9a-fA-F]{2}.*\\u[eE]0[0-9a-fA-F]{2}",  # Encoded PUA sequence in strings
    # --- Node.js spawning suspicious child processes (RAT dropper behavior) ---
    r"require\s*\(\s*['\"]child_process['\"]\s*\).*(?:curl|wget|osascript|cscript|python3?\s+-c)",
]

# Combine all static patterns map
STATIC_RULE_SETS = {
    "PROMPT_INJECTION": PROMPT_INJECTION_PATTERNS,
    "DESTRUCTIVE_COMMAND": DESTRUCTIVE_PATTERNS,
    "SENSITIVE_DATA": SENSITIVE_PATTERNS,
    "SUPPLY_CHAIN": SUPPLY_CHAIN_PATTERNS,
}

# =============================================================================
# State & Cache
# =============================================================================
_PATTERN_CACHE_MTIME = 0.0
_COMPILED_REGEXES = {}  # type: Dict[str, re.Pattern]
_RELOAD_LOCK = threading.Lock()


def _compile_regexes(dynamic_patterns: List[dict] = None) -> None:
    """Compiles all static and dynamic rules into regexes (skip invalid patterns)."""
    global _COMPILED_REGEXES

    def _filter_valid(patterns: List[str]) -> List[str]:
        out = []
        for p in patterns or []:
            try:
                re.compile(p, re.IGNORECASE | re.UNICODE)
                out.append(p)
            except Exception:
                # Skip invalid regex to avoid breaking the whole rule set.
                continue
        return out

    new_regexes = {}

    # Base: Static Rules
    for category, patterns in STATIC_RULE_SETS.items():
        valid = _filter_valid(patterns or [])
        if valid:
            new_regexes[category] = re.compile("|".join(valid), re.IGNORECASE | re.UNICODE)

    # Add: Dynamic Rules (merged into categories or separate)
    # Dynamic rules usually go into DESTRUCTIVE_COMMAND or specialized dynamic category
    # For now, we keep them as "DYNAMIC_RULE" check.
    if dynamic_patterns:
        valid_pats = _filter_valid([p["pattern"] for p in dynamic_patterns if p.get("enabled", True) and p.get("pattern")])
        if valid_pats:
            new_regexes["DYNAMIC_RULE"] = re.compile("|".join(valid_pats), re.IGNORECASE | re.UNICODE)

    _COMPILED_REGEXES = new_regexes


def _load_dynamic_state() -> dict:
    if not os.path.exists(IRON_DOME_DYNAMIC_RULES_PATH):
        return {"patterns": []}
    try:
        with open(IRON_DOME_DYNAMIC_RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"patterns": []}


def _reload_patterns(force: bool = False) -> bool:
    """Reloads patterns from disk if changed."""
    global _PATTERN_CACHE_MTIME
    
    # Check cache file (synced from other nodes)
    cache_path = PATTERNS_CACHE_FILE
    has_cache = os.path.exists(cache_path)
    
    if has_cache:
        try:
            st = os.stat(cache_path)
            if not force and st.st_mtime <= _PATTERN_CACHE_MTIME:
                return False
            
            with _RELOAD_LOCK:
                # Double check
                if not force and st.st_mtime <= _PATTERN_CACHE_MTIME:
                    return False
                
                # Load cache (Schema: export JSON from sync)
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # If local dynamic rules are empty, allow cache patterns as a fallback.
                # This helps first-time sync where dynamic rules haven't been saved yet.
                cache_dynamic = []
                if isinstance(data, dict):
                    # Accept both legacy and new schema shapes.
                    if isinstance(data.get("dynamic"), list):
                        cache_dynamic = data.get("dynamic") or []
                    elif isinstance(data.get("patterns"), dict):
                        # patterns: {category: [regex,...]}
                        pats = []
                        for lst in (data.get("patterns") or {}).values():
                            pats.extend(lst or [])
                        cache_dynamic = [{"pattern": p, "enabled": True} for p in pats]
                # If cache has dynamic patterns and local is empty, merge.
                if cache_dynamic:
                    state = _load_dynamic_state()
                    if not state.get("patterns"):
                        try:
                            os.makedirs(os.path.dirname(IRON_DOME_DYNAMIC_RULES_PATH), exist_ok=True)
                            payload = dict(state or {})
                            payload["patterns"] = cache_dynamic
                            payload["updated_at"] = datetime.now().isoformat()
                            with open(IRON_DOME_DYNAMIC_RULES_PATH, "w", encoding="utf-8") as f:
                                json.dump(payload, f, ensure_ascii=False, indent=2)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 214, exc_info=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 216, exc_info=True)

    # Load Dynamic Rules (Local Truth)
    state = _load_dynamic_state()
    dynamic_pats = state.get("patterns", [])
    
    with _RELOAD_LOCK:
        _compile_regexes(dynamic_pats)
        if has_cache:
            _PATTERN_CACHE_MTIME = os.stat(cache_path).st_mtime
            
    return True


# Initialize on import
_reload_patterns(force=True)


# =============================================================================
# API
# =============================================================================

class IronDomeViolation(Exception):
    def __init__(self, rule_category: str, matched_content: str):
        self.rule_category = rule_category
        self.matched_content = matched_content
        super().__init__(f"[IRON DOME] Violation: {rule_category} -> '{matched_content}'")


def sanitize_input(text: str) -> str:
    """
    Validates input against all rules. Raises IronDomeViolation if unsafe.
    Returns original text if safe.
    """
    _reload_patterns()  # Check for hot reloads
    
    if not text:
        return text

    text = _strip_zero_width(text)
        
    for category, regex in _COMPILED_REGEXES.items():
        match = regex.search(text)
        if match:
            raise IronDomeViolation(category, match.group())
            
    return text


def is_safe(text: str) -> Tuple[bool, str]:
    """Safe boolean check."""
    try:
        sanitize_input(text)
        return True, ""
    except IronDomeViolation as e:
        return False, str(e)



def get_all_patterns() -> List[dict]:
    """Returns list of all active patterns for inspection."""
    rules = []
    # Static
    for cat, pats in STATIC_RULE_SETS.items():
        for p in pats:
            rules.append({"pattern": p, "category": cat, "type": "static"})
    # Dynamic
    state = _load_dynamic_state()
    for p in state.get("patterns", []):
        if p.get("enabled", True):
            rules.append({
                "pattern": p.get("pattern"), 
                "category": "DYNAMIC", 
                "type": "dynamic", 
                "id": p.get("id")
            })
    return rules


# =============================================================================
# Supply Chain Audit
# =============================================================================

# Known malicious npm packages — curated blocklist.
# Only packages that are INHERENTLY malicious (not legitimate packages
# that were temporarily compromised — those go in _NPM_COMPROMISED_VERSIONS).
_NPM_BLOCKLIST: Dict[str, str] = {
    # --- axios incident (2026-03) ---
    "plain-crypto-js":      "RAT dropper (axios supply-chain attack 2026-03-31)",
    # --- Typosquats ---
    "crypto-js-esm":        "Typosquat targeting crypto-js",
    "cryptojs-esm":         "Typosquat targeting crypto-js",
    # --- event-stream incident (2018) ---
    "flatmap-stream":       "Payload for event-stream attack (2018)",
    # --- GlassWorm / invisible Unicode campaign (2025-2026) ---
    # (These are identified by behavior, not fixed names — see Unicode patterns above)
    # --- Crypto stealer packages (2025-2026) ---
    "ethereum-cryptographyy": "Typosquat targeting ethereum-cryptography",
    "solanajs":              "Typosquat targeting @solana/web3.js",
    "web3-utils-pro":        "Fake web3 utility — credential stealer",
}

# Known compromised npm versions — {package: {bad_versions}}.
_NPM_COMPROMISED_VERSIONS: Dict[str, set] = {
    "axios":         {"1.14.1", "0.30.4"},
    "ua-parser-js":  {"0.7.29", "0.8.0", "1.0.0"},
    "coa":           {"2.0.3", "2.0.4", "2.1.1", "2.1.3", "3.0.1", "3.1.3"},
    "rc":            {"1.2.9", "1.3.9", "2.3.9"},
    "colors":        {"1.4.1", "1.4.2"},
    "faker":         {"6.6.6"},
}

# Compromised npm package SHA-1 hashes (for lockfile integrity check).
_NPM_COMPROMISED_HASHES: set = {
    "2553649f232204966871cea80a5d0d6adc700ca",   # axios@1.14.1
    "d6f3f62fd3b9f5432f5782b62d8cfd5247d5ee71",  # axios@0.30.4
    "07d889e2dadce6f3910dcbc253317d28ca61c766",  # plain-crypto-js@4.2.1
}

# C2 domains / network IOCs — used by audit_supply_chain() for code scanning.
_C2_DOMAINS: set = {
    "sfrclak.com",          # axios RAT C2
}

# Known malicious PyPI packages.
_PIP_BLOCKLIST: Dict[str, str] = {
    # --- Typosquats ---
    "python-dateutil2":     "Typosquat targeting python-dateutil",
    "jeIlyfish":            "Typosquat targeting jellyfish (capital-I as lowercase-L)",
    "python3-dateutil":     "Typosquat targeting python-dateutil",
    "request":              "Typosquat targeting requests",
    "python-binance":       "Typosquat targeting python-binance (credential stealer)",
    # --- SilentSync RAT campaign (2025) ---
    "termncolor":           "Typosquat targeting termcolor — SilentSync RAT dropper",
    "sisaws":               "Typosquat targeting sisa — SilentSync RAT",
    "secmeasure":           "Fake security package — SilentSync RAT",
    # --- Fake cloud utilities (2025) ---
    "acloud-client":        "Fake cloud client — token stealer",
    "enumer-iam":           "Typosquat targeting enumerate-iam — token stealer",
    "snapshot-photo":       "Fake utility — credential exfiltration",
    # --- Colorama incident ---
    "colorizr":             "Typosquat targeting colorama — credential stealer",
}


def audit_supply_chain(root_dir: str = "") -> dict:
    """
    Scan project files for known malicious packages.

    Checks:
      1. package.json / package-lock.json  → _NPM_BLOCKLIST + _NPM_COMPROMISED_VERSIONS
      2. requirements*.txt / pyproject.toml → _PIP_BLOCKLIST
      3. node_modules directory             → blocklisted directories

    Returns:
        {"ok": bool, "findings": [{"file", "package", "version", "severity", "detail"}]}
    """
    logger = logging.getLogger(__name__)
    root = root_dir or _MAGI_ROOT
    findings: List[dict] = []

    # --- Helper: extract version from lockfile entry ---
    def _check_npm_lock(filepath: str) -> None:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        packages = data.get("packages") or data.get("dependencies") or {}
        for pkg_path, info in packages.items():
            if not isinstance(info, dict):
                continue
            # Extract package name from path (e.g. "node_modules/axios" → "axios")
            name = pkg_path.rsplit("node_modules/", 1)[-1] if "node_modules/" in pkg_path else pkg_path
            if not name:
                continue
            version = str(info.get("version", ""))
            resolved = str(info.get("resolved", ""))
            integrity = str(info.get("integrity", ""))

            # Check blocklist
            if name in _NPM_BLOCKLIST:
                findings.append({
                    "file": filepath, "package": name, "version": version,
                    "severity": "CRITICAL",
                    "detail": f"已知惡意套件：{_NPM_BLOCKLIST[name]}",
                })
            # Check compromised versions
            if name in _NPM_COMPROMISED_VERSIONS and version in _NPM_COMPROMISED_VERSIONS[name]:
                findings.append({
                    "file": filepath, "package": name, "version": version,
                    "severity": "CRITICAL",
                    "detail": f"已知被入侵版本 {name}@{version}",
                })
            # Check rogue registry
            if resolved and resolved.startswith("http://"):
                findings.append({
                    "file": filepath, "package": name, "version": version,
                    "severity": "WARNING",
                    "detail": f"使用非 HTTPS registry: {resolved[:120]}",
                })
            # Check weak integrity hash
            if integrity and integrity.startswith("sha1-"):
                findings.append({
                    "file": filepath, "package": name, "version": version,
                    "severity": "WARNING",
                    "detail": f"弱 SHA-1 integrity hash（應為 sha512）",
                })

    def _check_npm_package_json(filepath: str) -> None:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            deps = data.get(section) or {}
            for name, ver_spec in deps.items():
                if name in _NPM_BLOCKLIST:
                    findings.append({
                        "file": filepath, "package": name, "version": str(ver_spec),
                        "severity": "CRITICAL",
                        "detail": f"已知惡意套件：{_NPM_BLOCKLIST[name]}",
                    })
        # Check postinstall scripts for dropper patterns
        scripts = data.get("scripts") or {}
        for hook in ("preinstall", "postinstall", "install"):
            cmd = str(scripts.get(hook, ""))
            if cmd and re.search(r"curl|wget|powershell|certutil|bash\s+-[ec]|sh\s+-c", cmd, re.IGNORECASE):
                findings.append({
                    "file": filepath, "package": "(self)", "version": "",
                    "severity": "WARNING",
                    "detail": f"{hook} script 含下載/執行指令：{cmd[:120]}",
                })

    def _check_pip_requirements(filepath: str) -> None:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[><=!~\[]", line)[0].strip()
            if pkg.lower() in (k.lower() for k in _PIP_BLOCKLIST):
                findings.append({
                    "file": filepath, "package": pkg, "version": line,
                    "severity": "CRITICAL",
                    "detail": f"已知惡意 PyPI 套件：{_PIP_BLOCKLIST.get(pkg, '')}",
                })

    def _check_node_modules(dirpath: str) -> None:
        if not os.path.isdir(dirpath):
            return
        try:
            entries = os.listdir(dirpath)
        except Exception:
            return
        for entry in entries:
            if entry.startswith("@"):
                # Scoped packages
                scope_path = os.path.join(dirpath, entry)
                try:
                    for sub in os.listdir(scope_path):
                        full_name = f"{entry}/{sub}"
                        if full_name in _NPM_BLOCKLIST or sub in _NPM_BLOCKLIST:
                            findings.append({
                                "file": scope_path, "package": full_name, "version": "?",
                                "severity": "CRITICAL",
                                "detail": f"node_modules 中發現惡意套件目錄",
                            })
                except Exception:
                    continue
            elif entry in _NPM_BLOCKLIST:
                findings.append({
                    "file": dirpath, "package": entry, "version": "?",
                    "severity": "CRITICAL",
                    "detail": f"node_modules 中發現惡意套件目錄",
                })

    def _check_compromised_hashes(filepath: str) -> None:
        """Check lockfile resolved URLs for known compromised SHA-1 hashes."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return
        for bad_hash in _NPM_COMPROMISED_HASHES:
            if bad_hash in content:
                findings.append({
                    "file": filepath, "package": "(hash match)", "version": "",
                    "severity": "CRITICAL",
                    "detail": f"發現已知惡意套件 SHA-1 hash: {bad_hash[:20]}...",
                })

    # Invisible Unicode detection regex (GlassWorm-style obfuscation)
    _INVISIBLE_UNICODE_RE = re.compile(
        r"[\uFE00-\uFE0F\u200B-\u200F\u2028-\u202F\u2060-\u2064\uFEFF"
        r"\U000E0100-\U000E01EF]{4,}"
    )

    def _check_source_file(filepath: str) -> None:
        """Check JS/TS/PY source files for C2 domains and invisible Unicode."""
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(500_000)  # Cap at 500KB per file
        except Exception:
            return
        # C2 domain check
        for domain in _C2_DOMAINS:
            if domain in content:
                findings.append({
                    "file": filepath, "package": "(C2 IOC)", "version": "",
                    "severity": "CRITICAL",
                    "detail": f"原始碼中發現已知 C2 網域：{domain}",
                })
        # Invisible Unicode check
        match = _INVISIBLE_UNICODE_RE.search(content)
        if match:
            findings.append({
                "file": filepath, "package": "(obfuscation)", "version": "",
                "severity": "CRITICAL",
                "detail": f"發現隱形 Unicode 字元序列（GlassWorm 風格混淆），"
                          f"位置 offset {match.start()}，長度 {len(match.group())}",
            })

    # --- Walk project ---
    _SOURCE_EXTS = {".js", ".ts", ".mjs", ".cjs", ".py", ".jsx", ".tsx"}
    # Exclude Iron Dome's own source from C2/Unicode scanning (it contains the IOC definitions)
    _self_dir = os.path.normpath(os.path.dirname(__file__))
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip deep backup / archive directories
        rel = os.path.relpath(dirpath, root)
        if any(skip in rel for skip in ("backups", ".git", "__pycache__", ".next", "venv", ".venv")):
            dirnames.clear()
            continue
        # Skip nested node_modules (handled via lockfile + directory check)
        if "node_modules" in dirnames:
            _check_node_modules(os.path.join(dirpath, "node_modules"))
            dirnames.remove("node_modules")

        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if fname == "package-lock.json":
                _check_npm_lock(fpath)
                _check_compromised_hashes(fpath)
            elif fname == "package.json":
                _check_npm_package_json(fpath)
            elif fname.startswith("requirements") and fname.endswith(".txt"):
                _check_pip_requirements(fpath)
            # Source file checks (C2 + invisible Unicode) — skip Iron Dome's own definitions
            _, ext = os.path.splitext(fname)
            if ext in _SOURCE_EXTS and os.path.normpath(dirpath) != _self_dir:
                _check_source_file(fpath)

    ok = not any(f["severity"] == "CRITICAL" for f in findings)
    if findings:
        logger.warning("[IRON DOME] Supply chain audit: %d finding(s), ok=%s", len(findings), ok)
    else:
        logger.info("[IRON DOME] Supply chain audit: CLEAN")
    return {"ok": ok, "findings": findings}


# =============================================================================
# Management API (Dynamic Rules)
# =============================================================================

def _save_dynamic_state(state: dict) -> dict:
    try:
        os.makedirs(os.path.dirname(IRON_DOME_DYNAMIC_RULES_PATH), exist_ok=True)
        payload = dict(state or {})
        payload.setdefault("patterns", [])
        payload["updated_at"] = datetime.now().isoformat()
        with open(IRON_DOME_DYNAMIC_RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _reload_patterns(force=True)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def list_patterns(include_static: bool = False, include_disabled: bool = False, limit: int = 500) -> dict:
    state = _load_dynamic_state()
    dynamic = []
    for item in state.get("patterns", []):
        if not isinstance(item, dict): continue
        enabled = bool(item.get("enabled", True))
        if (not include_disabled) and (not enabled): continue
        dynamic.append(item)
    
    if limit > 0:
        dynamic = dynamic[:limit]
        
    result = {
        "success": True,
        "static_count": sum(len(p) for p in STATIC_RULE_SETS.values()),
        "dynamic_count": len(dynamic),
        "dynamic": dynamic,
        "updated_at": state.get("updated_at", ""),
    }
    if include_static:
        result["static"] = get_all_patterns()
    return result


def add_pattern(pattern: str, reason: str = "", source: str = "manual", enabled: bool = True) -> dict:
    pattern = (pattern or "").strip()
    if not pattern:
        return {"success": False, "error": "empty pattern"}
    
    try:
        re.compile(pattern, re.IGNORECASE)
    except Exception as e:
        return {"success": False, "error": f"invalid regex: {e}"}
        
    # Check static
    for p in get_all_patterns():
        if p["pattern"] == pattern and p.get("type") == "static":
             return {"success": True, "added": False, "scope": "static", "message": "pattern already in static rules"}

    state = _load_dynamic_state()
    existing = None
    for item in state.get("patterns", []):
        if str(item.get("pattern", "")).strip() == pattern:
            existing = item
            break
            
    if existing:
        existing["enabled"] = bool(enabled)
        if reason: existing["reason"] = reason[:200]
        if source: existing["source"] = source[:80]
        return _save_dynamic_state(state)
        
    rule_id = f"dyn_{hashlib.sha1(pattern.encode('utf-8')).hexdigest()[:12]}"
    entry = {
        "id": rule_id,
        "pattern": pattern,
        "reason": (reason or "").strip()[:200],
        "source": (source or "manual").strip()[:80],
        "enabled": bool(enabled),
        "created_at": datetime.now().isoformat(),
        "hits": 0
    }
    state.setdefault("patterns", []).append(entry)
    res = _save_dynamic_state(state)
    res["id"] = rule_id
    res["added"] = bool(res.get("success"))
    return res

# =============================================================================
# Auto-Harden Logic (From Skill Genesis)
# =============================================================================

def _extract_auto_harden_candidates(text: str, max_candidates: int = 12) -> List[str]:
    data = (text or "").strip()
    if not data: return []

    # Never store secrets verbatim inside dynamic rules. Prefer generic patterns.
    generic_secret_patterns = [
        r"(?:channel\s+access\s+token|access\s+token)\s*[:=]\s*[A-Za-z0-9+/=_-]{20,}",
        r"(?:token|api[_-]?key|secret)\s*[:=]\s*[A-Za-z0-9+/=_-]{20,}",
        r"authorization\s*:\s*bearer\s+[A-Za-z0-9+/=_-]{20,}",
    ]

    risky_markers = [
        "rm -rf", "drop table", "drop database", "truncate table", "delete from",
        "os.system(", "subprocess.", "eval(", "exec(", "curl ", "wget ",
        "/dev/tcp/", "nc -e", "chmod 777", "mkfs", "wipefs", "shred",
        "token=", "api_key=", "secret=",
    ]

    picked = []
    seen = set()

    def _pick(sample: str):
        s = (sample or "").strip()
        if not s or len(s) < 5 or len(s) > 180: return
        low = s.lower()
        if re.search(r"(token|api[_-]?key|secret)\s*[:=]\s*[a-z0-9+/=_-]{20,}", low, re.IGNORECASE):
            for pat in generic_secret_patterns:
                if pat not in seen:
                    seen.add(pat)
                    picked.append(pat)
            return
        if re.search(r"[a-z0-9+/=_-]{28,}", low, re.IGNORECASE): return
        if not any(marker in low for marker in risky_markers): return
        if s in seen: return
        seen.add(s)
        picked.append(s)

    for block in re.findall(r"`([^`]{5,220})`", data):
        _pick(block)
    for line in data.splitlines():
        _pick(line)

    patterns = []
    for sample in picked[:max_candidates]:
        if sample.startswith("(?:") or sample.startswith("authorization"):
            patterns.append(sample)
            continue
        escaped = re.escape(sample).replace(r"\ ", r"\s+")
        patterns.append(escaped)
    return patterns


def auto_harden_scope(incident_text: str, source: str = "auto", max_new: int = 3) -> dict:
    candidates = _extract_auto_harden_candidates(incident_text or "", max_candidates=max(1, max_new * 3))
    if not candidates:
        return {"success": True, "added": [], "skipped": [], "message": "no strong candidates"}

    added = []
    skipped = []
    for pattern in candidates:
        if len(added) >= max_new:
            skipped.append({"pattern": pattern, "reason": "max_new_reached"})
            continue
        # Use add_pattern from this module
        result = add_pattern(
            pattern,
            reason="auto-harden from incident",
            source=source or "auto",
            enabled=True,
        )
        if result.get("success") and result.get("added"):
            added.append({"id": result.get("id"), "pattern": pattern})
        else:
            skipped.append({"pattern": pattern, "reason": result.get("error") or "already_exists"})

    return {
        "success": True,
        "added": added,
        "skipped": skipped,
    }
