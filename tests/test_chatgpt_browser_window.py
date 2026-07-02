from platforms._browser_backend import BrowserBackendConfig
from platforms.chatgpt.browser_register import (
    _apply_camoufox_visible_window_limit,
    _new_browser_page,
)


def test_apply_camoufox_visible_window_limit_sets_1280_by_720_window_for_headed_camoufox():
    launch_opts = {"headless": False}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.camoufox(headless=False),
    )

    assert launch_opts["window"] == (1280, 720)


def test_apply_camoufox_visible_window_limit_skips_headless_camoufox():
    launch_opts = {"headless": True}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.camoufox(headless=True),
    )

    assert "window" not in launch_opts


def test_apply_camoufox_visible_window_limit_skips_bitbrowser():
    launch_opts = {"headless": False}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.bitbrowser(profile_id="profile-1"),
    )

    assert "window" not in launch_opts


def test_new_browser_page_uses_no_viewport_context_for_camoufox():
    calls = []

    class _Context:
        def new_page(self):
            calls.append(("context_new_page",))
            return "page"

    class _Browser:
        def new_context(self, **kwargs):
            calls.append(("new_context", kwargs))
            return _Context()

        def new_page(self):
            calls.append(("browser_new_page",))
            return "fallback"

    page = _new_browser_page(
        _Browser(),
        BrowserBackendConfig.camoufox(headless=False),
        log=lambda _msg: None,
    )

    assert page == "page"
    assert calls == [
        ("new_context", {"no_viewport": True}),
        ("context_new_page",),
    ]
