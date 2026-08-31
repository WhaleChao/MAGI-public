from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from casper_ecosystem.law_firm_orchestrators import file_review_automation as module


@dataclass
class _Clock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _SwitchTo:
    def __init__(self, driver: "_Driver") -> None:
        self.driver = driver

    def default_content(self) -> None:
        self.driver.in_frame = False

    def frame(self, _frame: object) -> None:
        self.driver.in_frame = True


class _Driver:
    def __init__(self, clock: _Clock, *, main_ready_at: float | None = None, frame_ready_at: float | None = None) -> None:
        self.clock = clock
        self.main_ready_at = main_ready_at
        self.frame_ready_at = frame_ready_at
        self.in_frame = False
        self.switch_to = _SwitchTo(self)

    @property
    def page_source(self) -> str:
        if self.in_frame and self.frame_ready_at is not None and self.clock.now >= self.frame_ready_at:
            return "<html>聲請閱卷</html>"
        if self.main_ready_at is not None and self.clock.now >= self.main_ready_at:
            return "<frameset><frame name='mainFrame'></frameset>"
        return "<html>會員登入</html>"

    def find_elements(self, _by: object, tag: str) -> list[object]:
        if tag == "frame" and self.frame_ready_at is not None and self.clock.now >= self.frame_ready_at:
            return [object()]
        return []


def _sso(driver: _Driver, messages: list[str]) -> module.LawyerPortalSSO:
    instance = object.__new__(module.LawyerPortalSSO)
    instance.driver = driver
    instance.log_callback = messages.append
    return instance


def _patch_clock(monkeypatch, clock: _Clock) -> None:
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    monkeypatch.setattr(module, "By", SimpleNamespace(TAG_NAME="tag_name"))


def test_login_readiness_waits_for_delayed_main_frame(monkeypatch) -> None:
    clock = _Clock()
    messages: list[str] = []
    _patch_clock(monkeypatch, clock)

    assert _sso(_Driver(clock, main_ready_at=1.0), messages)._check_login_success() is True
    assert clock.now == 1.0
    assert any("mainFrame" in message for message in messages)


def test_login_readiness_reloads_dynamic_frame_handles(monkeypatch) -> None:
    clock = _Clock()
    messages: list[str] = []
    _patch_clock(monkeypatch, clock)

    assert _sso(_Driver(clock, frame_ready_at=1.5), messages)._check_login_success() is True
    assert clock.now == 1.5
    assert any("Frame[0]" in message for message in messages)


def test_login_readiness_deadline_remains_fail_closed(monkeypatch) -> None:
    clock = _Clock()
    messages: list[str] = []
    _patch_clock(monkeypatch, clock)
    monkeypatch.setenv("MAGI_FILE_REVIEW_LOGIN_RESULT_TIMEOUT_SEC", "5")

    assert _sso(_Driver(clock), messages)._check_login_success() is False
    assert clock.now == 5.0
    assert any("等待逾時" in message and "frames=0" in message for message in messages)
