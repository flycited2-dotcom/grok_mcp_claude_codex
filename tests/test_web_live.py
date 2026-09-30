"""Живой тест драйвера web: реальный Chrome, реальный аккаунт Grok, расходует лимит подписки.

Запуск:  $env:GROK_WEB_LIVE='1'; .\\.venv\\Scripts\\pytest tests\\test_web_live.py -s
"""
import asyncio
import os
import struct
import zlib
from pathlib import Path

import pytest

from grok_imagine_mcp.drivers import build_driver
from grok_imagine_mcp.media_check import check_mp4, detect_image_ext
from grok_imagine_mcp.models import ImageRequest, VideoRequest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("GROK_WEB_LIVE") != "1", reason="живой тест: задайте GROK_WEB_LIVE=1")]


def _png(path: Path, w=512, h=768) -> None:
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            rows += bytes((250, 210, 60) if (x - 256) ** 2 + (y - 240) ** 2 < 70 ** 2
                          else (60, 140, 70) if y > 560 else (90 + y // 12, 150 + y // 16, 235 - y // 10))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def _driver(tmp_path):
    home = Path(os.environ.get("GROK_STATE_DIR") or Path.home() / ".grok_imagine_mcp")
    env = {**os.environ, "GROK_CHROME_PROFILE": os.environ.get("GROK_CHROME_PROFILE") or str(home / "chrome-profile")}
    return build_driver("web", tmp_path, tmp_path / "out", env)           # задачи и блокировка во временной папке


async def _until_done(drv, task_id, limit_s=600):
    waited = 0
    while waited < limit_s:
        info = await drv.task_status(task_id, 45)
        if info.status != "pending":
            return info
        waited += 45
    raise AssertionError("генерация не завершилась за отведённое время")


def test_live_text_to_video(tmp_path):
    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="A red paper boat floating on calm water, soft daylight",
                                                   duration_s=6, resolution="480p"))
        return await _until_done(drv, info.task_id)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    check_mp4(Path(info.output_path).read_bytes())


def test_live_photo_to_video(tmp_path):
    img = tmp_path / "start.png"
    _png(img)

    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="The sun slowly rises, gentle camera push-in",
                                                   image_path=str(img), duration_s=6, resolution="480p"))
        return await _until_done(drv, info.task_id)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    check_mp4(Path(info.output_path).read_bytes())


def test_live_text_to_image(tmp_path):
    async def main():
        drv = _driver(tmp_path)
        info = await drv.submit_image(ImageRequest(prompt="A small red paper boat on calm water, soft daylight"))
        return await _until_done(drv, info.task_id, limit_s=300)
    info = asyncio.run(main())
    assert info.status == "done", info.error
    detect_image_ext(Path(info.output_path).read_bytes())
