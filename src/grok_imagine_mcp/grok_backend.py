"""Backend драйвера web: окно Chrome + CDP + страница Grok. Единственное место, где всё это собирается вместе."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from .cdp import CdpConnection, http_json
from .chrome import ensure_chrome
from .drivers import DriverError
from .grok_page import GROK_HOSTS, IMAGINE_URL, POST_ID_RE, POST_URL, GrokPage
from .web_options import ImageOptions, VideoOptions
from .web_types import BackendState, Generated, Reporter

_LOGIN_PREFIX = "accounts."


def pick_target(pages: list[dict]) -> str | None:
    """Адрес WebSocket вкладки: сначала вкладка на grok.com, затем любая обычная вкладка."""
    tabs = [p for p in pages if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    for tab in tabs:
        if (urlsplit(tab.get("url", "")).hostname or "") in GROK_HOSTS:
            return tab["webSocketDebuggerUrl"]
    return tabs[0]["webSocketDebuggerUrl"] if tabs else None


class GrokBackend:
    def __init__(self, port: int, profile: Path, chrome_path: Path | None, *,
                 fetch_json=http_json, ensure=ensure_chrome, connect=CdpConnection.connect, make_page=GrokPage):
        self.port, self.profile, self.chrome_path = port, Path(profile), chrome_path
        self._fetch_json, self._ensure, self._connect, self._make_page = fetch_json, ensure, connect, make_page

    async def state(self) -> BackendState:
        """Не запускает окно и не открывает страницы: только смотрит на уже открытые вкладки."""
        if not await self._fetch_json(self.port, "/json/version"):
            return BackendState(chrome_up=False, logged_in=None, profile=str(self.profile))
        hosts = [urlsplit(p.get("url", "")).hostname or ""
                 for p in (await self._fetch_json(self.port, "/json/list") or []) if p.get("type") == "page"]
        if any(h in GROK_HOSTS for h in hosts):
            logged_in = True
        elif any(h.startswith(_LOGIN_PREFIX) for h in hosts):
            logged_in = False
        else:
            logged_in = None
        return BackendState(chrome_up=True, logged_in=logged_in, profile=str(self.profile))

    @asynccontextmanager
    async def _session(self):
        await self._ensure(self.port, self.profile, self.chrome_path)
        pages = await self._fetch_json(self.port, "/json/list") or []
        ws_url = pick_target(pages)
        if ws_url is None:
            created = await self._fetch_json(self.port, "/json/new?about:blank", method="PUT")
            ws_url = (created or {}).get("webSocketDebuggerUrl")
        if not ws_url:
            raise DriverError(f"в окне Chrome (порт {self.port}) нет вкладки, которой можно управлять")
        cdp = await self._connect(ws_url)
        try:
            await cdp.send("Page.bringToFront")
            yield self._make_page(cdp, profile=str(self.profile))
        finally:
            await cdp.close()

    async def generate(self, opts: VideoOptions | ImageOptions, reporter: Reporter) -> Generated:
        kind = "video" if isinstance(opts, VideoOptions) else "image"
        async with self._session() as page:
            await page.open(IMAGINE_URL)
            if kind == "video":
                await page.prepare_video(opts)
            else:
                await page.prepare_image(opts)
            await page.enter_prompt(opts.prompt)
            post_id = await page.submit(kind)              # у картинки страницы поста нет: None
            reporter.submitted(post_id)
            url = await page.wait_result(kind, post_id or "")
            reporter.phase("downloading")
            generated = await page.download(url, kind)
            if kind == "image" and page.last_image_count > 1:
                generated = replace(generated, note=f"Grok сгенерировал {page.last_image_count} картинок, "
                                                    "сохранена первая")
            return generated

    async def resume(self, post_id: str, kind: str, reporter: Reporter) -> Generated:
        if not POST_ID_RE.match(post_id):
            raise DriverError("в записи задачи некорректный post_id: возобновить нельзя")
        async with self._session() as page:
            await page.open(POST_URL.format(post_id=post_id), post=True)
            url = await page.wait_result(kind, post_id)
            reporter.phase("downloading")
            return await page.download(url, kind)
