import asyncio

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_backend import GrokBackend, pick_target
from grok_imagine_mcp.web_types import BackendState


def backend(version, pages, tmp_path):
    async def fetch_json(port, path, **kw):
        return version if path == "/json/version" else pages

    return GrokBackend(9340, tmp_path / "prof", None, fetch_json=fetch_json)


def page(url, ws="ws://x/1", kind="page"):
    return {"type": kind, "url": url, "webSocketDebuggerUrl": ws}


def test_no_window_means_chrome_is_down_and_login_unknown(tmp_path):
    st = asyncio.run(backend(None, None, tmp_path).state())
    assert st == BackendState(chrome_up=False, logged_in=None, profile=str(tmp_path / "prof"))


def test_a_tab_on_grok_means_logged_in(tmp_path):
    st = asyncio.run(backend({"Browser": "c"}, [page("https://grok.com/imagine")], tmp_path).state())
    assert st.chrome_up is True and st.logged_in is True


def test_only_a_login_tab_means_logged_out(tmp_path):
    pages = [page("https://accounts.x.ai/sign-in"), page("chrome://newtab/", kind="browser_ui")]
    assert asyncio.run(backend({"Browser": "c"}, pages, tmp_path).state()).logged_in is False


def test_unrecognised_tabs_leave_login_unknown(tmp_path):
    st = asyncio.run(backend({"Browser": "c"}, [page("about:blank")], tmp_path).state())
    assert st.chrome_up is True and st.logged_in is None


def test_grok_tab_wins_over_login_tab(tmp_path):
    pages = [page("https://accounts.google.com/x"), page("https://grok.com/imagine")]
    assert asyncio.run(backend({"Browser": "c"}, pages, tmp_path).state()).logged_in is True


def test_pick_target_prefers_grok_then_any_page_then_none():
    grok, other = page("https://grok.com/imagine", "ws://grok"), page("https://example.com", "ws://other")
    assert pick_target([other, grok]) == "ws://grok"
    assert pick_target([other, page("chrome://x", "ws://ui", kind="browser_ui")]) == "ws://other"
    assert pick_target([page("chrome://x", "ws://ui", kind="service_worker")]) is None
    assert pick_target([]) is None


def test_resume_refuses_a_malformed_post_id_before_touching_chrome(tmp_path):
    calls = []

    async def ensure(*a, **k):
        calls.append(a)

    b = GrokBackend(9340, tmp_path, None, ensure=ensure)

    class R:
        def phase(self, n): ...
        def submitted(self, p): ...

    with pytest.raises(DriverError, match="post_id"):
        asyncio.run(b.resume("../../x", "video", R()))
    assert calls == []
