"""Проверки скачанных файлов: целый ли mp4 и какого типа картинка."""
from __future__ import annotations

import struct


class MediaError(ValueError):
    """Файл не прошёл проверку; текст показывается пользователю."""


def check_mp4(data: bytes) -> None:
    """Блоки верхнего уровня должны в сумме давать размер файла и включать ftyp, moov и mdat."""
    pos, kinds = 0, []
    while pos < len(data):
        if pos + 8 > len(data):
            raise MediaError("файл обрезан: хвост короче заголовка блока")
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if size == 1:                               # 64-битный размер
            if pos + 16 > len(data):
                raise MediaError("файл обрезан: неполный расширенный заголовок блока")
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
        elif size == 0:                             # блок тянется до конца файла
            size = len(data) - pos
        if size < 8:
            raise MediaError("битый заголовок блока")
        kinds.append(kind)
        pos += size
    if pos != len(data):
        raise MediaError(f"файл обрезан: блоки занимают {pos} байт, а файл {len(data)}")
    missing = [k.decode() for k in (b"ftyp", b"moov", b"mdat") if k not in kinds]
    if missing:
        raise MediaError("в файле нет блоков: " + ", ".join(missing))


def detect_image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise MediaError("скачанный файл не похож на картинку PNG/JPG/WEBP")
