import asyncio

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_backend import GrokBackend
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
        if path.startswith("/json/new"):
            return {"id": "T1", "webSocketDebuggerUrl": "ws://g"}
        return None

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


def session_backend(tmp_path, existing_tabs=(), created=None):
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
            return list(existing_tabs)
        if path.startswith("/json/new"):
            return created
        return None

    page_obj = FakePage()
    b = GrokBackend(9340, tmp_path / "prof", None, fetch_json=fetch_json, ensure=ensure, connect=connect,
                    make_page=lambda cdp, **kw: page_obj)
    return b, conn, fetched


NEW_TAB = {"id": "T1", "webSocketDebuggerUrl": "ws://new"}


def test_every_session_works_in_its_own_tab_and_never_takes_a_tab_the_user_has_open(tmp_path):
    """Окно Grok владелец использует и руками: драйвер не должен уводить его страницу и сбрасывать его композер."""
    users_tab = page("https://grok.com/imagine", "ws://users")
    b, conn, fetched = session_backend(tmp_path, existing_tabs=[users_tab], created=NEW_TAB)
    asyncio.run(b.resume(POST, "video", Rec()))
    assert conn.ws == "ws://new"                                        # подключились к своей вкладке, не к чужой
    assert ("/json/new?about:blank", "PUT") in fetched


def test_the_own_tab_is_closed_afterwards_even_when_the_page_fails(tmp_path):
    b, conn, fetched = session_backend(tmp_path, created=NEW_TAB)
    b._make_page = lambda cdp, **kw: FakePage(fail=DriverError("файл повреждён"))
    with pytest.raises(DriverError, match="повреждён"):
        asyncio.run(b.resume(POST, "video", Rec()))
    assert conn.closed is True and ("/json/close/T1", "GET") in fetched


def test_session_fails_clearly_when_a_tab_cannot_be_opened(tmp_path):
    b, _, _ = session_backend(tmp_path, created=None)
    with pytest.raises(DriverError, match="не удалось открыть вкладку"):
        asyncio.run(b.resume(POST, "video", Rec()))


def test_video_generation_drives_the_page_in_order_and_reports_the_post_id(tmp_path):
    page = FakePage()
    b, conn, ensured = wired(tmp_path, page)
    rec = Rec()
    opts = VideoOptions("лодка", (), "480p", 6, None, None)
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
        asyncio.run(b.generate(VideoOptions("x", (), None, None, None, None), Rec()))
    assert conn.closed is True
