from __future__ import annotations

from pathlib import Path

import pytest

from magi_v3.config import load_settings
from magi_v3.errors import ConfigurationError
from magi_v3.runtime import CoreRuntime


def test_default_config_is_loopback_non_binding_and_policy_aligned(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "MAGI_V3_STATE_DIR": str(tmp_path / "state"),
            "MAGI_V3_HOST_ACTIVE_LOCK_PATH": str(tmp_path / "active-release.lock"),
        }
    )

    assert settings.bind_enabled is False
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 0
    assert settings.resource.interactive_reserve_mb == 8192
    assert settings.resource.max_heavy == 2
    assert settings.resource.max_browser == 1
    assert settings.resource.max_light == 2
    assert settings.resource.reserved_p0_light_slots == 1
    assert not settings.host_active_lock_path.is_relative_to(settings.state_dir)


def test_build_is_side_effect_free(tmp_path: Path) -> None:
    state_dir = tmp_path / "never-created"
    runtime = CoreRuntime.build(load_settings({"MAGI_V3_STATE_DIR": str(state_dir)}))

    assert runtime.settings.state_dir == state_dir
    assert not state_dir.exists()


def test_binding_requires_explicit_port(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="non-zero port"):
        load_settings(
            {
                "MAGI_V3_STATE_DIR": str(tmp_path),
                "MAGI_V3_BIND_ENABLED": "1",
                "MAGI_V3_BIND_PORT": "0",
            }
        )


def test_phase_one_rejects_non_loopback_binding(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="loopback"):
        load_settings(
            {
                "MAGI_V3_STATE_DIR": str(tmp_path),
                "MAGI_V3_BIND_HOST": "0.0.0.0",
            }
        )
