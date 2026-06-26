from __future__ import annotations

from scripts.ops import purge_ops_logs_from_vectors as purge


def test_prefix_params_exclude_keep_prefixes():
    params = purge._prefix_params("magi_autopilot")

    assert params[0] == "magi_autopilot%"
    assert "magi_autopilot|%" in params[1:]
    assert "source NOT LIKE" in purge._prefix_where()


def test_chunked_batches_ids():
    assert list(purge._chunked([1, 2, 3], size=2)) == [[1, 2], [3]]
