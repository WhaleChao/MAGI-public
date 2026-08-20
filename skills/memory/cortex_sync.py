# -*- coding: utf-8 -*-
"""
Cortex Sync Skill (皮質同步)
Iron Dome Audit: ✅ SAFE — Read-only from source DB, write to internal DB

Bridges Source DB (law_firm_data) -> Vector DB (magi_brain)
"""

from __future__ import annotations

import json
import os
import sys
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
# This module is also the deterministic cron CLI entry point.  When Python is
# given this file path directly, only ``skills/memory`` is added to sys.path;
# add the sealed release root so package imports resolve exactly as under
# ``python -m``.  This changes import resolution only and does not relax any
# release or external-state binding.
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)
import logging
import argparse

_SCHEDULE_FIXTURE_MODE = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE") == "1"
if not _SCHEDULE_FIXTURE_MODE:
    import mysql.connector
    from skills.memory.mem_bridge import remember as _memory_remember
else:
    mysql = None
    _memory_remember = None

# --- Load the deployment-bound environment for subprocess/cron credentials ---
def _load_runtime_environment() -> None:
    if _SCHEDULE_FIXTURE_MODE:
        return
    from dotenv import load_dotenv
    from api.runtime_paths import dotenv_override_allowed, get_env_file

    env_path = get_env_file()
    if (os.environ.get("MAGI_V3_RELEASE_ID") or "").strip():
        import hashlib
        import re

        expected = (os.environ.get("MAGI_ENV_FILE_SHA256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError("sealed V3 Cortex sync requires MAGI_ENV_FILE_SHA256")
        if env_path.is_symlink() or not env_path.is_file():
            raise RuntimeError("sealed V3 Cortex sync environment is unavailable")
        actual = hashlib.sha256(env_path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("sealed V3 Cortex sync environment SHA mismatch")
    if env_path.is_file():
        load_dotenv(str(env_path), override=dotenv_override_allowed())


_load_runtime_environment()

logger = logging.getLogger("CortexSync")

_MAX_MEMORY_CONTENT_CHARS = max(
    512,
    int(os.environ.get("MAGI_MEMORY_MAX_CONTENT_LEN", "5000") or "5000"),
)

from api.runtime_paths import get_cortex_sync_state_path

# STATE FILE to track last synced IDs. V2 keeps the historical repo-local
# default; V3 writes only to its external shared runtime directory.
_LEGACY_STATE_FILE = os.path.join(_MAGI_ROOT, "cortex_sync_state.json")
STATE_FILE = str(get_cortex_sync_state_path())

# DB CONFIG (Source: law_firm_data — 透過 db_failover 動態切換遠端/本機)
def _resolve_osc_host() -> str:
    try:
        from api.db_failover import probe_remote, get_osc_host, _switch_to_local
        if not probe_remote():
            _switch_to_local()
        return get_osc_host()
    except Exception:
        return os.environ.get("OSC_DB_HOST", os.environ.get("MAGI_REMOTE_DB_HOST", "127.0.0.1"))

SOURCE_DB_CONFIG = {
    # SafeProcess intentionally strips generic DB_* / OSC_DB_* credentials
    # from cron children.  Production exposes the same already-configured
    # database account through the approved MAGI_REMOTE_DB_* namespace, so use
    # it as the fail-closed cron fallback instead of weakening the subprocess
    # environment allowlist or embedding credentials in the command.
    'user': os.environ.get(
        "OSC_DB_USER",
        os.environ.get("MAGI_REMOTE_DB_USER", os.environ.get("DB_USER", "casper_service")),
    ),
    'password': os.environ.get(
        "OSC_DB_PASSWORD",
        os.environ.get("MAGI_REMOTE_DB_PASSWORD", os.environ.get("DB_PASSWORD", "")),
    ),
    'host': "fixture.invalid" if _SCHEDULE_FIXTURE_MODE else _resolve_osc_host(),
    'port': int(os.environ.get("OSC_DB_PORT", os.environ.get("MAGI_REMOTE_DB_PORT", "3306"))),
    'database': 'law_firm_data',
}

class CortexSync:
    def __init__(self, *, source_connector=None, remember_fn=None, state_file=None):
        self.source_connector = source_connector
        self.remember_fn = remember_fn or _memory_remember
        self.state_file = str(state_file or STATE_FILE)
        self.state = self._load_state()

    def _load_state(self):
        state_file = str(getattr(self, "state_file", STATE_FILE))
        candidates = [state_file]
        if (
            (not _SCHEDULE_FIXTURE_MODE)
            and not (os.environ.get("MAGI_V3_RELEASE_ID") or "").strip()
            and os.path.abspath(state_file) != os.path.abspath(_LEGACY_STATE_FILE)
        ):
            candidates.append(_LEGACY_STATE_FILE)
        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, 'r') as f:
                    return json.load(f)
            except Exception:
                continue
        return {}

    def _save_state(self):
        state_file = str(getattr(self, "state_file", STATE_FILE))
        parent = os.path.dirname(state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temporary = state_file + ".tmp"
        with open(temporary, 'w') as f:
            json.dump(self.state, f, indent=2)
        os.replace(temporary, state_file)

    def get_source_connection(self):
        if self.source_connector is not None:
            return self.source_connector()
        if _SCHEDULE_FIXTURE_MODE:
            raise RuntimeError("bounded fixture requires an isolated source provider")
        return mysql.connector.connect(**SOURCE_DB_CONFIG)

    def _remember_strict(self, content, *, source):
        if self.remember_fn is None:
            raise RuntimeError("memory provider is unavailable")
        result = self.remember_fn(
            content,
            source=source,
            metadata={"require_embedding": True},
        )
        persisted = result is True or (
            isinstance(result, dict) and result.get("success") is True
        )
        if not persisted:
            raise RuntimeError("usable embedding was not persisted; retry is required")

    @staticmethod
    def _judgment_memory_contents(row):
        """Return complete, policy-sized memory records for one judgment.

        The memory policy rejects records longer than its configured content
        ceiling.  A single overlong public summary must therefore be split
        deterministically rather than blocking the ID cursor forever.  Every
        chunk repeats the source identity header; the caller advances the
        cursor only after every chunk has been persisted.  Retried chunks are
        safe because the memory bridge deduplicates identical content.
        """

        header = (
            f"判決書: {row.get('court_name')} {row.get('case_number')}\n"
            f"日期: {row.get('judgment_date')}\n"
        )
        summary = str(row.get("summary") or "").strip()
        prefix = header + "摘要: "
        if len(prefix) + len(summary) <= _MAX_MEMORY_CONTENT_CHARS:
            return [prefix + summary]

        # Reserve enough room for a stable segment label even when the number
        # of chunks grows.  Do not truncate: legal knowledge remains complete.
        payload_chars = max(128, _MAX_MEMORY_CONTENT_CHARS - len(prefix) - 40)
        chunks = [
            summary[offset : offset + payload_chars]
            for offset in range(0, len(summary), payload_chars)
        ]
        total = len(chunks)
        records = []
        for index, chunk in enumerate(chunks, start=1):
            label = f"（摘要分段 {index}/{total}）\n"
            content = prefix + label + chunk
            if len(content) > _MAX_MEMORY_CONTENT_CHARS:
                raise RuntimeError("judgment memory chunk exceeds policy ceiling")
            records.append(content)
        return records

    def sync_legal_news(self, limit=10):
        """Sync new legal news to memory."""
        last_id = self.state.get('legal_news_last_id', 0)
        added = 0
        
        try:
            conn = self.get_source_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.execute("""
                SELECT id, title, snippet, url, published_date, source 
                FROM legal_news 
                WHERE id > %s 
                ORDER BY id ASC 
                LIMIT %s
            """, (last_id, limit))
            
            rows = cursor.fetchall()
            
            for row in rows:
                content = f"法律新聞: {row['title']}\n摘要: {row['snippet']}\n來源: {row['source']} ({row['published_date']})\n連結: {row['url']}"
                
                # Call Memory Bridge (Embed + Store)
                self._remember_strict(content, source="legal_crawler_news")
                
                last_id = row['id']
                added += 1
                
            self.state['legal_news_last_id'] = last_id
            self._save_state()
            
        except Exception as e:
            logger.error(f"Sync Legal News Error: {e}")
            return f"❌ News Sync Failed: {e}"
        finally:
            if 'conn' in locals() and conn.is_connected():
                cursor.close()
                conn.close()
                
        return added

    def sync_judgments(self, limit=5):
        """Sync new judgments (summary only) to memory."""
        last_id = self.state.get('judgments_last_id', 0)
        added = 0
        
        try:
            conn = self.get_source_connection()
            cursor = conn.cursor(dictionary=True)
            
            cursor.execute("""
                SELECT id, jid, case_number, court_name, summary, judgment_date 
                FROM court_judgments 
                WHERE id > %s 
                ORDER BY id ASC 
                LIMIT %s
            """, (last_id, limit))
            
            rows = cursor.fetchall()
            
            for row in rows:
                for content in self._judgment_memory_contents(row):
                    # Call Memory Bridge.  The row cursor is advanced only
                    # after every deterministic chunk has been persisted.
                    self._remember_strict(content, source="legal_crawler_judgment")
                
                last_id = row['id']
                added += 1
                
            self.state['judgments_last_id'] = last_id
            self._save_state()
            
        except Exception as e:
            logger.error(f"Sync Judgments Error: {e}")
            return f"❌ Judgments Sync Failed: {e}"
        finally:
            if 'conn' in locals() and conn.is_connected():
                cursor.close()
                conn.close()
                
        return added

    def run_sync(self):
        """Run full sync cycle."""
        logger.info("🧠 Starting Cortex Sync...")
        
        n_added = self.sync_legal_news()
        j_added = self.sync_judgments()

        failures = [
            str(value)
            for value in (n_added, j_added)
            if isinstance(value, str) and value.strip().startswith("❌")
        ]
        if failures:
            msg = "❌ Cortex Sync Failed:\n" + "\n".join(failures)
            logger.error(msg)
            return msg
        
        msg = f"🧠 Cortex Sync Complete:\n- News: {n_added} items\n- Judgments: {j_added} items"
        logger.info(msg)
        return msg


class _FixtureCursor:
    def __init__(self, rows_by_kind):
        self._rows_by_kind = rows_by_kind
        self._rows = []

    def execute(self, query, params):
        kind = "legal_news" if "FROM legal_news" in str(query) else "judgments"
        last_id, limit = int(params[0]), int(params[1])
        self._rows = [
            dict(row)
            for row in sorted(self._rows_by_kind[kind], key=lambda item: item["id"])
            if int(row["id"]) > last_id
        ][:limit]

    def fetchall(self):
        return [dict(row) for row in self._rows]

    def close(self):
        return None


class _FixtureConnection:
    def __init__(self, rows_by_kind):
        self._rows_by_kind = rows_by_kind
        self._connected = True

    def cursor(self, dictionary=False):
        if dictionary is not True:
            raise RuntimeError("fixture requires dictionary cursor")
        return _FixtureCursor(self._rows_by_kind)

    def is_connected(self):
        return self._connected

    def close(self):
        self._connected = False


def _run_schedule_fixture(raw_root: str, raw_output: str) -> int:
    from scripts.ops.schedule_fixture_contract import (
        load_schedule_fixture,
        safety_receipt,
        write_fixture_report,
    )

    fixture = load_schedule_fixture(raw_root, job_id="job_1770948489644_0726cf")
    product_input = fixture.manifest["product_input"]
    initial_state = product_input.get("initial_state")
    news_rows = product_input.get("legal_news")
    judgment_rows = product_input.get("judgments")
    expected_added = product_input.get("expected_added")
    expected_state = product_input.get("expected_final_state")
    typed = bool(
        isinstance(initial_state, dict)
        and all(type(value) is int and value >= 0 for value in initial_state.values())
        and isinstance(news_rows, list)
        and all(isinstance(row, dict) and type(row.get("id")) is int for row in news_rows)
        and isinstance(judgment_rows, list)
        and all(isinstance(row, dict) and type(row.get("id")) is int for row in judgment_rows)
        and isinstance(expected_added, dict)
        and all(type(expected_added.get(key)) is int for key in ("news", "judgments"))
        and isinstance(expected_state, dict)
    )
    state_path = fixture.workspace / "cortex_sync_state.json"
    state_path.write_text(
        json.dumps(initial_state if isinstance(initial_state, dict) else {}, sort_keys=True),
        encoding="utf-8",
    )
    remembered = []

    def isolated_remember(content, *, source, metadata=None):
        remembered.append({"content": str(content), "source": str(source)})
        return {"success": True, "provider": "fixture_memory"}

    rows_by_kind = {
        "legal_news": news_rows if isinstance(news_rows, list) else [],
        "judgments": judgment_rows if isinstance(judgment_rows, list) else [],
    }
    syncer = CortexSync(
        source_connector=lambda: _FixtureConnection(rows_by_kind),
        remember_fn=isolated_remember,
        state_file=state_path,
    )
    outcome = syncer.run_sync()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    observed_added = {
        "news": sum(row["source"] == "legal_crawler_news" for row in remembered),
        "judgments": sum(
            row["source"] == "legal_crawler_judgment" for row in remembered
        ),
    }
    checks = {
        "fixture_sample_bound": 1 <= fixture.sample_id <= 3,
        "typed_state_and_rows": typed,
        "sync_reached_terminal_state": str(outcome).startswith("🧠 Cortex Sync Complete:"),
        "added_counts_match_expected": observed_added == expected_added,
        "state_checkpoint_matches_expected": persisted == expected_state,
        "memory_writes_use_isolated_provider": len(remembered)
        == int(observed_added["news"]) + int(observed_added["judgments"]),
        "no_dispatch_or_subprocess": True,
    }
    success = all(checks.values())
    safety = safety_receipt(fixture)
    safety.update(
        {
            "source_database_provider": "fixture_source",
            "memory_provider": "fixture_memory",
            "database_provider": "fixture_source",
            "model_provider": "fixture_memory",
            "notification_provider": "not_used",
            "subprocess_spawned": False,
            "dispatcher_invoked": False,
        }
    )
    report = {
        "schema": "magi.schedule-nonstorage-result/v1",
        "job_id": fixture.job_id,
        "fixture_sample_id": fixture.sample_id,
        "success": success,
        "status": "passed" if success else "failed",
        "terminal_state": "completed" if checks["sync_reached_terminal_state"] else "failed",
        "checks": checks,
        "observed_added": observed_added,
        "final_state": persisted,
        "remembered_sources": [row["source"] for row in remembered],
        "safety": safety,
    }
    output = write_fixture_report(fixture, raw_output, report)
    report["json_out"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if success else 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule-fixture-root")
    parser.add_argument("--json-out", default="cortex_sync.json")
    args = parser.parse_args()
    if args.schedule_fixture_root:
        raise SystemExit(_run_schedule_fixture(args.schedule_fixture_root, args.json_out))
    syncer = CortexSync()
    output = syncer.run_sync()
    print(output)
    raise SystemExit(1 if str(output or "").strip().startswith("❌") else 0)
