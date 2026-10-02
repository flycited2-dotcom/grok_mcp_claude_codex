"""Страница Grok Imagine: всё, что зависит от вёрстки grok.com. При смене сайта ломается только этот файл.

Подписи элементов русские (подтверждены живой пробой 2026-09-30); английские варианты в таблицах — запасные
кандидаты, пробой не проверены. Меню параметров (Radix) открываются только настоящими событиями мыши,
обычные кнопки принимают click().
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cdp import CdpConnection, CdpError
from .drivers import DriverError
from .media_check import MediaError, check_mp4, detect_image_ext
from .web_options import ImageOptions, VideoOptions
from .web_types import Generated

IMAGINE_URL = "https://grok.com/imagine"
POST_URL = "https://grok.com/imagine/post/{post_id}"
POST_ID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
GROK_HOSTS = frozenset({"grok.com", "www.grok.com"})
IDLE_LIMIT_S = 45.0
LOAD_TIMEOUT_S = 45.0
IMAGE_SETTLE_S = 6.0            # сетка картинок не менялась столько секунд: догрузилась

L_MODE = {"video": ("Видео", "Video"), "image": ("Изображение", "Image")}
L_RESOLUTION = ("Разрешение видео", "Video resolution")
L_DURATION = ("Длительность видео", "Video duration")
L_ASPECT = ("Соотношение сторон", "Aspect ratio")
L_AUDIO = ("Звук видео", "Video sound", "Video audio")
L_UPLOAD = ("Загрузить", "Upload")
PROGRESS_WORDS = ("Создаётся", "Generating", "Creating")

# ---------- JS-фрагменты (метка в начале нужна тестам; __X__ подставляется перед отправкой) ----------

_STATE = r"""/* STATE */ (() => {
  const root = document.getElementById('grok-content-area') || document.body;
  const dialogs = [...document.querySelectorAll('[role=dialog],[role=alertdialog]')]
    .map(d => (d.innerText || '').trim().replace(/\s+/g, ' '))
    .filter(t => t && !/cookie|конфиденциальн|privacy/i.test(t));
  const post = __POST_ID__;
  const vids = [...document.querySelectorAll('video')].map(v => v.currentSrc || v.src).filter(s => s && (!post || s.includes(post)));
  // картинки приходят прямо на /imagine как data:-адреса: отдаём только начало, целиком их читает FIRST_IMAGE
  const imgs = [...document.querySelectorAll('img')].filter(i => i.getBoundingClientRect().width > 100 && i.naturalWidth >= 256)
    .map(i => i.currentSrc || i.src).filter(s => s && (s.startsWith('data:image/') || (post && s.includes(post)))).map(s => s.slice(0, 100));
  const text = (root.innerText || '').replace(/\s+/g, ' ').trim();
  const box = document.querySelector('[role=textbox]');
  return JSON.stringify({href: location.href, path: location.pathname, textbox: !!box,
    composer_empty: !!box && (box.innerText || '').trim() === '',
    ready: document.readyState === 'complete', progress: __PROGRESS_RE__.test(text), dialog: dialogs[0] || null,
    vids, imgs, text: text.slice(0, 200)});
})()"""

_FIRST_IMAGE = r"""/* FIRST_IMAGE */ (() => {
  const post = __POST_ID__;
  const i = [...document.querySelectorAll('img')].find(i => {
    const s = i.currentSrc || i.src;
    return i.getBoundingClientRect().width > 100 && i.naturalWidth >= 256 && s && (s.startsWith('data:image/') || (post && s.includes(post)));
  });
  return i ? (i.currentSrc || i.src) : null;
})()"""

_RECT = r"""/* RECT */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button,[role=radio]')].find(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && L.includes((e.getAttribute('aria-label') || '').trim());
  });
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
})()"""

_CLICK_RADIO = r"""/* CLICK_RADIO */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('[role=radio]')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  if (!b) return false;
  b.click();
  return true;
})()"""

_RADIO_CHECKED = r"""/* RADIO_CHECKED */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('[role=radio]')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  return !!b && b.getAttribute('aria-checked') === 'true';
})()"""

_MENU_ITEMS = r"""/* MENU_ITEMS */ (() => {
  const menu = [...document.querySelectorAll('[role=menu]')].find(m => m.getBoundingClientRect().width > 0 && m.querySelector('[role=menuitemradio]'));
  if (!menu) return null;
  return JSON.stringify([...menu.querySelectorAll('[role=menuitemradio]')].map(e => {
    const r = e.getBoundingClientRect();
    const text = (e.innerText || '').trim().replace(/\s+/g, ' ');
    return {text, first: text.split(' ')[0], x: r.x + r.width / 2, y: r.y + r.height / 2,
            disabled: e.getAttribute('aria-disabled') === 'true' || e.hasAttribute('data-disabled')};
  }));
})()"""

_MENU_OPEN = r"""/* MENU_OPEN */ [...document.querySelectorAll('[role=menu]')].some(m => m.getBoundingClientRect().width > 0)"""

_TRIGGER_TEXT = r"""/* TRIGGER_TEXT */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  return b ? (b.innerText || '').trim().split(/\s+/)[0] : null;
})()"""

_AUDIO_STATE = r"""/* AUDIO_STATE */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button')].find(e => e.getBoundingClientRect().width > 0 && L.includes((e.getAttribute('aria-label') || '').trim()));
  if (!b) return null;
  const v = b.getAttribute('aria-checked') ?? b.getAttribute('aria-pressed');
  return v === null ? 'unknown' : v;
})()"""

_PANEL_UPLOAD = r"""/* PANEL_UPLOAD */ (() => {
  const L = __LABELS__;
  const b = [...document.querySelectorAll('button')].find(e => !e.getAttribute('aria-label') && L.includes((e.innerText || '').trim()) && e.getBoundingClientRect().width > 0);
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return JSON.stringify({x: r.x + r.width / 2, y: r.y + r.height / 2});
})()"""

_ATTACHED = r"""/* ATTACHED */ [...document.querySelectorAll('button')].filter(e => e.getBoundingClientRect().width > 0 && /remove image|удалить изображение/i.test(e.getAttribute('aria-label') || '')).length"""
_MENTIONS = r"""/* MENTIONS */ (() => { const b = document.querySelector('[role=textbox]'); return b ? b.querySelectorAll('[data-mention-type]').length : 0; })()"""
_MENTION_REACHED = r"""/* MENTION_REACHED */ (() => { const b = document.querySelector('[role=textbox]'); return !!b && b.querySelectorAll('[data-mention-type]').length >= __N__; })()"""
_FOCUS = r"""/* FOCUS */ (() => { const t = document.querySelector('[role=textbox]'); if (!t) return false; t.focus(); return true; })()"""
_SUBMIT_READY = r"""/* SUBMIT_READY */ (() => { const b = document.querySelector('button[type=submit]'); return !!b && !b.disabled && b.getBoundingClientRect().width > 0; })()"""
_SUBMIT_CLICK = r"""/* SUBMIT_CLICK */ (() => { const b = document.querySelector('button[type=submit]'); if (!b || b.disabled) return false; b.click(); return true; })()"""
_LABELS = r"""/* LABELS */ JSON.stringify({
  labels: [...document.querySelectorAll('button,[role=radio]')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.x >= 260; }).map(e => (e.getAttribute('aria-label') || '').trim()).filter(Boolean),
  hidden: document.visibilityState === 'hidden'
})"""

_FETCH = r"""/* FETCH */ (async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include'});
    const b = new Uint8Array(await r.arrayBuffer());
    let s = '';
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return JSON.stringify({ok: r.ok, status: r.status, size: b.length, b64: r.ok ? btoa(s) : ''});
  } catch (e) { return JSON.stringify({err: String(e)}); }
})(__URL__)"""

_PROGRESS_RE = "/(" + "|".join(PROGRESS_WORDS) + ")/"


def _labels(js: str, labels) -> str:
    return js.replace("__LABELS__", json.dumps(list(labels)))


def check_asset_url(url: str) -> None:
    """fetch идёт из страницы с куками пользователя, поэтому берём только адреса самого Grok."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "grok.com" or host.endswith(".grok.com")):
        raise DriverError(f"Grok отдал результат с неожиданного адреса ({host or url[:40]}); скачивание отменено")


class GrokPage:
    def __init__(self, cdp: CdpConnection, *, profile: str = "", sleep=asyncio.sleep, now=time.monotonic,
                 poll_s: float = 1.0):
        self.cdp, self.profile, self._sleep, self._now, self.poll_s = cdp, profile, sleep, now, poll_s
        self.last_image_count = 0          # сколько картинок Grok выдал в последней генерации (для примечания)

    # ---------- низкий уровень ----------

    def _need_login(self) -> DriverError:
        return DriverError(f"Grok просит войти в аккаунт. Откройте окно Chrome (профиль {self.profile}), войдите "
                           "вручную и повторите. Драйвер логин, пароли и коды не вводит.")

    async def _state(self, post_id: str = "") -> dict:
        js = _STATE.replace("__POST_ID__", json.dumps(post_id)).replace("__PROGRESS_RE__", _PROGRESS_RE)
        return json.loads(await self.cdp.evaluate(js))

    async def _missing(self, what: str) -> DriverError:
        try:
            seen = json.loads(await self.cdp.evaluate(_LABELS))
            found, hidden = seen["labels"], seen["hidden"]
        except (CdpError, ValueError, TypeError, KeyError):
            found, hidden = [], False
        if hidden:          # в скрытом окне Chrome не рисует кадры, и меню не появляются: вина окна, а не вёрстки
            return DriverError(f"окно Chrome скрыто (закрыто другими окнами или свёрнуто), поэтому Grok не показал "
                               f"«{what}». Разверните окно Chrome (профиль {self.profile}) или закройте его: "
                               "сервер запустит новое, которое работает и под другими окнами.")
        shown = ", ".join(found[:30]) or "ничего не найдено"
        return DriverError(f"интерфейс Grok изменился или не успел загрузиться: не найден элемент «{what}». "
                           f"Подписи на странице: {shown}")

    async def reveal_window(self) -> None:
        """Свёрнутое окно Chrome не рисует страницу: разворачиваем его и выводим вперёд (развёрнутое не трогаем)."""
        try:
            info = await self.cdp.send("Browser.getWindowForTarget")
            if (info.get("bounds") or {}).get("windowState") == "minimized":
                await self.cdp.send("Browser.setWindowBounds", windowId=info["windowId"],
                                    bounds={"windowState": "normal"})
        except CdpError:
            pass                                   # старый Chrome без этого API: не критично
        await self.cdp.send("Page.bringToFront")

    async def _wait_for(self, js: str, timeout_s: float, what: str):
        deadline = self._now() + timeout_s
        while True:
            value = await self.cdp.evaluate(js)
            if value:
                return value
            if self._now() >= deadline:
                raise await self._missing(what)
            await self._sleep(0.4)

    async def _rect(self, labels) -> tuple[float, float] | None:
        raw = await self.cdp.evaluate(_labels(_RECT, labels))
        if not raw:
            return None
        point = json.loads(raw)
        return point["x"], point["y"]

    async def _real_click(self, x: float, y: float) -> None:
        await self.cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        await self.cdp.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        await self.cdp.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    async def _escape(self) -> None:
        for kind in ("keyDown", "keyUp"):
            await self.cdp.send("Input.dispatchKeyEvent", type=kind, key="Escape", code="Escape",
                                windowsVirtualKeyCode=27)

    # ---------- загрузка страницы ----------

    async def open(self, url: str, *, post: bool = False) -> None:
        """Открывает страницу и ждёт композер (post=True: страницу результата, ей достаточно загрузиться)."""
        await self.cdp.send("Page.enable")
        await self.cdp.send("Page.navigate", url=url)
        await self.cdp.wait_event("Page.loadEventFired", 30.0)       # иначе можно прочитать ещё старый документ
        deadline = self._now() + LOAD_TIMEOUT_S
        while True:
            st = await self._state()
            parts = urlsplit(st["href"])
            if (parts.hostname or "") not in GROK_HOSTS:
                raise self._need_login()
            if st["dialog"]:
                raise DriverError(f"Grok показал окно, которое мешает работе: {st['dialog'][:200]}")
            if st["textbox"] or (post and st["ready"]):
                return
            if self._now() >= deadline:
                raise DriverError(f"страница Grok не загрузилась за {int(LOAD_TIMEOUT_S)} с "
                                  f"(адрес: {parts.hostname}{parts.path})")
            await self._sleep(self.poll_s)

    # ---------- режим и параметры ----------

    async def select_mode(self, kind: str) -> None:
        labels = L_MODE[kind]
        if not await self.cdp.evaluate(_labels(_CLICK_RADIO, labels)):
            raise await self._missing(f"переключатель режима «{labels[0]}»")
        await self._wait_for(_labels(_RADIO_CHECKED, labels), 6.0, f"режим «{labels[0]}» не включился")

    async def choose_menu_option(self, button_labels, option: str) -> None:
        pos = await self._rect(button_labels)
        if pos is None:
            raise await self._missing(f"кнопка «{button_labels[0]}»")
        await self._real_click(*pos)
        raw = await self._wait_for(_MENU_ITEMS, 4.0, f"меню «{button_labels[0]}»")
        items = json.loads(raw)
        hit = next((i for i in items if i["first"] == option), None)
        if hit is None:
            await self._escape()
            raise DriverError(f"в меню «{button_labels[0]}» нет значения {option}. "
                              f"Есть: {', '.join(i['first'] for i in items)}")
        if hit["disabled"]:
            await self._escape()
            raise DriverError(f"значение {option} в меню «{button_labels[0]}» недоступно "
                              "(возможно, не входит в вашу подписку)")
        await self._real_click(hit["x"], hit["y"])
        await self._sleep(0.6)
        if await self.cdp.evaluate(_MENU_OPEN):
            await self._escape()
        # клик мог не попасть: значение на кнопке перечитываем, иначе вышло бы видео с настройками по умолчанию
        shown = await self.cdp.evaluate(_labels(_TRIGGER_TEXT, button_labels))
        if shown != option:
            raise DriverError(f"Grok не применил значение {option} в меню «{button_labels[0]}»: сейчас выбрано "
                              f"{shown}. Генерация не запущена, чтобы не получить не то, что заказано.")

    async def set_audio(self, wanted: bool) -> None:
        state = await self.cdp.evaluate(_labels(_AUDIO_STATE, L_AUDIO))
        if state is None:
            raise await self._missing(f"переключатель «{L_AUDIO[0]}»")
        if state not in ("true", "false"):
            raise DriverError(f"не удалось определить состояние переключателя «{L_AUDIO[0]}»")
        if (state == "true") != wanted:
            pos = await self._rect(L_AUDIO)
            if pos is None:
                raise await self._missing(f"переключатель «{L_AUDIO[0]}»")
            await self._real_click(*pos)
            await self._sleep(0.4)
            if (await self.cdp.evaluate(_labels(_AUDIO_STATE, L_AUDIO)) == "true") != wanted:
                raise DriverError(f"не удалось переключить «{L_AUDIO[0]}» на {'вкл' if wanted else 'выкл'}: "
                                  "Grok оставил прежнее состояние. Генерация не запущена.")

    async def attach_images(self, paths: list[Path]) -> None:
        """Все файлы идут одним выбором: Grok принимает пакет (проверено: 12 за раз). Одна картинка - первый кадр,
        две и больше - референсы."""
        await self.cdp.send("Page.enable")
        await self.cdp.send("Page.setInterceptFileChooserDialog", enabled=True)   # системное окно не должно появиться
        try:
            pos = await self._rect(L_UPLOAD)
            if pos is None:
                raise await self._missing(f"кнопка «{L_UPLOAD[0]}»")
            await self._real_click(*pos)
            await self._sleep(1.2)                                   # панель «Генерации / Загрузки» открывается
            raw = await self._wait_for(_labels(_PANEL_UPLOAD, L_UPLOAD), 6.0, "кнопка загрузки в панели")
            point = json.loads(raw)
            await self._real_click(point["x"], point["y"])
            event = await self.cdp.wait_event("Page.fileChooserOpened", 6.0)
            if event is None:
                raise DriverError("Grok не открыл выбор файла: не удалось приложить фото")
            await self.cdp.send("DOM.setFileInputFiles", files=[str(p) for p in paths],
                                backendNodeId=event["backendNodeId"])
            await self._wait_attached(len(paths))
        finally:
            try:
                await self.cdp.send("Page.setInterceptFileChooserDialog", enabled=False)
            except CdpError:
                pass

    async def _wait_attached(self, n: int) -> None:
        deadline = self._now() + 25.0 + 2.0 * n
        while True:
            have = await self.cdp.evaluate(_ATTACHED)
            if have == n:
                return
            if self._now() >= deadline:
                raise DriverError(f"приложились не все картинки: {have} из {n}. Повторите запрос; если повторяется, "
                                  "уменьшите число или размер картинок.")
            await self._sleep(0.5)

    async def prepare_video(self, opts: VideoOptions) -> None:
        await self.select_mode("video")
        if opts.image_paths:
            await self.attach_images(list(opts.image_paths))
        if opts.resolution:
            await self.choose_menu_option(L_RESOLUTION, opts.resolution)
        if opts.duration_s:
            await self.choose_menu_option(L_DURATION, f"{opts.duration_s}s")
        if opts.aspect_ratio:                                        # с фото его уже нет: драйвер передаёт None
            await self.choose_menu_option(L_ASPECT, opts.aspect_ratio)
        if opts.generate_audio is not None:
            await self.set_audio(opts.generate_audio)

    async def prepare_image(self, opts: ImageOptions) -> None:
        await self.select_mode("image")
        if opts.aspect_ratio:
            await self.choose_menu_option(L_ASPECT, opts.aspect_ratio)

    # ---------- промт, отправка, результат ----------

    async def enter_prompt(self, text: str) -> None:
        if not await self.cdp.evaluate(_FOCUS):
            raise await self._missing("поле ввода промта")
        # переносы строк схлопываем: перенос мог бы отправить форму раньше времени
        parts = [p for p in re.split(r"(@Image \d+)", re.sub(r"\s+", " ", text).strip()) if p]
        have = None
        for part in parts:
            if re.fullmatch(r"@Image \d+", part):
                if have is None:
                    have = await self.cdp.evaluate(_MENTIONS)
                await self._insert_mention(part[1:], have + 1)
                have += 1
            else:
                await self.cdp.send("Input.insertText", text=part)
        await self._wait_for(_SUBMIT_READY, 6.0, "кнопка отправки")

    async def _insert_mention(self, label: str, want: int) -> None:
        """Ссылка на картинку становится ссылкой, только если выбрать её из списка после «@» (проверено живьём:
        набранное текстом «@Image 2» остаётся обычным текстом)."""
        await self.cdp.send("Input.insertText", text="@")
        await self._sleep(0.5)
        await self.cdp.send("Input.insertText", text=label)             # список сужается до этой картинки
        await self._sleep(0.5)
        for kind in ("keyDown", "keyUp"):
            await self.cdp.send("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter", windowsVirtualKeyCode=13)
        deadline = self._now() + 3.0
        while not await self.cdp.evaluate(_MENTION_REACHED.replace("__N__", str(want))):
            if self._now() >= deadline:
                raise DriverError(f"не удалось вставить ссылку @{label}: Grok не добавил её в промт. "
                                  "Проверьте, что картинка с таким номером приложена.")
            await self._sleep(0.3)

    async def submit(self, kind: str = "video") -> str | None:
        """Видео: возвращает post_id со страницы результата. Картинка: страницы поста нет, возвращает None,
        когда Grok принял запрос (композер очистился или уже пошли картинки)."""
        if not await self.cdp.evaluate(_SUBMIT_CLICK):
            raise await self._missing("кнопка отправки")
        deadline = self._now() + 30.0
        while True:
            st = await self._state()
            if st["dialog"]:
                raise DriverError(f"Grok не принял запрос и показал окно: {st['dialog'][:200]}")
            if kind == "video":
                match = re.search(r"/imagine/post/([0-9a-f-]{36})", st["path"])
                if match:
                    return match.group(1)
            elif st["composer_empty"] or st["progress"] or st["imgs"]:
                return None
            if self._now() >= deadline:
                raise DriverError("Grok не принял запрос за 30 с: страница не отреагировала на отправку")
            await self._sleep(self.poll_s)

    async def wait_result(self, kind: str, post_id: str) -> str:
        """Видео: адрес файла. Картинка: Grok выдаёт сетку картинок прямо на странице; ждём, пока она
        перестанет меняться, и отдаём первую (data:-адрес целиком)."""
        idle_since, count, changed_at = None, 0, self._now()
        while True:
            st = await self._state(post_id)
            if st["dialog"]:
                raise DriverError(f"Grok показал окно и не довёл генерацию до конца: {st['dialog'][:200]}")
            media = st["vids"] if kind == "video" else st["imgs"]
            if len(media) != count:
                count, changed_at = len(media), self._now()
            if media and not st["progress"]:
                if kind == "video":
                    return media[0]
                if self._now() - changed_at >= IMAGE_SETTLE_S:
                    self.last_image_count = count
                    return await self.cdp.evaluate(_FIRST_IMAGE.replace("__POST_ID__", json.dumps(post_id)))
            if st["progress"] or media:
                idle_since = None
            else:
                if idle_since is None:
                    idle_since = self._now()
                if self._now() - idle_since > IDLE_LIMIT_S:
                    raise DriverError("Grok не показал ни прогресса, ни результата: возможно, запрос отклонён "
                                      f"(модерация) или исчерпан лимит. Текст страницы: {st['text']}")
            await self._sleep(self.poll_s * 3)

    async def download(self, url: str, kind: str) -> Generated:
        if kind == "image" and url.startswith("data:image/"):     # картинка уже в странице: скачивать нечего
            try:
                data = base64.b64decode(url.partition(",")[2], validate=True)
                return Generated(data, detect_image_ext(data))
            except (ValueError, MediaError) as e:                 # binascii.Error — подкласс ValueError
                raise DriverError(f"скачанный файл повреждён: {e}") from e
        check_asset_url(url)
        res = json.loads(await self.cdp.evaluate(_FETCH.replace("__URL__", json.dumps(url))))
        if "err" in res:
            raise DriverError(f"не удалось скачать результат из Grok: {res['err']}")
        if not res.get("ok"):
            raise DriverError(f"не удалось скачать результат из Grok: HTTP {res.get('status')}")
        data = base64.b64decode(res["b64"])
        try:
            if kind == "video":
                check_mp4(data)
                return Generated(data, ".mp4")
            return Generated(data, detect_image_ext(data))
        except MediaError as e:
            raise DriverError(f"скачанный файл повреждён: {e}") from e
