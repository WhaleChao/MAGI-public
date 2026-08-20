#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 2 — 常駐 memory watchdog。

每 30 秒檢查系統記憶體壓力；連續 3 次（共 ~90s）確認目前仍有壓力才
觸發 action。高 swap 本身不是壓力證據，必須同時有 memory_pressure 低水位、
可用記憶體不足或 swap 持續成長，避免把歷史 swap 高水位當成當前壓力。

觸發後以 macOS ``footprint`` 量測 8081 Embed / 8090 MTP 的實體與
IOAccelerator (Metal) footprint，依序採取安全動作：
  1. MTP 未啟用時停止 8090 LaunchAgent；
  2. 8081 Metal footprint 異常膨脹時受控重啟並確認 health；
  3. 8080 模型已卸載但 footprint 持續異常時受控重啟；
  4. 沒有安全模型動作時只記錄，不殺 API server。

Mode:
  - shadow（預設 MAGI_WATCHDOG_KILL_MODE=shadow）：寫
    .runtime/metrics/memory_watchdog_decisions.jsonl，不真殺
  - enforce：執行上述模型服務動作；不殺 API/Discord/Tools 等核心程序

設計紅線：
  - 不殺 daemon.py（MAGI 主管家）
  - 不殺 API server、daemon、Discord、Tools、launchd 或瀏覽器
  - 只透過 launchctl 管理 8081/8090，不直接 SIGKILL 模型服務
  - 連續 N 次才觸發（避免瞬時波動誤判）
  - 額外回收 MAGI 啟動後逾時未關的 Playwright driver / headless browser；
    這類進程常是 portal 自動化 teardown hang，不受記憶體壓力門檻限制。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAGI_ROOT = Path(
    os.environ.get("MAGI_ROOT_DIR")
    or os.environ.get("MAGI_ROOT")
    or str(Path(__file__).resolve().parents[2])
).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402

# ---- 設定 --------------------------------------------------------------

CHECK_INTERVAL_SEC = int(os.environ.get("MAGI_WATCHDOG_INTERVAL_SEC", "30"))
TRIGGER_CONSECUTIVE = int(os.environ.get("MAGI_WATCHDOG_TRIGGER_CONSECUTIVE", "3"))
SWAP_THRESHOLD_GB = float(os.environ.get("MAGI_WATCHDOG_SWAP_GB", "8"))
FREE_INACTIVE_MIN_GB = float(os.environ.get("MAGI_WATCHDOG_FREE_MIN_GB", "2"))
MEMORY_FREE_PERCENT_MIN = float(os.environ.get("MAGI_WATCHDOG_MEMORY_FREE_PERCENT", "20"))
SWAP_GROWTH_TRIGGER_GB = float(os.environ.get("MAGI_WATCHDOG_SWAP_GROWTH_GB", "0.25"))
EMBED_FOOTPRINT_MAX_GB = float(os.environ.get("MAGI_WATCHDOG_EMBED_FOOTPRINT_GB", "4"))
MAIN_UNLOADED_FOOTPRINT_MAX_GB = float(os.environ.get("MAGI_WATCHDOG_MAIN_FOOTPRINT_GB", "2"))
METAL_FOOTPRINT_CONSECUTIVE = int(os.environ.get("MAGI_WATCHDOG_METAL_CONSECUTIVE", "3"))
EMBED_HEALTH_TIMEOUT_SEC = int(os.environ.get("MAGI_WATCHDOG_EMBED_HEALTH_TIMEOUT_SEC", "90"))
ACTION_COOLDOWN_SEC = int(os.environ.get("MAGI_WATCHDOG_COOLDOWN_SEC", "600"))  # 10 min between kills
STALE_PLAYWRIGHT_ENABLED = os.environ.get(
    "MAGI_WATCHDOG_REAP_STALE_PLAYWRIGHT", "1"
).strip().lower() not in {"0", "false", "no", "off"}
STALE_PLAYWRIGHT_MAX_AGE_SEC = int(os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_SEC", str(45 * 60),
))
STALE_PLAYWRIGHT_COOLDOWN_SEC = int(os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_COOLDOWN_SEC", "300",
))
STALE_PLAYWRIGHT_MODE = os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE", "enforce",
).strip().lower()
if STALE_PLAYWRIGHT_MODE not in {"shadow", "enforce"}:
    STALE_PLAYWRIGHT_MODE = "shadow"

MTP_LAUNCH_LABEL = "com.magi.mlx-mtp"
EMBED_LAUNCH_LABEL = "com.magi.omlx-embed"
MAIN_LAUNCH_LABEL = "com.magi.omlx"
EMBED_HEALTH_URL = "http://127.0.0.1:8081/health"
MAIN_HEALTH_URL = "http://127.0.0.1:8080/health"

DECISION_LOG = runtime_dir.metrics("memory_watchdog_decisions")


# ---- 記憶體感測 ---------------------------------------------------------

@dataclass
class MemoryReading:
    swap_used_gb: float
    free_gb: float
    inactive_gb: float
    memory_free_percent: float = -1.0
    page_size: int = 16384

    @property
    def free_plus_inactive_gb(self) -> float:
        return self.free_gb + self.inactive_gb


def _read_memory_free_percent() -> float:
    """Return ``memory_pressure -Q`` free percentage, or -1 when unavailable."""
    binary = "/usr/bin/memory_pressure"
    if not os.path.exists(binary):
        binary = "memory_pressure"
    try:
        proc = subprocess.run(
            [binary, "-Q"], capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return -1.0
    for line in (proc.stdout or "").splitlines():
        if "System-wide memory free percentage:" not in line:
            continue
        try:
            return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
        except (ValueError, IndexError):
            return -1.0
    return -1.0


def read_memory() -> MemoryReading:
    """Read vm_stat, swap usage and current macOS memory-pressure percentage."""
    vm_stat = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=10, check=False,
    )
    page_size = 16384
    free_pages = 0
    inactive_pages = 0
    for line in vm_stat.stdout.splitlines():
        if "page size of" in line:
            # "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
            try:
                page_size = int(line.split("page size of")[1].split("bytes")[0].strip())
            except (IndexError, ValueError):
                pass
        elif line.startswith("Pages free:"):
            free_pages = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
    free_gb = free_pages * page_size / 1024 / 1024 / 1024
    inactive_gb = inactive_pages * page_size / 1024 / 1024 / 1024

    # swap (use absolute paths so launchd PATH limitations don't break us)
    _sysctl_bin = "/usr/sbin/sysctl"
    if not os.path.exists(_sysctl_bin):
        _sysctl_bin = "sysctl"  # fallback to PATH lookup
    swap = subprocess.run(
        [_sysctl_bin, "vm.swapusage"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    swap_used_gb = 0.0
    # "vm.swapusage: total = 12288.00M  used = 5432.16M  free = ..."
    for token in swap.stdout.replace("=", " ").split():
        if token.endswith("M") and "swap_used_marker" not in token:
            pass
    # 更穩定的解析：split by "used"
    if "used" in swap.stdout:
        try:
            after = swap.stdout.split("used", 1)[1]
            # 下一個 M 之前的數字
            num = ""
            for ch in after:
                if ch.isdigit() or ch == ".":
                    num += ch
                elif num:
                    break
            swap_used_gb = float(num) / 1024  # MB → GB
        except (ValueError, IndexError):
            pass
    return MemoryReading(
        swap_used_gb=swap_used_gb,
        free_gb=free_gb,
        inactive_gb=inactive_gb,
        memory_free_percent=_read_memory_free_percent(),
        page_size=page_size,
    )


def memory_pressure_reasons(r: MemoryReading, swap_growth_gb: float = 0.0) -> List[str]:
    """Return evidence of *current* pressure, ignoring stale high swap by itself."""
    reasons: List[str] = []
    low_available = r.free_plus_inactive_gb < FREE_INACTIVE_MIN_GB
    pressure_percent_known = r.memory_free_percent >= 0
    low_pressure_percent = (
        pressure_percent_known and r.memory_free_percent < MEMORY_FREE_PERCENT_MIN
    )
    swap_high = r.swap_used_gb > SWAP_THRESHOLD_GB
    swap_rising = swap_growth_gb >= SWAP_GROWTH_TRIGGER_GB

    if low_available:
        reasons.append("free_plus_inactive_low")
    if low_pressure_percent:
        reasons.append("memory_pressure_low")
    if swap_high and swap_rising:
        reasons.append("swap_high_and_rising")
    elif swap_high and low_pressure_percent:
        reasons.append("swap_high_with_memory_pressure")
    return reasons


def is_memory_pressure(r: MemoryReading, swap_growth_gb: float = 0.0) -> bool:
    return bool(memory_pressure_reasons(r, swap_growth_gb))


# ---- Metal-aware 模型服務量測 ------------------------------------------

@dataclass
class MetalService:
    name: str
    launch_label: str
    pid: int
    port: int
    rss_bytes: int
    footprint_bytes: int
    graphics_bytes: int
    cmdline: str
    loaded_count: Optional[int] = None
    model_memory_bytes: Optional[int] = None

    @property
    def footprint_gb(self) -> float:
        return self.footprint_bytes / 1024 / 1024 / 1024

    @property
    def graphics_gb(self) -> float:
        return self.graphics_bytes / 1024 / 1024 / 1024


@dataclass
class ServiceAction:
    kind: str
    service: MetalService


@dataclass
class ProcessRow:
    pid: int
    ppid: int
    elapsed_sec: int
    cmdline: str


_SIZE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>TB|GB|MB|KB|B)\b", re.I)


def _size_to_bytes(text: str) -> int:
    match = _SIZE_RE.search(text or "")
    if not match:
        return 0
    scale = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    return int(float(match.group("value")) * scale[match.group("unit").upper()])


def read_process_footprint(pid: int) -> Tuple[int, int]:
    """Return (physical footprint, IOAccelerator/Metal bytes) for a PID."""
    binary = "/usr/bin/footprint"
    if not os.path.exists(binary):
        binary = "footprint"
    try:
        proc = subprocess.run(
            [binary, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        return 0, 0
    output = proc.stdout or ""
    footprint_bytes = 0
    graphics_bytes = 0
    header = re.search(r"Footprint:\s*([^\n(]+)", output, re.I)
    if header:
        footprint_bytes = _size_to_bytes(header.group(1))
    for line in output.splitlines():
        if "IOAccelerator" in line or "IOSurface" in line:
            graphics_bytes += _size_to_bytes(line)
    return footprint_bytes, graphics_bytes


def read_omlx_load_state(url: str) -> Tuple[Optional[int], Optional[int]]:
    """Read load state only as a restart safety guard, never as memory usage."""
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if int(getattr(response, "status", 0) or 0) != 200:
                return None, None
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None, None
    pool = payload.get("engine_pool")
    if not isinstance(pool, dict):
        return None, None
    try:
        loaded_count = int(pool.get("loaded_count"))
    except (TypeError, ValueError):
        loaded_count = None
    try:
        model_memory = int(pool.get("current_model_memory"))
    except (TypeError, ValueError):
        model_memory = None
    return loaded_count, model_memory


def list_metal_services() -> List[MetalService]:
    """Locate 8080/8081/8090 and measure their real Metal-aware footprint."""
    res = subprocess.run(
        ["ps", "-eo", "pid=,rss=,command="],
        capture_output=True, text=True, timeout=10, check=False,
    )
    out: List[MetalService] = []
    for line in res.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        if "serve_mlx_mtp.py" in cmd or re.search(r"--port(?:=|\s+)8090\b", cmd):
            name, label, port = "mtp", MTP_LAUNCH_LABEL, 8090
        elif "omlx" in cmd and re.search(r"--port(?:=|\s+)8081\b", cmd):
            name, label, port = "embed", EMBED_LAUNCH_LABEL, 8081
        elif "omlx" in cmd and re.search(r"--port(?:=|\s+)8080\b", cmd):
            name, label, port = "main", MAIN_LAUNCH_LABEL, 8080
        else:
            continue
        footprint_bytes, graphics_bytes = read_process_footprint(pid)
        if name == "main":
            loaded_count, model_memory_bytes = read_omlx_load_state(MAIN_HEALTH_URL)
        else:
            loaded_count, model_memory_bytes = None, None
        out.append(MetalService(
            name=name,
            launch_label=label,
            pid=pid,
            port=port,
            rss_bytes=rss_kb * 1024,
            footprint_bytes=footprint_bytes,
            graphics_bytes=graphics_bytes,
            cmdline=cmd,
            loaded_count=loaded_count,
            model_memory_bytes=model_memory_bytes,
        ))
    priority = {"mtp": 0, "embed": 1, "main": 2}
    out.sort(key=lambda service: (priority.get(service.name, 99), -service.footprint_bytes))
    return out


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _mtp_feature_enabled() -> bool:
    """Read the same feature flag used by API workers, including MAGI's .env."""
    if "MAGI_ENABLE_MTP_DRAFT" in os.environ:
        return _env_enabled("MAGI_ENABLE_MTP_DRAFT", False)
    env_path = MAGI_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("export "):
            text = text[7:].lstrip()
        key, sep, value = text.partition("=")
        if sep and key.strip() == "MAGI_ENABLE_MTP_DRAFT":
            normalized = value.split("#", 1)[0].strip().strip('"\'')
            return normalized.lower() in {"1", "true", "yes", "on"}
    return False


def choose_service_action(
    services: List[MetalService],
    oversized_counts: Optional[Dict[str, int]] = None,
) -> Optional[ServiceAction]:
    """Prefer disabled MTP shutdown, then abnormal Embed restart."""
    def sustained(name: str) -> bool:
        if oversized_counts is None:
            return True
        return oversized_counts.get(name, 0) >= METAL_FOOTPRINT_CONSECUTIVE

    if not _mtp_feature_enabled():
        for service in services:
            if service.name == "mtp":
                return ServiceAction("stop_mtp", service)
    embed_threshold = int(EMBED_FOOTPRINT_MAX_GB * 1024 ** 3)
    for service in services:
        if service.name != "embed":
            continue
        if sustained("embed") and (
            service.footprint_bytes >= embed_threshold
            or service.graphics_bytes >= embed_threshold
        ):
            return ServiceAction("restart_embed", service)
    main_threshold = int(MAIN_UNLOADED_FOOTPRINT_MAX_GB * 1024 ** 3)
    for service in services:
        if service.name != "main" or service.loaded_count != 0:
            continue
        if sustained("main") and (
            service.footprint_bytes >= main_threshold
            or service.graphics_bytes >= main_threshold
        ):
            return ServiceAction("restart_main", service)
    return None


def _parse_etime(raw: str) -> int:
    """Parse ps etime ([[DD-]HH:]MM:SS) into seconds."""
    text = (raw or "").strip()
    if not text:
        return 0
    days = 0
    if "-" in text:
        day_s, text = text.split("-", 1)
        try:
            days = int(day_s)
        except ValueError:
            days = 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        hours, minutes, seconds = nums
    elif len(nums) == 2:
        hours = 0
        minutes, seconds = nums
    else:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def list_process_rows() -> List[ProcessRow]:
    res = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True, text=True, timeout=10, check=False,
    )
    rows: List[ProcessRow] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append(ProcessRow(
                pid=int(parts[0]),
                ppid=int(parts[1]),
                elapsed_sec=_parse_etime(parts[2]),
                cmdline=parts[3],
            ))
        except ValueError:
            continue
    return rows


def _is_stale_magi_playwright(row: ProcessRow, by_pid: Dict[int, ProcessRow]) -> bool:
    if "playwright/driver/node" not in row.cmdline or "run-driver" not in row.cmdline:
        return False
    if row.elapsed_sec < STALE_PLAYWRIGHT_MAX_AGE_SEC:
        return False
    parent = by_pid.get(row.ppid)
    parent_cmd = parent.cmdline if parent else ""
    if str(MAGI_ROOT) not in row.cmdline and str(MAGI_ROOT) not in parent_cmd:
        return False
    return any(marker in parent_cmd for marker in ("api/server.py", "daemon.py", "laf_orchestrator.py"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap_stale_playwright(state: "WatchdogState") -> Optional[Dict]:
    """Reap MAGI-owned Playwright drivers that outlive the automation request."""
    if not STALE_PLAYWRIGHT_ENABLED:
        return None
    if time.time() - state.last_stale_playwright_at < STALE_PLAYWRIGHT_COOLDOWN_SEC:
        return None
    rows = list_process_rows()
    by_pid = {p.pid: p for p in rows}
    stale = [p for p in rows if _is_stale_magi_playwright(p, by_pid)]
    if not stale:
        return None
    stale.sort(key=lambda p: p.elapsed_sec, reverse=True)
    target = stale[0]
    parent = by_pid.get(target.ppid)
    record: Dict = {
        "mode": STALE_PLAYWRIGHT_MODE,
        "action": "stale_playwright_would_reap" if STALE_PLAYWRIGHT_MODE == "shadow" else "stale_playwright_reaped",
        "target_pid": target.pid,
        "target_ppid": target.ppid,
        "target_elapsed_sec": target.elapsed_sec,
        "target_cmd": target.cmdline,
        "parent_cmd": parent.cmdline if parent else "",
    }
    if STALE_PLAYWRIGHT_MODE == "enforce":
        try:
            os.kill(target.pid, signal.SIGTERM)
            time.sleep(2)
            if _pid_alive(target.pid):
                os.kill(target.pid, signal.SIGKILL)
                record["sigkill_sent"] = True
            else:
                record["sigterm_sent"] = True
        except ProcessLookupError:
            record["action"] = "stale_playwright_target_gone"
        except PermissionError as e:
            record["action"] = "stale_playwright_permission_denied"
            record["error"] = str(e)
        except Exception as e:
            record["action"] = "stale_playwright_error"
            record["error"] = str(e)
    _write_decision(record)
    state.last_stale_playwright_at = time.time()
    print(
        f"[memory-watchdog] {record['action']}: pid={target.pid} "
        f"age={target.elapsed_sec}s cmd={target.cmdline[:80]}"
    )
    return record


# ---- 決策與執行 ---------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write_decision(record: Dict) -> None:
    record["ts"] = _now_iso()
    try:
        runtime_dir.atomic_append_jsonl(DECISION_LOG, record, rotate_at=500, keep_tail=300)
    except Exception as e:
        print(f"[memory-watchdog] decision log write failed: {e}", file=sys.stderr)


def _kill_mode() -> str:
    m = os.environ.get("MAGI_WATCHDOG_KILL_MODE", "shadow").strip().lower()
    return m if m in {"shadow", "enforce"} else "shadow"


class WatchdogState:
    def __init__(self) -> None:
        self.consecutive_pressure = 0
        self.last_action_at: float = 0.0
        self.last_stale_playwright_at: float = 0.0
        self.previous_swap_used_gb: Optional[float] = None
        self.last_swap_growth_gb: float = 0.0
        self.last_pressure_reasons: List[str] = []
        self.oversized_service_counts: Dict[str, int] = {}

    def record_reading(self, r: MemoryReading) -> bool:
        """Record trends and return True only when a pressure action is due."""
        if self.previous_swap_used_gb is None:
            growth = 0.0
        else:
            growth = max(0.0, r.swap_used_gb - self.previous_swap_used_gb)
        self.previous_swap_used_gb = r.swap_used_gb
        self.last_swap_growth_gb = growth
        self.last_pressure_reasons = memory_pressure_reasons(r, growth)
        if self.last_pressure_reasons:
            self.consecutive_pressure += 1
        else:
            self.consecutive_pressure = 0
            self.oversized_service_counts.clear()
            return False
        if self.consecutive_pressure < TRIGGER_CONSECUTIVE:
            return False
        if time.time() - self.last_action_at < ACTION_COOLDOWN_SEC:
            return False
        return True

    def observe_services(self, services: List[MetalService]) -> None:
        """Require physical-footprint inflation to persist across pressure samples."""
        seen: set[str] = set()
        for service in services:
            if service.name == "embed":
                threshold = int(EMBED_FOOTPRINT_MAX_GB * 1024 ** 3)
                oversized = (
                    service.footprint_bytes >= threshold
                    or service.graphics_bytes >= threshold
                )
            elif service.name == "main":
                threshold = int(MAIN_UNLOADED_FOOTPRINT_MAX_GB * 1024 ** 3)
                oversized = service.loaded_count == 0 and (
                    service.footprint_bytes >= threshold
                    or service.graphics_bytes >= threshold
                )
            else:
                continue
            seen.add(service.name)
            self.oversized_service_counts[service.name] = (
                self.oversized_service_counts.get(service.name, 0) + 1 if oversized else 0
            )
        for name in list(self.oversized_service_counts):
            if name not in seen:
                self.oversized_service_counts[name] = 0


def _run_launchctl(*args: str) -> Tuple[bool, str]:
    binary = "/bin/launchctl"
    if not os.path.exists(binary):
        binary = "launchctl"
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout or "").strip()[:500]
    return proc.returncode == 0, detail


def _stop_mtp_service(_service: MetalService) -> Tuple[bool, str]:
    target = f"gui/{os.getuid()}/{MTP_LAUNCH_LABEL}"
    return _run_launchctl("bootout", target)


def _health_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if int(getattr(response, "status", 0) or 0) != 200:
                return False
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return False
    return str(payload.get("status") or "").lower() == "healthy"


def _restart_omlx_service(label: str, health_url: str) -> Tuple[bool, str]:
    target = f"gui/{os.getuid()}/{label}"
    ok, detail = _run_launchctl("kickstart", "-k", target)
    if not ok:
        return False, detail or "launchctl kickstart failed"
    deadline = time.monotonic() + EMBED_HEALTH_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if _health_ready(health_url):
            return True, "health_ready"
        time.sleep(1)
    return False, "health_timeout"


def _restart_embed_service(_service: MetalService) -> Tuple[bool, str]:
    return _restart_omlx_service(EMBED_LAUNCH_LABEL, EMBED_HEALTH_URL)


def _restart_main_service(_service: MetalService) -> Tuple[bool, str]:
    return _restart_omlx_service(MAIN_LAUNCH_LABEL, MAIN_HEALTH_URL)


def _base_pressure_record(
    r: MemoryReading,
    state: WatchdogState,
    mode: str,
) -> Dict:
    return {
        "swap_used_gb": r.swap_used_gb,
        "swap_growth_gb": state.last_swap_growth_gb,
        "free_gb": r.free_gb,
        "inactive_gb": r.inactive_gb,
        "free_plus_inactive_gb": r.free_plus_inactive_gb,
        "memory_free_percent": r.memory_free_percent,
        "pressure_reasons": list(state.last_pressure_reasons),
        "mode": mode,
    }


def _perform_service_action(
    selected: ServiceAction,
    r: MemoryReading,
    state: WatchdogState,
    mode: str,
) -> Dict:
    service = selected.service
    record = _base_pressure_record(r, state, mode)
    record.update({
        "target_service": service.name,
        "target_pid": service.pid,
        "target_port": service.port,
        "target_rss_gb": service.rss_bytes / 1024 / 1024 / 1024,
        "target_footprint_gb": service.footprint_gb,
        "target_graphics_gb": service.graphics_gb,
        "target_loaded_count": service.loaded_count,
        "target_model_memory_bytes": service.model_memory_bytes,
    })
    if mode == "shadow":
        action_names = {
            "stop_mtp": "would_stop_mtp",
            "restart_embed": "would_restart_embed",
            "restart_main": "would_restart_main",
        }
        record["action"] = action_names[selected.kind]
        print(
            f"[memory-watchdog] SHADOW: {record['action']} pid={service.pid} "
            f"footprint={service.footprint_gb:.2f}GB graphics={service.graphics_gb:.2f}GB"
        )
    else:
        if selected.kind == "stop_mtp":
            ok, detail = _stop_mtp_service(service)
            record["action"] = "mtp_stopped" if ok else "mtp_stop_failed"
        elif selected.kind == "restart_embed":
            ok, detail = _restart_embed_service(service)
            record["action"] = "embed_restarted" if ok else "embed_restart_failed"
        else:
            ok, detail = _restart_main_service(service)
            record["action"] = "main_restarted" if ok else "main_restart_failed"
        if detail:
            record["detail"] = detail
        print(
            f"[memory-watchdog] ENFORCE: {record['action']} pid={service.pid} "
            f"footprint={service.footprint_gb:.2f}GB"
        )
    _write_decision(record)
    return record


def _record_no_safe_action(
    services: List[MetalService],
    r: MemoryReading,
    state: WatchdogState,
    mode: str,
) -> Dict:
    record = _base_pressure_record(r, state, mode)
    record["action"] = "pressure_no_safe_action"
    record["services"] = [
        {
            "name": service.name,
            "pid": service.pid,
            "rss_gb": service.rss_bytes / 1024 / 1024 / 1024,
            "footprint_gb": service.footprint_gb,
            "graphics_gb": service.graphics_gb,
            "loaded_count": service.loaded_count,
            "model_memory_bytes": service.model_memory_bytes,
        }
        for service in services
    ]
    _write_decision(record)
    print("[memory-watchdog] pressure persists, but no safe model-service action applies")
    return record


# ---- main loop ----------------------------------------------------------

def run_once(state: WatchdogState) -> Dict:
    """單次檢查；測試用。"""
    stale_rec = reap_stale_playwright(state)
    if stale_rec is not None:
        return stale_rec
    r = read_memory()
    action_due = state.record_reading(r)
    services: List[MetalService] = []
    if state.last_pressure_reasons:
        services = list_metal_services()
        state.observe_services(services)
    if not action_due:
        return {
            "pressure": bool(state.last_pressure_reasons),
            "pressure_reasons": list(state.last_pressure_reasons),
            "swap_growth_gb": state.last_swap_growth_gb,
            "consecutive": state.consecutive_pressure,
            "oversized_service_counts": dict(state.oversized_service_counts),
        }
    mode = _kill_mode()
    selected = choose_service_action(services, state.oversized_service_counts)
    if selected is None:
        rec = _record_no_safe_action(services, r, state, mode)
    else:
        rec = _perform_service_action(selected, r, state, mode)
    state.last_action_at = time.time()
    state.consecutive_pressure = 0
    return rec


def main_loop() -> int:
    print(f"[memory-watchdog] start: interval={CHECK_INTERVAL_SEC}s "
          f"trigger={TRIGGER_CONSECUTIVE}x swap>{SWAP_THRESHOLD_GB}GB "
          f"with growth/current pressure, free+inactive<{FREE_INACTIVE_MIN_GB}GB, "
          f"memory_free<{MEMORY_FREE_PERCENT_MIN}%, mode={_kill_mode()}, "
          f"stale_playwright={STALE_PLAYWRIGHT_MODE if STALE_PLAYWRIGHT_ENABLED else 'off'}")
    state = WatchdogState()
    while True:
        try:
            run_once(state)
        except Exception as e:
            print(f"[memory-watchdog] loop error: {e}", file=sys.stderr)
        time.sleep(CHECK_INTERVAL_SEC)
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main_loop())
