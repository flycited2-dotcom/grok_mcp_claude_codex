"""Общие типы драйвера web: так driver и backend не импортируют друг друга."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .web_options import ImageOptions, VideoOptions


@dataclass(frozen=True)
class BackendState:
    chrome_up: bool
    logged_in: bool | None          # None: неизвестно (окна нет или страница не определена)
    profile: str = ""


@dataclass(frozen=True)
class Generated:
    data: bytes
    ext: str                        # ".mp4", ".png", ".jpg" или ".webp"
    note: str = ""                  # попадает в «Примечания» задачи (например, «сохранена первая из 10»)


class Reporter(Protocol):
    def phase(self, name: str) -> None: ...

    def submitted(self, post_id: str | None) -> None:
        """post_id есть у видео; у картинки страницы поста нет, тогда None."""


class WebBackend(Protocol):
    async def state(self) -> BackendState: ...

    async def generate(self, opts: VideoOptions | ImageOptions, reporter: Reporter) -> Generated: ...

    async def resume(self, post_id: str, kind: str, reporter: Reporter) -> Generated: ...
