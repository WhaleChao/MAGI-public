from __future__ import annotations

from pathlib import Path

import pytest

from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.mutation import ArmedLaunchdRunner
from scripts.v3_cutover.probe import ReleaseSpec


def spec(tmp_path: Path) -> ReleaseSpec:
    root = tmp_path / "release"
    root.mkdir()
    pidfile = root / "release.pid"
    pidfile.write_text("123", encoding="utf-8")
    plist = tmp_path / "com.magi.v3.control.plist"
    plist.write_text("plist fixture", encoding="utf-8")
    return ReleaseSpec(
        "v3",
        root.resolve(),
        "magi-v3-test",
        pidfiles=(pidfile,),
        launchd_labels=("com.magi.v3.control",),
        launchd_plists={"com.magi.v3.control": plist},
    )


def test_missing_armed_token_cannot_reach_runner(tmp_path: Path) -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not run")

    with pytest.raises(CutoverError, match="mutation disabled"):
        ArmedLaunchdRunner((spec(tmp_path),), provided_token=None, environment_token="x" * 32, runner=fake_runner)
    assert calls == []


def test_even_matching_caller_supplied_tokens_cannot_reach_launchctl(tmp_path: Path) -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not run")

    token = "z" * 32
    with pytest.raises(CutoverError, match="mutation disabled"):
        ArmedLaunchdRunner(
            (spec(tmp_path),), provided_token=token, environment_token=token, runner=fake_runner
        )
    assert calls == []


@pytest.mark.parametrize("method", ["bootout", "bootstrap"])
def test_constructor_bypass_still_cannot_reach_mutation_method(method: str) -> None:
    runner = object.__new__(ArmedLaunchdRunner)
    with pytest.raises(CutoverError, match="mutation disabled"):
        getattr(runner, method)("v3", "com.magi.v3.control")
