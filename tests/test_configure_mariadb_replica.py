from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "configure_mariadb_replica.py"


def _module():
    spec = importlib.util.spec_from_file_location("configure_mariadb_replica", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_change_master_dry_run_redacts_password():
    mod = _module()
    target = mod.ReplicaTarget(
        host="100.97.29.92",
        port=3306,
        user="repl",
        password="pw",
        server_id=2,
        use_gtid=True,
    )

    sql, params, display = mod._build_change_master(target)

    assert "MASTER_PASSWORD=%s" in sql
    assert "pw" in params
    assert "pw" not in display
    assert "MASTER_PASSWORD='***'" in display
    assert "MASTER_USE_GTID=slave_pos" in display


def test_change_master_requires_file_position_without_gtid():
    mod = _module()
    target = mod.ReplicaTarget(
        host="100.97.29.92",
        port=3306,
        user="repl",
        password="pw",
        server_id=2,
        use_gtid=False,
    )

    try:
        mod._build_change_master(target)
    except ValueError as exc:
        assert "master-log-file" in str(exc) or "MASTER_LOG_FILE" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_managed_config_block_sets_server_id_and_row_format():
    mod = _module()
    block = mod._managed_config_block(2, relay_log_prefix="magi-relay-bin")

    assert "server-id=2" in block
    assert "relay-log=magi-relay-bin" in block
    assert "binlog-format=ROW" in block
    assert mod.CONFIG_BEGIN in block
    assert mod.CONFIG_END in block
