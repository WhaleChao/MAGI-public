# -*- coding: utf-8 -*-
"""Unified circuit breaker for remote inference peers.

Replaces the scattered CB logic in inference_gateway (Balthasar),
melchior_client (Melchior), and nim_heavy (NVIDIA NIM).

Opt-in via env MAGI_USE_REMOTE_HEALTH_GATE=1. When disabled, callers
MUST use their legacy path — this module will RuntimeError to force
the caller to not silently drift into the new code path.

Memory safety:
- singleton protected by lock; never rebuilt
- audit markers capped at 10 files per peer per day
- probe HTTP uses (connect=1.0s, read=1.5s) tuple timeout
- probe result cached 30s (configurable per peer)
- all shared state reads use `with state.lock:`
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


@dataclass
class PeerConfig:
    name: str
    probe_url: Optional[str] = None
    probe_timeout_connect_sec: float = 1.0
    probe_timeout_read_sec: float = 1.5
    fail_threshold: int = 2
    cooldown_seconds: Tuple[int, ...] = (30, 90, 180)
    probe_cache_ttl_sec: float = 30.0
    audit_dir_env: Optional[str] = None
    max_audit_files_per_day: int = 10


@dataclass
class PeerState:
    consecutive_failures: int = 0
    tripped_at_mono: float = 0.0
    cooldown_level: int = 0
    last_reason: str = ""
    last_probe_ok: Optional[bool] = None
    last_probe_at_mono: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class RemoteHealthGate:
    def __init__(self) -> None:
        self._configs: Dict[str, PeerConfig] = {}
        self._states: Dict[str, PeerState] = {}
        self._registry_lock = threading.Lock()

    # ---- registration ----
    def register(self, cfg: PeerConfig) -> None:
        with self._registry_lock:
            if cfg.name not in self._configs:
                self._configs[cfg.name] = cfg
                self._states[cfg.name] = PeerState()

    # ---- main API ----
    def is_reachable(self, name: str) -> Tuple[bool, str]:
        cfg = self._configs.get(name)
        state = self._states.get(name)
        if cfg is None or state is None:
            return False, "down:peer_not_registered"
        now = time.monotonic()
        with state.lock:
            # circuit open?
            if state.tripped_at_mono > 0:
                cooldown = cfg.cooldown_seconds[
                    min(state.cooldown_level, len(cfg.cooldown_seconds) - 1)
                ]
                elapsed = now - state.tripped_at_mono
                if elapsed < cooldown:
                    retry_in = int(cooldown - elapsed)
                    return False, f"circuit_open_fallback:retry_in_{retry_in}s"
                # expired — allow probe
                state.tripped_at_mono = 0.0
            # probe cache still fresh?
            if (
                state.last_probe_ok is not None
                and now - state.last_probe_at_mono < cfg.probe_cache_ttl_sec
            ):
                if state.last_probe_ok:
                    return True, "ok"
                return False, f"down:{state.last_reason}"
        # do the probe outside the lock (HTTP IO)
        ok, reason = self._http_probe(cfg)
        with state.lock:
            state.last_probe_ok = ok
            state.last_probe_at_mono = time.monotonic()
            if ok:
                state.consecutive_failures = 0
                if state.cooldown_level > 0:
                    state.cooldown_level -= 1
                return True, "ok"
            state.consecutive_failures += 1
            state.last_reason = reason
            if state.consecutive_failures >= cfg.fail_threshold:
                state.tripped_at_mono = time.monotonic()
                state.cooldown_level += 1
                self._write_audit_marker(cfg, state, reason)
                cooldown = cfg.cooldown_seconds[
                    min(state.cooldown_level - 1, len(cfg.cooldown_seconds) - 1)
                ]
                return False, f"circuit_open_fallback:retry_in_{cooldown}s"
            return False, f"down:{reason}"

    def mark_success(self, name: str) -> None:
        state = self._states.get(name)
        if state is None:
            return
        with state.lock:
            state.consecutive_failures = 0
            state.tripped_at_mono = 0.0
            if state.cooldown_level > 0:
                state.cooldown_level -= 1
            state.last_probe_ok = True
            state.last_probe_at_mono = time.monotonic()

    def mark_failure(self, name: str, reason: str) -> None:
        cfg = self._configs.get(name)
        state = self._states.get(name)
        if cfg is None or state is None:
            return
        with state.lock:
            state.consecutive_failures += 1
            state.last_reason = reason
            state.last_probe_ok = False
            state.last_probe_at_mono = time.monotonic()
            if state.consecutive_failures >= cfg.fail_threshold:
                state.tripped_at_mono = time.monotonic()
                state.cooldown_level += 1
                self._write_audit_marker(cfg, state, reason)

    # ---- inspection ----
    def circuit_status(self, name: str) -> dict:
        cfg = self._configs.get(name)
        state = self._states.get(name)
        if cfg is None or state is None:
            return {"open": False, "registered": False}
        now = time.monotonic()
        with state.lock:
            open_ = state.tripped_at_mono > 0
            retry_in = 0
            if open_:
                cooldown = cfg.cooldown_seconds[
                    min(state.cooldown_level - 1, len(cfg.cooldown_seconds) - 1)
                ]
                retry_in = max(0, int(cooldown - (now - state.tripped_at_mono)))
            return {
                "open": open_,
                "registered": True,
                "consecutive_failures": state.consecutive_failures,
                "cooldown_level": state.cooldown_level,
                "retry_in_sec": retry_in,
                "last_reason": state.last_reason,
            }

    def all_status(self) -> Dict[str, dict]:
        return {name: self.circuit_status(name) for name in self._configs}

    def force_reset(self, name: str) -> None:
        state = self._states.get(name)
        if state is None:
            return
        with state.lock:
            state.consecutive_failures = 0
            state.tripped_at_mono = 0.0
            state.cooldown_level = 0
            state.last_reason = ""
            state.last_probe_ok = None
            state.last_probe_at_mono = 0.0

    def reset_for_test(self) -> None:
        with self._registry_lock:
            self._configs.clear()
            self._states.clear()

    # ---- internals ----
    def _http_probe(self, cfg: PeerConfig) -> Tuple[bool, str]:
        if cfg.probe_url is None or requests is None:
            return True, "ok"  # no probe configured — assume reachable
        try:
            resp = requests.get(
                cfg.probe_url,
                timeout=(cfg.probe_timeout_connect_sec, cfg.probe_timeout_read_sec),
            )
            if resp.status_code == 200:
                return True, "ok"
            return False, f"health_status_{resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__ + ":" + str(exc)[:200]

    def _write_audit_marker(
        self, cfg: PeerConfig, state: PeerState, reason: str
    ) -> None:
        if not cfg.audit_dir_env:
            return
        dir_path = os.environ.get(cfg.audit_dir_env, "").strip()
        if not dir_path:
            return
        try:
            p = Path(dir_path)
            p.mkdir(parents=True, exist_ok=True)
            # cap files per day per peer
            today = time.strftime("%Y%m%d")
            pattern = f"{cfg.name}_down_{today}_*.json"
            existing = sorted(p.glob(pattern))
            while len(existing) >= cfg.max_audit_files_per_day:
                try:
                    existing[0].unlink()
                except Exception:
                    pass
                existing = sorted(p.glob(pattern))
            ts = int(time.time())
            out = {
                "ts": ts,
                "peer": cfg.name,
                "probe_url": cfg.probe_url or "",
                "balthasar_url": cfg.probe_url or "",  # legacy compat for Balthasar
                "reason": reason,
                "ttl_sec": cfg.cooldown_seconds[
                    min(state.cooldown_level - 1, len(cfg.cooldown_seconds) - 1)
                ],
                "consecutive_failures": state.consecutive_failures,
                "note": f"{cfg.name} unreachable — Synology fallback armed",
            }
            dest = p / f"{cfg.name}_down_{today}_{ts}.json"
            tmp = dest.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
            tmp.replace(dest)
        except Exception:
            pass  # audit write failure must never break the caller


_SINGLETON: Optional[RemoteHealthGate] = None
_SINGLETON_LOCK = threading.Lock()


def get_gate() -> RemoteHealthGate:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = RemoteHealthGate()
        return _SINGLETON


def _require_enabled() -> None:
    flag = os.environ.get("MAGI_USE_REMOTE_HEALTH_GATE", "0").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "RemoteHealthGate disabled (MAGI_USE_REMOTE_HEALTH_GATE != 1); "
            "caller must use legacy CB path"
        )
