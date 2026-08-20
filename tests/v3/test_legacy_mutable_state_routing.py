from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _metadata_snapshot() -> dict[str, tuple[int, int]]:
    roots = (
        ROOT / "api",
        ROOT / "skills" / "memory",
        ROOT / "skills" / "ops",
        ROOT / "scripts" / "ops",
        ROOT / ".agent",
        ROOT / ".runtime",
        ROOT / "static",
    )
    rows: dict[str, tuple[int, int]] = {}
    for base in roots:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                stat = path.stat()
                rows[str(path.relative_to(ROOT))] = (stat.st_size, stat.st_mtime_ns)
    return rows


def test_v3_legacy_modules_write_only_external_runtime_state(tmp_path: Path) -> None:
    agent = tmp_path / "shared" / "agent"
    runtime = tmp_path / "shared" / "runtime"
    mutable_static = tmp_path / "shared" / "static"
    env_file = tmp_path / "candidate.env"
    env_file.write_text("DISCORD_BOT_TOKEN=dummy\n", encoding="utf-8")
    before = _metadata_snapshot()
    environment = os.environ.copy()
    environment.update(
        {
            "DISCORD_BOT_TOKEN": "dummy",
            "MAGI_AGENT_DIR": str(agent),
            "MAGI_RUNTIME_DIR": str(runtime),
            "MAGI_MUTABLE_STATIC_DIR": str(mutable_static),
            "MAGI_V3_STATE_DIR": str(tmp_path / "state" / "supervisor"),
            "MAGI_ROOT": str(ROOT),
            "MAGI_ROOT_DIR": str(ROOT),
            "MAGI_ENV_FILE": str(env_file),
            "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
            "MAGI_INTERNAL_CRON_ENABLED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT),
        }
    )
    code = r'''
import importlib
import json
from pathlib import Path

root = Path(__import__('os').environ['MAGI_ROOT_DIR']).resolve()
agent = Path(__import__('os').environ['MAGI_AGENT_DIR']).resolve()
runtime = Path(__import__('os').environ['MAGI_RUNTIME_DIR']).resolve()
static = Path(__import__('os').environ['MAGI_MUTABLE_STATIC_DIR']).resolve()

from skills.memory import message_queue
assert Path(message_queue.__file__).resolve().is_relative_to(root)
assert Path(message_queue._DB_PATH).resolve().is_relative_to(agent)
message_queue.get_queue()

from scripts.ops import osc_shell_nas_helper as osc
assert Path(osc.__file__).resolve().is_relative_to(root)
assert osc._RUNTIME_DIR == runtime
class FakeServer:
    def __init__(self, *args, **kwargs): self.timeout = None
    def serve_forever(self): return None
    def server_close(self): return None
osc.HTTPServer = FakeServer
assert osc.main() == 0

from skills.ops import heartbeat
assert Path(heartbeat.__file__).resolve().is_relative_to(root)
assert Path(heartbeat.STATUS_FILE).resolve().is_relative_to(static)
assert Path(heartbeat._AGENT_DIR).resolve() == agent

from skills.ops import file_review_auto_worker as review
assert Path(review.__file__).resolve().is_relative_to(root)
for path in (review.LOCK_PATH, review.LOG_STATE_PATH, review.DOWNLOAD_OWNERSHIP_PATH):
    assert path.resolve().is_relative_to(static)
review._write_state({'ok': True})

from api.webhooks import telegram
assert Path(telegram.__file__).resolve().is_relative_to(root)
assert Path(telegram.AGENT_DIR).resolve() == agent
telegram._append_channel_delivery_audit({'kind': 'state-routing-test'})
assert telegram._save_telegram_channel_state({'notifyTo': [], 'topicMap': {}})
telegram._save_telegram_poll_offset(7)

from api import discord_bot
assert Path(discord_bot.__file__).resolve().is_relative_to(root)
assert Path(discord_bot._AGENT_DIR).resolve() == agent
discord_bot._append_channel_delivery_audit({'kind': 'state-routing-test'})
discord_bot._save_last_channel_id('test-channel')

print(json.dumps({'agent': str(agent), 'runtime': str(runtime), 'static': str(static)}))
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "agent": str(agent),
        "runtime": str(runtime),
        "static": str(mutable_static),
    }
    assert (agent / "mq" / "message_queue.db").is_file()
    assert (agent / "channel_delivery_audit.jsonl").is_file()
    assert (agent / "telegram_channel_state.json").is_file()
    assert (agent / "telegram_poll_offset.json").is_file()
    assert (agent / "discord_last_channel.json").is_file()
    assert (mutable_static / "file_review_auto_state.json").is_file()
    assert _metadata_snapshot() == before
