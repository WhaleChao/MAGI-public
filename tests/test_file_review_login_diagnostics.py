from __future__ import annotations

from types import SimpleNamespace

from casper_ecosystem.law_firm_orchestrators import file_review_automation as mod


def test_lawyer_portal_sso_records_driver_bootstrap_failure(monkeypatch, tmp_path):
    sso = mod.LawyerPortalSSO(
        username="user",
        password="pass",
        download_folder=str(tmp_path),
        headless=True,
    )

    def _boom():
        raise RuntimeError("session not created: DevToolsActivePort file doesn't exist")

    monkeypatch.setattr(sso, "_setup_driver", _boom)

    assert sso.login(max_retries=1) is False
    assert sso.last_error_code == "driver_init_failed"
    assert "DevToolsActivePort" in sso.last_error_detail


def test_file_review_manager_exposes_last_login_error(monkeypatch, tmp_path):
    class FakeSSO:
        def __init__(self, **kwargs):
            self.driver = None
            self.last_error_code = "driver_init_failed"
            self.last_error_detail = "session not created: DevToolsActivePort file doesn't exist"

        def login(self):
            return False

        def close(self):
            return None

    monkeypatch.setattr(mod, "LawyerPortalSSO", FakeSSO)

    mgr = mod.FileReviewManager(
        username="user",
        password="pass",
        download_folder=str(tmp_path),
        headless=True,
    )

    assert mgr.login() is False
    assert mgr.last_login_error_code == "driver_init_failed"
    assert "DevToolsActivePort" in mgr.last_login_error_detail


def test_navigate_playwright_second_popup_continues_to_menu(monkeypatch, tmp_path):
    class FakeWait:
        def __init__(self, *args, **kwargs):
            pass

        def until(self, condition):
            return object()

    class FakeFrame:
        name = "menu"

        def evaluate(self, script):
            if "some(function" in script:
                return True
            return "clicked"

    class FakePage:
        frames = [FakeFrame(), FakeFrame()]

        def wait_for_load_state(self, *args, **kwargs):
            return None

    class FakeSwitch:
        def __init__(self, driver):
            self.driver = driver

        def default_content(self):
            return None

        def frame(self, frame):
            return None

        def window(self, handle):
            self.driver.switched.append(handle)
            self.driver.current = handle

    class FakeDriver:
        def __init__(self):
            self._page = FakePage()
            self._popup_pages = []
            self._context = None
            self.switch_to = FakeSwitch(self)
            self.switched = []
            self.current = "portal"
            self.clicks = 0
            self.title = "OLA"
            self.page_source = "ok" * 300

        def implicitly_wait(self, seconds):
            return None

        @property
        def window_handles(self):
            return ["portal", "popup-2"] if self.clicks >= 2 else ["portal"]

        @property
        def current_window_handle(self):
            return self.current

        @property
        def current_url(self):
            return "https://eefile.judicial.gov.tw/"

        def click_link_and_wait_for_popup(self, elem, timeout_ms=0):
            self.clicks += 1
            return None if self.clicks == 1 else "popup-2"

        def find_elements(self, *args, **kwargs):
            return []

        def execute_script(self, *args, **kwargs):
            return "complete"

        def close(self):
            return None

    monkeypatch.setattr(mod, "WebDriverWait", FakeWait)
    monkeypatch.setattr(
        mod,
        "EC",
        SimpleNamespace(
            presence_of_element_located=lambda locator: (lambda driver: object()),
            element_to_be_clickable=lambda locator: (lambda driver: object()),
        ),
    )
    monkeypatch.setattr(mod, "By", SimpleNamespace(XPATH="xpath", TAG_NAME="tag"))

    mgr = mod.FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.driver = FakeDriver()

    assert mgr.navigate_to_file_review() is True
    assert "popup-2" in mgr.driver.switched
    assert mgr.last_navigation_error_code == ""
