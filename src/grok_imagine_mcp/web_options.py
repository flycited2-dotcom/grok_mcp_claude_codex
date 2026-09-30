"""Что из запроса умеет интерфейс Grok. Неподдерживаемое отклоняется, значения не подгоняются."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .drivers import DriverError
from .models import ImageRequest, VideoRequest

WEB_DURATIONS = (6, 10, 15)
WEB_ASPECTS = ("2:3", "3:2", "1:1", "9:16", "16:9")
_DEFAULT_VIDEO_MODEL = "grok-imagine-video"
_DEFAULT_IMAGE_MODEL = "grok-imagine-image"


@dataclass(frozen=True)
class VideoOptions:
    prompt: str
    image_path: Path | None
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
    aspect = req.aspect_ratio
    if req.image_path is not None and aspect is not None:
        notes.append("aspect_ratio проигнорирован: с фото Grok берёт формат кадра из фото")
        aspect = None
    if req.model != _DEFAULT_VIDEO_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return VideoOptions(req.prompt, req.image_path, req.resolution, req.duration_s, aspect, req.generate_audio), notes


def image_options(req: ImageRequest) -> tuple[ImageOptions, list[str]]:
    notes: list[str] = []
    if req.aspect_ratio is not None and req.aspect_ratio not in WEB_ASPECTS:
        raise _refuse("aspect_ratio", req.aspect_ratio, WEB_ASPECTS)
    if req.resolution is not None:
        notes.append("resolution не применяется: web-драйвер не задаёт размер картинки")
    if req.model != _DEFAULT_IMAGE_MODEL:
        notes.append(f"model={req.model} не применяется: web-драйвер использует модель, выбранную в интерфейсе Grok")
    return ImageOptions(req.prompt, req.aspect_ratio), notes
