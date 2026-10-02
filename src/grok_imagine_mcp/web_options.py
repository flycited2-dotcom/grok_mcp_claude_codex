"""Что из запроса умеет интерфейс Grok. Неподдерживаемое отклоняется, значения не подгоняются."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .drivers import DriverError
from .models import ImageRequest, VideoRequest

WEB_DURATIONS = (6, 10, 15)
WEB_ASPECTS = ("2:3", "3:2", "1:1", "9:16", "16:9")
_DEFAULT_VIDEO_MODEL = "grok-imagine-video"
_DEFAULT_IMAGE_MODEL = "grok-imagine-image"
_MENTION_RE = re.compile(r"@Image (\d+)")


@dataclass(frozen=True)
class VideoOptions:
    prompt: str
    image_paths: tuple[Path, ...]     # 1 - первый кадр, 2 и больше - референсы
    resolution: str | None
    duration_s: int | None
    aspect_ratio: str | None
    generate_audio: bool | None


@dataclass(frozen=True)
class ImageOptions:
    prompt: str
    aspect_ratio: str | None


def _refuse(name: str, value, allowed) -> DriverError:
    return DriverError(f"web-драйвер: {name}={value} не поддерживается интерфейсом Grok. "
                       f"Допустимо: {', '.join(str(a) for a in allowed)}.")


def video_options(req: VideoRequest) -> tuple[VideoOptions, list[str]]:
    notes: list[str] = []
    if req.duration_s is not None and req.duration_s not in WEB_DURATIONS:
        raise _refuse("duration_s", req.duration_s, WEB_DURATIONS)
    if req.aspect_ratio is not None and req.aspect_ratio not in WEB_ASPECTS:
        raise _refuse("aspect_ratio", req.aspect_ratio, WEB_ASPECTS)
    images = tuple(req.image_paths or ((req.image_path,) if req.image_path else ()))
    for n in sorted({int(m) for m in _MENTION_RE.findall(req.prompt)}):
        if not 1 <= n <= len(images):
            raise DriverError(f"в промте ссылка @Image {n}, а приложено картинок: {len(images)}")
    if len(images) >= 2 and req.resolution == "1080p":     # проверено живьём: в режиме референсов только 480p и 720p
        raise DriverError("с несколькими картинками 1080p недоступно (Grok предлагает 480p и 720p): выберите 480p или 720p")
    aspect = req.aspect_ratio
    if len(images) == 1 and aspect is not None:            # проверено живьём: с одной картинкой кнопки формата нет
        notes.append("aspect_ratio проигнорирован: с одной картинкой Grok берёт формат кадра из неё")
        aspect = None
    if req.model != _DEFAULT_VIDEO_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return VideoOptions(req.prompt, images, req.resolution, req.duration_s, aspect, req.generate_audio), notes


def image_options(req: ImageRequest) -> tuple[ImageOptions, list[str]]:
    notes: list[str] = []
    if req.aspect_ratio is not None and req.aspect_ratio not in WEB_ASPECTS:
        raise _refuse("aspect_ratio", req.aspect_ratio, WEB_ASPECTS)
    if req.resolution is not None:
        notes.append("resolution не применяется: web-драйвер не задаёт размер картинки")
    if req.model != _DEFAULT_IMAGE_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return ImageOptions(req.prompt, req.aspect_ratio), notes
