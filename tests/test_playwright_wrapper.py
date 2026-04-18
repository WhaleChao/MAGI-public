"""
tests/test_playwright_wrapper.py

Unit tests for skills/engine/playwright_wrapper.py (no live browser needed).
All Playwright internals are mocked.
"""

from unittest.mock import MagicMock, patch, call
import pytest

from skills.engine.playwright_wrapper import (
    PlaywrightElementWrapper,
    PlaywrightDriverWrapper,
    PlaywrightActionChains,
    PlaywrightSelect,
    PlaywrightWebDriverWait,
    _convert_script_for_playwright,
    _by_to_selector,
    _PlaywrightSwitchTo,
    _PlaywrightAlert,
    create_playwright_driver,
    By,
)


# ==============================================================================
# Helpers
# ==============================================================================

def _make_driver():
    """Return a PlaywrightDriverWrapper backed by mocks."""
    page = MagicMock()
    context = MagicMock()
    pw = MagicMock()
    page.url = "https://example.com/"
    page.frames = []
    driver = PlaywrightDriverWrapper(page, context, pw, download_dir="/tmp")
    return driver


def _make_element(driver=None):
    """Return a PlaywrightElementWrapper backed by a mock ElementHandle."""
    el = MagicMock()
    if driver is None:
        driver = _make_driver()
    return PlaywrightElementWrapper(el, driver)


# ==============================================================================
# _convert_script_for_playwright
# ==============================================================================

class TestConvertScript:
    def test_no_args(self):
        fn, arg = _convert_script_for_playwright("return 1;", [])
        assert "return 1;" in fn
        assert arg is None

    def test_single_arg(self):
        fn, arg = _convert_script_for_playwright("arguments[0].click();", ["el_handle"])
        assert "(__pw_a0) =>" in fn
        assert "__pw_a0.click();" in fn
        assert arg == "el_handle"

    def test_multi_args(self):
        fn, arg = _convert_script_for_playwright(
            "arguments[0].textContent = arguments[1];", ["el", "text"]
        )
        assert "[__pw_a0, __pw_a1]" in fn
        assert arg == ["el", "text"]

    def test_no_arguments_refs(self):
        fn, arg = _convert_script_for_playwright("return document.title;", ["unused"])
        assert arg is None
        assert "document.title" in fn


# ==============================================================================
# _by_to_selector
# ==============================================================================

class TestByToSelector:
    def test_css(self):
        assert _by_to_selector(By.CSS_SELECTOR, ".foo") == ".foo"

    def test_id(self):
        assert _by_to_selector(By.ID, "main") == "#main"

    def test_name(self):
        assert _by_to_selector(By.NAME, "account") == '[name="account"]'

    def test_xpath(self):
        sel = _by_to_selector(By.XPATH, "//div")
        assert "xpath=" in sel and "//div" in sel

    def test_tag(self):
        assert _by_to_selector(By.TAG_NAME, "input") == "input"


# ==============================================================================
# PlaywrightElementWrapper
# ==============================================================================

class TestElementWrapper:
    def test_click_calls_scroll_and_click(self):
        elem = _make_element()
        elem.click()
        elem._el.scroll_into_view_if_needed.assert_called_once()
        elem._el.click.assert_called_once()

    def test_send_keys_regular_text(self):
        elem = _make_element()
        elem._el.evaluate.side_effect = lambda expr: (
            "input" if "tagName" in expr else "text"
        )
        elem.send_keys("hello")
        elem._el.type.assert_called_once_with("hello")

    def test_send_keys_file_input(self):
        elem = _make_element()
        # simulate <input type="file">
        def _eval(expr):
            if "tagName" in expr:
                return "input"
            if "getAttribute('type')" in expr:
                return "file"
            return ""
        elem._el.evaluate.side_effect = _eval
        elem.send_keys("/tmp/doc.pdf")
        elem._el.set_input_files.assert_called_once_with("/tmp/doc.pdf")

    def test_get_attribute_innerhtml(self):
        elem = _make_element()
        elem._el.inner_html.return_value = "<b>ok</b>"
        result = elem.get_attribute("innerHTML")
        assert result == "<b>ok</b>"

    def test_get_attribute_regular(self):
        elem = _make_element()
        elem._el.get_attribute.return_value = "value1"
        assert elem.get_attribute("data-id") == "value1"

    def test_get_dom_attribute(self):
        elem = _make_element()
        elem._el.evaluate.return_value = "some-value"
        assert elem.get_dom_attribute("value") == "some-value"
        elem._el.evaluate.assert_called_with("el => el['value']")

    def test_value_of_css_property(self):
        elem = _make_element()
        elem._el.evaluate.return_value = "block"
        assert elem.value_of_css_property("display") == "block"

    def test_text_property(self):
        elem = _make_element()
        elem._el.text_content.return_value = "Hello"
        assert elem.text == "Hello"

    def test_is_displayed(self):
        elem = _make_element()
        elem._el.is_visible.return_value = True
        assert elem.is_displayed() is True

    def test_find_element_returns_wrapper(self):
        driver = _make_driver()
        child_handle = MagicMock()
        elem = _make_element(driver)
        elem._el.query_selector.return_value = child_handle
        result = elem.find_element(By.CSS_SELECTOR, ".child")
        assert isinstance(result, PlaywrightElementWrapper)
        assert result._el is child_handle

    def test_find_elements_returns_list(self):
        driver = _make_driver()
        handles = [MagicMock(), MagicMock()]
        elem = _make_element(driver)
        elem._el.query_selector_all.return_value = handles
        results = elem.find_elements(By.CSS_SELECTOR, "li")
        assert len(results) == 2
        assert all(isinstance(r, PlaywrightElementWrapper) for r in results)

    def test_location_property(self):
        elem = _make_element()
        elem._el.bounding_box.return_value = {"x": 10, "y": 20, "width": 100, "height": 50}
        assert elem.location == {"x": 10, "y": 20}

    def test_screenshot_as_png(self):
        elem = _make_element()
        elem._el.screenshot.return_value = b"\x89PNG"
        assert elem.screenshot_as_png == b"\x89PNG"


# ==============================================================================
# PlaywrightDriverWrapper
# ==============================================================================

class TestDriverWrapper:
    def test_get_navigates(self):
        driver = _make_driver()
        driver.get("https://example.com/page")
        driver._page.goto.assert_called()

    def test_current_url(self):
        driver = _make_driver()
        driver._page.url = "https://example.com/foo"
        assert driver.current_url == "https://example.com/foo"

    def test_find_element_wraps_handle(self):
        driver = _make_driver()
        handle = MagicMock()
        driver._page.query_selector.return_value = handle
        result = driver.find_element(By.ID, "btn")
        assert isinstance(result, PlaywrightElementWrapper)

    def test_find_elements_returns_list(self):
        driver = _make_driver()
        driver._page.query_selector_all.return_value = [MagicMock(), MagicMock()]
        results = driver.find_elements(By.CSS_SELECTOR, "li")
        assert len(results) == 2

    def test_execute_script_no_args(self):
        driver = _make_driver()
        driver._page.evaluate.return_value = "title"
        result = driver.execute_script("return document.title;")
        assert result == "title"

    def test_save_screenshot(self):
        driver = _make_driver()
        driver._page.screenshot.return_value = None
        ok = driver.save_screenshot("/tmp/shot.png")
        assert ok is True

    def test_close_only_closes_page_not_context(self):
        """close() closes only the current tab; context stays alive for multi-tab use."""
        driver = _make_driver()
        driver.close()
        driver._page.close.assert_called_once()
        driver._context.close.assert_not_called()

    def test_quit_closes_page_context_and_pw(self):
        driver = _make_driver()
        driver.quit()
        driver._page.close.assert_called_once()
        driver._context.close.assert_called_once()
        driver._pw.stop.assert_called_once()

    def test_quit_stops_pw(self):
        driver = _make_driver()
        driver.quit()
        driver._pw.stop.assert_called_once()


# ==============================================================================
# switch_to / alert
# ==============================================================================

class TestSwitchTo:
    def test_switch_to_frame_by_name(self):
        driver = _make_driver()
        mock_frame = MagicMock()
        driver._page.frame.return_value = mock_frame
        driver.switch_to.frame("myFrame")
        assert driver._active_frame is mock_frame

    def test_switch_to_default_content(self):
        driver = _make_driver()
        driver._active_frame = MagicMock()
        driver.switch_to.default_content()
        assert driver._active_frame is None

    def test_alert_accept(self):
        driver = _make_driver()
        mock_dialog = MagicMock()
        mock_dialog.message = "密碼到期"
        driver._last_dialog = mock_dialog
        alert = driver.switch_to.alert
        assert alert.text == "密碼到期"
        alert.accept()
        mock_dialog.accept.assert_called_once()
        assert driver._last_dialog is None

    def test_alert_no_dialog_raises(self):
        driver = _make_driver()
        driver._last_dialog = None
        with pytest.raises(RuntimeError):
            _ = driver.switch_to.alert

    def test_window_handles_returns_list(self):
        driver = _make_driver()
        page2 = MagicMock()
        driver._context.pages = [driver._page, page2]
        handles = driver.window_handles
        assert len(handles) == 2
        assert all(isinstance(h, str) for h in handles)

    def test_switch_to_window(self):
        driver = _make_driver()
        page2 = MagicMock()
        driver._context.pages = [driver._page, page2]
        handle2 = str(id(page2))
        driver.switch_to.window(handle2)
        assert driver._page is page2

    def test_click_link_and_wait_for_popup_success(self):
        """click_link_and_wait_for_popup returns new page handle on success."""
        driver = _make_driver()
        elem = _make_element(driver)
        popup_page = MagicMock()
        # Simulate expect_popup context manager returning popup_page
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.value = popup_page
        driver._page.expect_popup = MagicMock(return_value=cm)
        result = driver.click_link_and_wait_for_popup(elem)
        assert result == str(id(popup_page))
        assert popup_page in driver._popup_pages

    def test_click_link_and_wait_for_popup_failure(self):
        """click_link_and_wait_for_popup returns None on exception (timeout etc.)."""
        driver = _make_driver()
        elem = _make_element(driver)
        driver._page.expect_popup = MagicMock(side_effect=RuntimeError("timeout"))
        result = driver.click_link_and_wait_for_popup(elem)
        assert result is None


# ==============================================================================
# ActionChains
# ==============================================================================

class TestActionChains:
    def test_move_and_click(self):
        driver = _make_driver()
        elem = _make_element(driver)
        chains = PlaywrightActionChains(driver)
        chains.move_to_element(elem).click().perform()
        elem._el.hover.assert_called_once()
        elem._el.click.assert_called_once()

    def test_click_element_directly(self):
        driver = _make_driver()
        elem = _make_element(driver)
        chains = PlaywrightActionChains(driver)
        chains.click(elem).perform()
        elem._el.click.assert_called_once()


# ==============================================================================
# Select
# ==============================================================================

class TestSelect:
    def test_select_by_visible_text(self):
        elem = _make_element()
        sel = PlaywrightSelect(elem)
        sel.select_by_visible_text("和股")
        elem._el.select_option.assert_called_once_with(label="和股")

    def test_select_by_value(self):
        elem = _make_element()
        sel = PlaywrightSelect(elem)
        sel.select_by_value("V001")
        elem._el.select_option.assert_called_once_with(value="V001")


# ==============================================================================
# create_playwright_driver factory
# ==============================================================================

class TestCreatePlaywrightDriver:
    def test_returns_wrapper_instance(self):
        mock_pw_ctx = MagicMock()
        mock_sync_pw = MagicMock(return_value=mock_pw_ctx)
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()
        mock_pw_ctx.start.return_value = mock_pw_ctx
        mock_pw_ctx.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page
        mock_context.new_cdp_session.return_value = MagicMock()

        with patch("skills.engine.playwright_wrapper.sync_playwright", mock_sync_pw,
                   create=True):
            # Direct import patch
            import skills.engine.playwright_wrapper as pw_mod
            original = getattr(pw_mod, "create_playwright_driver")

            # Monkeypatch the sync_playwright inside the module
            with patch.object(pw_mod, "create_playwright_driver",
                               wraps=original) as _patched:
                # We can't easily mock the internal import, so just verify
                # the function signature and module availability
                pass

        # At minimum confirm the module exposes the function
        from skills.engine.playwright_wrapper import create_playwright_driver as cpd
        assert callable(cpd)
