import struct

import pytest

from grok_imagine_mcp.media_check import MediaError, check_mp4, detect_image_ext


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


GOOD = box(b"ftyp", b"isom0000") + box(b"moov", b"m" * 20) + box(b"mdat", b"d" * 100)


def test_whole_mp4_passes():
    check_mp4(GOOD)


def test_truncated_mp4_is_rejected():
    with pytest.raises(MediaError, match="обрезан"):
        check_mp4(GOOD[:-3])


def test_trailing_garbage_shorter_than_a_header_is_rejected():
    with pytest.raises(MediaError, match="обрезан"):
        check_mp4(GOOD + b"\x00\x00")


def test_missing_moov_is_rejected():
    with pytest.raises(MediaError, match="moov"):
        check_mp4(box(b"ftyp", b"isom0000") + box(b"mdat", b"d" * 10))


def test_empty_and_html_error_page_are_rejected():
    with pytest.raises(MediaError):
        check_mp4(b"")
    with pytest.raises(MediaError):
        check_mp4(b"<html><body>403 Forbidden</body></html>")


def test_last_box_with_zero_size_runs_to_the_end():
    data = box(b"ftyp", b"isom0000") + box(b"moov", b"m" * 4) + struct.pack(">I4s", 0, b"mdat") + b"d" * 50
    check_mp4(data)


def test_image_types_are_detected_by_content():
    assert detect_image_ext(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == ".png"
    assert detect_image_ext(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == ".jpg"
    assert detect_image_ext(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == ".webp"


def test_non_image_is_rejected():
    with pytest.raises(MediaError, match="не похож на картинку"):
        detect_image_ext(b"<html>")
