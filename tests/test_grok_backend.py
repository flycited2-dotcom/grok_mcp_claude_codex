import asyncio

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_backend import GrokBackend, pick_target
from grok_imagine_mcp.grok_page import IMAGINE_URL, POST_URL
from grok_imagine_mcp.web_options import ImageOptions, VideoOptions
from grok_imagine_mcp.web_types import BackendState, Generated

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
POST = "11111111-2222-3333-4444-555555555555"


class FakeConn:
    def __init__(self):
        self.sent, self.closed = [], False

    async def send(self, method, **params):
        self.sent.append(method)
        return {}

    async def close(self):
        self.closed = True


class FakePage:
    def __init__(self, post_id=POST, result=None, fail=None):
        self.calls, self.post_id, self.fail = [], post_id, fail
        self.result = result or Generated(b"DATA", ".mp4")
        self.last_image_count = 0

    async def reveal_window(self):
        self.calls.append("reveal")

    async def open(self, url, *, post=False):
        self.calls.append(("open", url, post))

    async def prepare_video(self, opts):
        self.calls.append("prepare_video")

    async def prepare_image(self, opts):
        self.calls.append("prepare_image")

    async def enter_prompt(self, text):
        self.calls.append(("prompt", text))

    async def submit(self, kind):
        self.calls.append(("submit", kind))
        return self.post_id

    async def wait_result(self, kind, post_id):
        self.calls.append(("wait", kind, post_id))
        return "SRC"

    async def download(self, url, kind):
        self.calls.append(("download", url, kind))
        if self.fail:
            raise self.fail
        return self.result


class Rec:
    def __init__(self):
        self.events = []

    def phase(self, name):
        self.events.append(("phase", name))

    def submitted(self, post_id):
        self.events.append(("submitted", post_id))


def wired(tmp_path, page):
    conn, ensured = FakeConn(), []

    async def ensure(*a, **k):
        ensured.append(a)

    async def connect(ws_url):
        return conn

    async def fetch_json(port, path, **kw):
        return [{"type": "page", "url": "https://grok.com/imagine", "webSocketDebuggerUrl": "ws://g"}]

    b = GrokBackend(9340, tmp_path / "prof", None, fetch_json=fetch_json, ensure=ensure, connect=connect,
                    make_page=lambda cdp, **kw: page)
    return b, conn, ensured


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


def test_pick_target_prefers_grok_then_login_then_blank_and_never_a_foreign_tab():
    grok, login = page("https://grok.com/imagine", "ws://grok"), page("https://accounts.x.ai/sign-in", "ws://login")
    blank, foreign = page("about:blank", "ws://blank"), page("https://example.com", "ws://foreign")
    assert pick_target([foreign, login, grok]) == "ws://grok"
    assert pick_target([foreign, blank, login]) == "ws://login"
    assert pick_target([foreign, blank]) == "ws://blank"
    assert pick_target([foreign]) is None                       # чужую вкладку пользователя не уводим на grok.com
    assert pick_target([page("chrome://x", "ws://ui", kind="browser_ui")]) is None
    assert pick_target([]) is None


def session_backend(tmp_path, lists, created=None):
    conn, fetched = FakeConn(), []
    conn.ws = None

    async def ensure(*a, **k):
        pass

    async def connect(ws_url):
        conn.ws = ws_url
        return conn

    async def fetch_json(port, path, **kw):
        fetched.append((path, kw.get("method", "GET")))
        if path == "/json/list":
            return lists.pop(0) if len(lists) > 1 else lists[0]
        if path.startswith("/json/new"):
            return created
        return None

    async def sleep(s):
        pass

    page_obj = FakePage()
    b = GrokBackend(9340, tmp_path / "prof", None, fetch_json=fetch_json, ensure=ensure, connect=connect,
                    make_page=lambda cdp, **kw: page_obj, sleep=sleep)
    return b, conn, fetched


def test_session_waits_for_the_tab_list_after_a_cold_start(tmp_path):
    grok = page("https://grok.com/imagine", "ws://grok")
    b, conn, fetched = session_backend(tmp_path, [[], [], [grok]])
    asyncio.run(b.resume(POST, "video", Rec()))
    assert conn.ws == "ws://grok" and not [p for p, _ in fetched if p.startswith("/json/new")]


def test_session_opens_its_own_tab_when_only_foreign_tabs_exist_and_closes_it_afterwards(tmp_path):
    foreign = page("https://example.com", "ws://foreign")
    b, conn, fetched = session_backend(tmp_path, [[foreign]], created={"id": "T1", "webSocketDebuggerUrl": "ws://new"})
    asyncio.run(b.resume(POST, "video", Rec()))
    assert conn.ws == "ws://new"                                        # чужая вкладка не тронута
    assert ("/json/new?about:blank", "PUT") in fetched and ("/json/close/T1", "GET") in fetched


def test_session_fails_clearly_when_no_tab_can_be_used_or_created(tmp_path):
    b, _, _ = session_backend(tmp_path, [[]], created=None)
    with pytest.raises(DriverError, match="нет вкладки"):
        asyncio.run(b.resume(POST, "video", Rec()))


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


def test_video_generation_drives_the_page_in_order_and_reports_the_post_id(tmp_path):
    page = FakePage()
    b, conn, ensured = wired(tmp_path, page)
    rec = Rec()
    opts = VideoOptions("лодка", None, "480p", 6, None, None)
    got = asyncio.run(b.generate(opts, rec))
    assert page.calls == ["reveal", ("open", IMAGINE_URL, False), "prepare_video", ("prompt", "лодка"),
                          ("submit", "video"), ("wait", "video", POST), ("download", "SRC", "video")]
    assert rec.events == [("submitted", POST), ("phase", "downloading")]
    assert got == Generated(b"DATA", ".mp4") and conn.closed and len(ensured) == 1


def test_image_generation_has_no_post_id_and_notes_the_extra_images(tmp_path):
    page = FakePage(post_id=None, result=Generated(PNG, ".png"))
    page.last_image_count = 10
    b, _, _ = wired(tmp_path, page)
    rec = Rec()
    got = asyncio.run(b.generate(ImageOptions("кот", None), rec))
    assert ("submit", "image") in page.calls and ("wait", "image", "") in page.calls and "prepare_image" in page.calls
    assert rec.events[0] == ("submitted", None)
    assert got.data == PNG and got.ext == ".png" and "10" in got.note and "первая" in got.note


def test_single_image_has_no_note(tmp_path):
    page = FakePage(post_id=None, result=Generated(PNG, ".png"))
    page.last_image_count = 1
    b, _, _ = wired(tmp_path, page)
    assert asyncio.run(b.generate(ImageOptions("кот", None), Rec())).note == ""


def test_resume_opens_the_post_page_and_skips_the_form(tmp_path):
    page = FakePage()
    b, conn, _ = wired(tmp_path, page)
    rec = Rec()
    asyncio.run(b.resume(POST, "video", rec))
    assert page.calls == ["reveal", ("open", POST_URL.format(post_id=POST), True), ("wait", "video", POST),
                          ("download", "SRC", "video")]
    assert rec.events == [("phase", "downloading")] and conn.closed


def test_connection_is_closed_even_when_the_page_fails(tmp_path):
    page = FakePage(fail=DriverError("файл повреждён"))
    b, conn, _ = wired(tmp_path, page)
    with pytest.raises(DriverError, match="повреждён"):
        asyncio.run(b.generate(VideoOptions("x", None, None, None, None, None), Rec()))
    assert conn.closed is True
