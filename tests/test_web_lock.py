import asyncio

import pytest

from grok_imagine_mcp.web_lock import FileLock


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def test_second_lock_on_same_file_is_refused_until_release(tmp_path):
    path = tmp_path / "deep" / "web.lock"
    a, b = FileLock(path), FileLock(path)
    assert a.try_acquire() is True
    assert b.try_acquire() is False
    a.release()
    assert b.try_acquire() is True
    b.release()


def test_same_object_cannot_acquire_twice(tmp_path):
    a = FileLock(tmp_path / "web.lock")
    assert a.try_acquire()
    with pytest.raises(RuntimeError):
        a.try_acquire()
    a.release()


def test_release_is_safe_without_acquire_and_twice(tmp_path):
    a = FileLock(tmp_path / "web.lock")
    a.release()
    assert a.try_acquire()
    a.release()
    a.release()


def test_acquire_times_out_while_the_lock_is_held(tmp_path):
    a, b, clock = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock"), Clock()
    assert a.try_acquire()
    assert asyncio.run(b.acquire(2.0, sleep=clock.sleep, now=clock.now)) is False
    a.release()


def test_acquire_succeeds_as_soon_as_the_holder_releases(tmp_path):
    a, b, clock = FileLock(tmp_path / "x.lock"), FileLock(tmp_path / "x.lock"), Clock()
    assert a.try_acquire()

    async def sleep_and_release(s):
        clock.t += s
        a.release()

    assert asyncio.run(b.acquire(5.0, sleep=sleep_and_release, now=clock.now)) is True
    b.release()
