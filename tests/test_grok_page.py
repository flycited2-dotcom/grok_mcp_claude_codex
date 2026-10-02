import asyncio
import base64
import json
import struct

import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.grok_page import L_DURATION, GrokPage, check_asset_url
from grok_imagine_mcp.web_options import VideoOptions


def _box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


GOOD_MP4 = _box(b"ftyp", b"isom0000") + _box(b"moov", b"m" * 20) + _box(b"mdat", b"d" * 100)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
URL = "https://assets.grok.com/users/u/generated/11111111-2222-3333-4444-555555555555/generated_video.mp4?x=1"
POST = "11111111-2222-3333-4444-555555555555"


class FakeCdp:
    """Подставляет ответы по метке JS-фрагмента. Список в ответе отдаётся по одному, последний повторяется."""

    def __init__(self, script=None):
        self.script = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (script or {}).items()}
        self.sent, self.events, self.evaluated, self.responses = [], {}, [], {}

    async def evaluate(self, js):
        self.evaluated.append(js)
        for key, queue in self.script.items():
            if key in js:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"неожиданный JS: {js[:70]}")

    async def send(self, method, **params):
        self.sent.append((method, params))
        reply = self.responses.get(method, {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def wait_event(self, name, timeout=5.0):
        return self.events.get(name)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def page(script=None, **kw):
    cdp, clock = FakeCdp(script), Clock()
    return GrokPage(cdp, profile="PROF", sleep=clock.sleep, now=clock.now, poll_s=1.0, **kw), cdp, clock


def state(**over):
    base = {"href": "https://grok.com/imagine", "path": "/imagine", "textbox": True, "ready": True,
            "composer_empty": False, "progress": False, "dialog": None, "vids": [], "imgs": [],
            "text": "Что мы должны imagine?"}
    return json.dumps({**base, **over})


def run(coro):
    return asyncio.run(coro)


# ---------- вход и загрузка ----------

def test_open_asks_to_log_in_when_redirected_to_the_login_host():
    p, _, _ = page({"/* STATE */": state(href="https://accounts.x.ai/sign-in", textbox=False)})
    with pytest.raises(DriverError) as e:
        run(p.open("https://grok.com/imagine"))
    assert "войти в аккаунт" in str(e.value) and "PROF" in str(e.value) and "не вводит" in str(e.value)


def test_open_returns_once_the_composer_is_there():
    p, cdp, _ = page({"/* STATE */": [state(textbox=False), state()]})
    run(p.open("https://grok.com/imagine"))
    assert ("Page.navigate", {"url": "https://grok.com/imagine"}) in cdp.sent


def test_open_times_out_with_the_address_in_the_message():
    p, _, _ = page({"/* STATE */": state(textbox=False)})
    with pytest.raises(DriverError, match="не загрузилась"):
        run(p.open("https://grok.com/imagine"))


def test_open_reports_a_blocking_dialog_but_ignores_cookie_banner():
    p, _, _ = page({"/* STATE */": state(dialog="Достигнут дневной лимит генераций")})
    with pytest.raises(DriverError, match="лимит"):
        run(p.open("https://grok.com/imagine"))


# ---------- элементы интерфейса ----------

def test_missing_mode_switch_lists_the_labels_that_are_on_the_page():
    p, _, _ = page({"/* CLICK_RADIO */": False,
                    "/* LABELS */": json.dumps({"labels": ["Звук видео", "Загрузить"], "hidden": False})})
    with pytest.raises(DriverError) as e:
        run(p.select_mode("video"))
    assert "интерфейс Grok изменился" in str(e.value) and "Звук видео" in str(e.value)


def test_missing_element_in_a_hidden_window_blames_the_window_not_the_interface():
    p, _, _ = page({"/* CLICK_RADIO */": False,
                    "/* LABELS */": json.dumps({"labels": ["Звук видео"], "hidden": True})})
    with pytest.raises(DriverError) as e:
        run(p.select_mode("video"))
    msg = str(e.value)
    assert "окно Chrome скрыто" in msg and "PROF" in msg and "интерфейс Grok изменился" not in msg


def test_reveal_window_restores_a_minimized_window_and_brings_it_to_front():
    p, cdp, _ = page()
    cdp.responses["Browser.getWindowForTarget"] = {"windowId": 7, "bounds": {"windowState": "minimized"}}
    run(p.reveal_window())
    assert ("Browser.setWindowBounds", {"windowId": 7, "bounds": {"windowState": "normal"}}) in cdp.sent
    assert cdp.sent[-1] == ("Page.bringToFront", {})


def test_reveal_window_leaves_a_maximized_window_alone():
    p, cdp, _ = page()
    cdp.responses["Browser.getWindowForTarget"] = {"windowId": 7, "bounds": {"windowState": "maximized"}}
    run(p.reveal_window())
    assert not [m for m, _ in cdp.sent if m == "Browser.setWindowBounds"]
    assert cdp.sent[-1] == ("Page.bringToFront", {})


def test_reveal_window_survives_a_chrome_without_the_window_api():
    from grok_imagine_mcp.cdp import CdpError
    p, cdp, _ = page()
    cdp.responses["Browser.getWindowForTarget"] = CdpError("'Browser.getWindowForTarget' wasn't found")
    run(p.reveal_window())
    assert cdp.sent[-1] == ("Page.bringToFront", {})


def _item(text, disabled=False):
    return {"text": text, "first": text.split()[0], "x": 10.0, "y": 20.0, "disabled": disabled}


def test_menu_value_that_does_not_exist_lists_the_available_ones():
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}),
                      "/* MENU_ITEMS */": json.dumps([_item("6s"), _item("10s"), _item("15s")])})
    with pytest.raises(DriverError) as e:
        run(p.choose_menu_option(L_DURATION, "8s"))
    assert "нет значения 8s" in str(e.value) and "6s, 10s, 15s" in str(e.value)
    assert any(m == "Input.dispatchKeyEvent" for m, _ in cdp.sent)            # меню закрыто Escape


def test_disabled_menu_value_is_reported_as_unavailable():
    p, _, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}),
                    "/* MENU_ITEMS */": json.dumps([_item("480p"), _item("1080p", disabled=True)])})
    with pytest.raises(DriverError, match="недоступно"):
        run(p.choose_menu_option(("Разрешение видео",), "1080p"))


def test_menu_value_is_chosen_with_real_mouse_events():
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 5, "y": 6}),
                      "/* MENU_ITEMS */": json.dumps([_item("6s"), _item("10s")]), "/* MENU_OPEN */": False,
                      "/* TRIGGER_TEXT */": "10s"})
    run(p.choose_menu_option(L_DURATION, "10s"))
    presses = [params for m, params in cdp.sent if m == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"]
    assert [(q["x"], q["y"]) for q in presses] == [(5, 6), (10.0, 20.0)]


def test_menu_choice_that_did_not_stick_is_an_error_not_a_silent_default():
    """Клик мог не попасть (перекрытие, сдвиг вёрстки): без перечитывания вышло бы видео 6 с вместо 10 с."""
    p, _, _ = page({"/* RECT */": json.dumps({"x": 5, "y": 6}),
                    "/* MENU_ITEMS */": json.dumps([_item("6s"), _item("10s")]), "/* MENU_OPEN */": False,
                    "/* TRIGGER_TEXT */": "6s"})
    with pytest.raises(DriverError) as e:
        run(p.choose_menu_option(L_DURATION, "10s"))
    assert "не применил значение 10s" in str(e.value) and "6s" in str(e.value)


def test_prompt_newlines_are_collapsed_so_the_prompt_is_not_sent_early():
    p, cdp, _ = page({"/* FOCUS */": True, "/* SUBMIT_READY */": True})
    run(p.enter_prompt("первая строка\n\nвторая   строка\r\nтретья"))
    assert ("Input.insertText", {"text": "первая строка вторая строка третья"}) in cdp.sent


def test_submit_returns_post_id_from_the_address():
    p, _, _ = page({"/* SUBMIT_CLICK */": True,
                    "/* STATE */": [state(path="/imagine"), state(path=f"/imagine/post/{POST}")]})
    assert run(p.submit()) == POST


def test_submit_without_the_page_moving_on_is_an_error():
    p, _, _ = page({"/* SUBMIT_CLICK */": True, "/* STATE */": state(path="/imagine")})
    with pytest.raises(DriverError, match="не принял запрос"):
        run(p.submit())


def test_audio_toggle_is_clicked_only_when_the_state_differs():
    p, cdp, _ = page({"/* AUDIO_STATE */": "true", "/* RECT */": json.dumps({"x": 1, "y": 2})})
    run(p.set_audio(True))
    assert not [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]
    p, cdp, _ = page({"/* AUDIO_STATE */": ["true", "false"], "/* RECT */": json.dumps({"x": 1, "y": 2})})
    run(p.set_audio(False))
    assert [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]


def test_audio_toggle_that_did_not_flip_is_an_error():
    p, _, _ = page({"/* AUDIO_STATE */": "true", "/* RECT */": json.dumps({"x": 1, "y": 2})})
    with pytest.raises(DriverError, match="не удалось переключить"):
        run(p.set_audio(False))


def test_attach_images_puts_all_files_through_one_intercepted_file_chooser(tmp_path):
    imgs = []
    for name in ("a.png", "b.png", "c.png"):
        f = tmp_path / name
        f.write_bytes(PNG)
        imgs.append(f)
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}), "/* PANEL_UPLOAD */": json.dumps({"x": 3, "y": 4}),
                      "/* ATTACHED */": [1, 2, 3]})                    # вложения появляются по мере загрузки
    cdp.events["Page.fileChooserOpened"] = {"backendNodeId": 42, "mode": "selectMultiple"}
    run(p.attach_images(imgs))
    assert ("DOM.setFileInputFiles", {"files": [str(f) for f in imgs], "backendNodeId": 42}) in cdp.sent
    assert ("Page.setInterceptFileChooserDialog", {"enabled": True}) in cdp.sent
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


def test_attach_images_fails_clearly_when_no_chooser_opens(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}), "/* PANEL_UPLOAD */": json.dumps({"x": 3, "y": 4})})
    with pytest.raises(DriverError, match="выбор файла"):
        run(p.attach_images([img]))
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


def test_attach_images_reports_how_many_actually_attached_when_some_are_missing(tmp_path):
    imgs = []
    for name in ("a.png", "b.png", "c.png"):
        f = tmp_path / name
        f.write_bytes(PNG)
        imgs.append(f)
    p, cdp, _ = page({"/* RECT */": json.dumps({"x": 1, "y": 2}), "/* PANEL_UPLOAD */": json.dumps({"x": 3, "y": 4}),
                      "/* ATTACHED */": 1})                              # приложилась одна из трёх
    cdp.events["Page.fileChooserOpened"] = {"backendNodeId": 42, "mode": "selectMultiple"}
    with pytest.raises(DriverError, match="приложились не все картинки.*1 из 3"):
        run(p.attach_images(imgs))
    assert cdp.sent[-1] == ("Page.setInterceptFileChooserDialog", {"enabled": False})


# ---------- ссылки @Image N в промте ----------

def test_a_mention_in_the_prompt_is_inserted_as_a_real_chip_picked_from_the_list():
    """Проверено живьём: набранный текстом «@Image 2» ссылкой не становится; чип появляется, только если после «@»
    выбрать картинку из списка (Enter)."""
    p, cdp, _ = page({"/* FOCUS */": True, "/* SUBMIT_READY */": True, "/* MENTIONS */": 0,
                      "/* MENTION_REACHED */": True})
    run(p.enter_prompt("Герой @Image 1 танцует\nс @Image 2 вместе"))
    typed = [params["text"] for m, params in cdp.sent if m == "Input.insertText"]
    assert typed == ["Герой ", "@", "Image 1", " танцует с ", "@", "Image 2", " вместе"]
    enters = [params for m, params in cdp.sent if m == "Input.dispatchKeyEvent" and params["key"] == "Enter"]
    assert len(enters) == 4                                                  # два нажатия: keyDown + keyUp


def test_a_mention_that_does_not_turn_into_a_chip_is_an_error():
    p, _, _ = page({"/* FOCUS */": True, "/* SUBMIT_READY */": True, "/* MENTIONS */": 0,
                    "/* MENTION_REACHED */": False})
    with pytest.raises(DriverError, match="не удалось вставить ссылку @Image 3"):
        run(p.enter_prompt("Танцует @Image 3"))


def test_a_prompt_without_mentions_does_not_ask_the_page_about_chips():
    p, cdp, _ = page({"/* FOCUS */": True, "/* SUBMIT_READY */": True})      # MENTIONS в сценарии нет: вызов упал бы
    run(p.enter_prompt("просто текст"))
    assert ("Input.insertText", {"text": "просто текст"}) in cdp.sent


# ---------- ожидание результата ----------

def test_wait_result_returns_the_url_after_progress_is_gone():
    p, _, _ = page({"/* STATE */": [state(progress=True), state(progress=True, vids=[URL]), state(vids=[URL])]})
    assert run(p.wait_result("video", POST)) == URL


def test_wait_result_for_images_waits_for_the_grid_to_settle_and_returns_the_first_image():
    a, b = "data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB"
    p, _, _ = page({"/* STATE */": [state(imgs=[a]), state(imgs=[a, b])],       # сетка догружается, потом замирает
                    "/* FIRST_IMAGE */": "data:image/jpeg;base64,QQ=="})
    assert run(p.wait_result("image", "")) == "data:image/jpeg;base64,QQ=="
    assert p.last_image_count == 2


def test_wait_result_for_images_does_not_return_while_progress_is_shown():
    a = "data:image/jpeg;base64,AAAA"
    p, _, _ = page({"/* STATE */": [state(imgs=[a], progress=True)] * 3 + [state(imgs=[a])],
                    "/* FIRST_IMAGE */": "data:image/jpeg;base64,QQ=="})
    assert run(p.wait_result("image", "")) == "data:image/jpeg;base64,QQ=="


def test_submit_image_is_accepted_when_the_composer_clears_and_has_no_post_id():
    p, _, _ = page({"/* SUBMIT_CLICK */": True,
                    "/* STATE */": [state(composer_empty=False), state(composer_empty=True)]})
    assert run(p.submit("image")) is None


def test_submit_image_is_accepted_when_images_already_appeared():
    p, _, _ = page({"/* SUBMIT_CLICK */": True, "/* STATE */": state(imgs=["data:image/jpeg;base64,AAAA"])})
    assert run(p.submit("image")) is None


def test_submit_image_not_accepted_when_nothing_changes():
    p, _, _ = page({"/* SUBMIT_CLICK */": True, "/* STATE */": state(composer_empty=False)})
    with pytest.raises(DriverError, match="не принял запрос"):
        run(p.submit("image"))


def test_wait_result_raises_on_a_blocking_dialog():
    p, _, _ = page({"/* STATE */": state(progress=True, dialog="Запрос отклонён модерацией")})
    with pytest.raises(DriverError, match="модерацией"):
        run(p.wait_result("video", POST))


def test_wait_result_gives_up_when_nothing_happens_and_shows_the_page_text():
    p, _, _ = page({"/* STATE */": state(text="Что-то пошло не так. Повторите попытку")})
    with pytest.raises(DriverError) as e:
        run(p.wait_result("video", POST))
    assert "ни прогресса, ни результата" in str(e.value) and "Повторите попытку" in str(e.value)


# ---------- скачивание ----------

def _fetched(data, **over):
    return json.dumps({"ok": True, "status": 206, "size": len(data), "b64": base64.b64encode(data).decode(), **over})


def test_download_returns_a_verified_video():
    p, _, _ = page({"/* FETCH */": _fetched(GOOD_MP4)})
    got = run(p.download(URL, "video"))
    assert got.data == GOOD_MP4 and got.ext == ".mp4"


def test_download_rejects_a_truncated_video():
    p, _, _ = page({"/* FETCH */": _fetched(GOOD_MP4[:-5])})
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download(URL, "video"))


def test_download_rejects_an_html_error_page_saved_as_video():
    p, _, _ = page({"/* FETCH */": _fetched(b"<html>403</html>")})
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download(URL, "video"))


def test_download_reports_network_failure_and_http_errors():
    p, _, _ = page({"/* FETCH */": json.dumps({"err": "TypeError: Failed to fetch"})})
    with pytest.raises(DriverError, match="не удалось скачать"):
        run(p.download(URL, "video"))
    p, _, _ = page({"/* FETCH */": json.dumps({"ok": False, "status": 403, "size": 0, "b64": ""})})
    with pytest.raises(DriverError, match="403"):
        run(p.download(URL, "video"))


def test_download_detects_the_image_type():
    p, _, _ = page({"/* FETCH */": _fetched(PNG)})
    got = run(p.download("https://assets.grok.com/users/u/generated/x/image.png", "image"))
    assert got.ext == ".png"


def test_download_decodes_an_inline_data_uri_image_without_fetching():
    p, cdp, _ = page()                                           # пустой сценарий: fetch в странице не нужен
    got = run(p.download("data:image/png;base64," + base64.b64encode(PNG).decode(), "image"))
    assert got.data == PNG and got.ext == ".png" and cdp.evaluated == []


def test_download_rejects_a_data_uri_that_is_not_an_image():
    p, _, _ = page()
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download("data:image/jpeg;base64," + base64.b64encode(b"<html>").decode(), "image"))
    with pytest.raises(DriverError, match="повреждён"):
        run(p.download("data:image/jpeg;base64,@@@@", "image"))


def test_a_data_uri_is_never_accepted_as_a_video_source():
    p, _, _ = page()
    with pytest.raises(DriverError, match="неожиданного адреса"):
        run(p.download("data:video/mp4;base64,AAAA", "video"))


def test_asset_url_must_be_https_on_grok_com_and_nothing_is_fetched_otherwise():
    check_asset_url("https://assets.grok.com/a.mp4")
    check_asset_url("https://grok.com/a.mp4")
    for bad in ("http://assets.grok.com/a.mp4", "https://evil.example/a.mp4", "https://grok.com.evil.example/a",
                "file:///C:/x.mp4"):
        with pytest.raises(DriverError, match="неожиданного адреса"):
            check_asset_url(bad)
    p, cdp, _ = page()                                           # пустой сценарий: любой evaluate упал бы
    with pytest.raises(DriverError, match="неожиданного адреса"):
        run(p.download("https://evil.example/a.mp4", "video"))
    assert cdp.evaluated == []


# ---------- подготовка целиком ----------

def test_prepare_video_applies_only_the_options_that_were_given():
    p, cdp, _ = page({"/* CLICK_RADIO */": True, "/* RADIO_CHECKED */": True})
    run(p.prepare_video(VideoOptions("x", (), None, None, None, None)))
    assert not [1 for m, _ in cdp.sent if m == "Input.dispatchMouseEvent"]       # ни одного меню не открывали
