import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from grok_imagine_mcp.drivers import DriverError, now_iso
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.store import TaskStore
from grok_imagine_mcp.web_driver import WebDriver
from grok_imagine_mcp.web_lock import FileLock
from grok_imagine_mcp.web_types import BackendState, Generated

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
POST = "11111111-2222-3333-4444-555555555555"


class FakeBackend:
    def __init__(self, state=None, result=None):
        self._state = state or BackendState(chrome_up=True, logged_in=True, profile="P")
        self.result = result or Generated(b"VIDEO", ".mp4")
        self.calls = []
        self.exc = None
        self.gate = None                 # asyncio.Event: пока не выставлен, генерация «идёт»

    async def state(self):
        return self._state

    async def generate(self, opts, reporter):
        self.calls.append(("generate", opts))
        reporter.submitted(POST)
        if self.gate is not None:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        reporter.phase("downloading")
        return self.result

    async def resume(self, post_id, kind, reporter):
        self.calls.append(("resume", post_id, kind))
        if self.gate is not None:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        return self.result


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0.01)        # дать исполнителю поработать


def make(tmp_path, backend=None, **kw):
    backend = backend or FakeBackend()
    clock = Clock()
    drv = WebDriver(backend, TaskStore(tmp_path / "tasks"), tmp_path / "out", tmp_path / "web.lock",
                    sleep=clock.sleep, now=clock.now, **kw)
    return drv, backend


def run(coro):
    return asyncio.run(coro)


def pending_task(store, **extra):
    task = {"task_id": store.new_id(), "kind": "video", "driver": "web", "submitted": True, "status": "pending",
            "phase": "generating", "created_at": now_iso(), "request": {"prompt": "x"}, "notes": [], **extra}
    store.save(task)
    return task


def test_video_is_queued_then_done_with_a_file(tmp_path):
    async def main():
        drv, backend = make(tmp_path)
        info = await drv.submit_video(VideoRequest(prompt="лодка", duration_s=6))
        done = await drv.task_status(info.task_id, 30)
        return info, done, backend
    info, done, backend = run(main())
    assert info.status == "pending" and info.submitted is False and "очереди" in info.message
    assert done.status == "done" and done.submitted is True
    out = Path(done.output_path)
    assert out.read_bytes() == b"VIDEO" and out.suffix == ".mp4" and out.parent == tmp_path / "out"
    saved = json.loads((tmp_path / "tasks" / f"{info.task_id}.json").read_text(encoding="utf-8"))
    assert saved["post_id"] == POST and "phase" not in saved and saved["driver"] == "web"
    kind, opts = backend.calls[0]
    assert kind == "generate" and opts.prompt == "лодка" and opts.duration_s == 6


def test_image_task_gets_the_extension_of_the_generated_file(tmp_path):
    async def main():
        drv, backend = make(tmp_path, FakeBackend(result=Generated(PNG, ".png")))
        info = await drv.submit_image(ImageRequest(prompt="кот", aspect_ratio="1:1"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.kind == "image" and done.status == "done" and done.output_path.endswith(".png")


def test_unsupported_value_is_refused_and_no_task_is_created(tmp_path):
    async def main():
        drv, _ = make(tmp_path)
        with pytest.raises(DriverError, match="6, 10, 15"):
            await drv.submit_video(VideoRequest(prompt="x", duration_s=8))
    run(main())
    assert not list((tmp_path / "tasks").glob("*.json"))


def test_notes_reach_the_message(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)

    async def main():
        drv, _ = make(tmp_path)
        return await drv.submit_video(VideoRequest(prompt="оживи", image_path=str(img), aspect_ratio="16:9"))
    assert "aspect_ratio проигнорирован" in run(main()).message


def test_driver_error_from_backend_marks_task_failed_with_its_text(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.exc = DriverError("Grok просит войти в аккаунт")
        drv, _ = make(tmp_path, backend)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.status == "failed" and done.error == "Grok просит войти в аккаунт"
    assert done.output_path is None and "Генерация не удалась" in done.message


def test_unexpected_exception_is_reported_as_failed_not_lost(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.exc = RuntimeError("boom")
        drv, _ = make(tmp_path, backend)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    assert run(main()).error == "RuntimeError: boom"


def test_generation_timeout_fails_the_task_and_frees_the_lock(tmp_path):
    async def main():
        backend = FakeBackend()
        backend.gate = asyncio.Event()                     # генерация «зависла»
        drv, _ = make(tmp_path, backend, generation_timeout_s=0.05)
        info = await drv.submit_video(VideoRequest(prompt="x"))
        return await drv.task_status(info.task_id, 30)
    done = run(main())
    assert done.status == "failed" and "не завершил генерацию" in done.error
    lock = FileLock(tmp_path / "web.lock")
    assert lock.try_acquire()
    lock.release()


def test_second_driver_waits_for_the_first_through_the_lock(tmp_path):
    async def main():
        d1, b1 = make(tmp_path, queue_timeout_s=1e9)
        d2, b2 = make(tmp_path, queue_timeout_s=1e9)
        b1.gate = asyncio.Event()
        t1 = await d1.submit_video(VideoRequest(prompt="первая"))
        await asyncio.sleep(0.1)
        t2 = await d2.submit_video(VideoRequest(prompt="вторая"))
        await asyncio.sleep(0.2)
        while_first_runs = (len(b1.calls), len(b2.calls))
        b1.gate.set()
        r1 = await d1.task_status(t1.task_id, 30)
        r2 = await d2.task_status(t2.task_id, 30)
        return while_first_runs, r1, r2
    while_first_runs, r1, r2 = run(main())
    assert while_first_runs == (1, 0)
    assert r1.status == "done" and r2.status == "done"


def test_pending_task_with_post_id_is_resumed_after_a_restart(tmp_path):
    async def main():
        drv, backend = make(tmp_path)
        task = pending_task(drv.store, post_id=POST)
        done = await drv.task_status(task["task_id"], 30)
        return done, backend
    done, backend = run(main())
    assert done.status == "done" and Path(done.output_path).read_bytes() == b"VIDEO"
    assert backend.calls == [("resume", POST, "video")]


def test_task_without_post_id_is_failed_only_after_the_queue_limit_plus_grace(tmp_path):
    created = "2026-10-01T10:00:00+00:00"

    async def main(minutes_later):
        now = datetime(2026, 10, 1, 10, minutes_later, tzinfo=timezone.utc)
        drv, backend = make(tmp_path, utcnow=lambda: now)
        task = pending_task(drv.store, submitted=False, phase="queued", created_at=created)
        return await drv.task_status(task["task_id"], 0), backend
    fresh, b1 = run(main(1))
    assert fresh.status == "pending" and b1.calls == []
    stale, b2 = run(main(50))                               # 900 с очереди + 300 с запаса = 20 минут
    assert stale.status == "failed" and "прервана до отправки" in stale.error and b2.calls == []


def test_resume_does_not_fail_the_task_when_the_lock_stays_busy(tmp_path):
    async def main():
        drv, backend = make(tmp_path, queue_timeout_s=2)
        holder = FileLock(tmp_path / "web.lock")
        assert holder.try_acquire()
        task = pending_task(drv.store, post_id=POST)
        try:
            return await drv.task_status(task["task_id"], 5), backend
        finally:
            holder.release()
    info, backend = run(main())
    assert info.status == "pending" and backend.calls == []


def test_new_task_fails_with_a_clear_message_when_the_queue_never_frees(tmp_path):
    async def main():
        drv, backend = make(tmp_path, queue_timeout_s=2)
        holder = FileLock(tmp_path / "web.lock")
        assert holder.try_acquire()
        try:
            info = await drv.submit_video(VideoRequest(prompt="x"))
            return await drv.task_status(info.task_id, 10), backend
        finally:
            holder.release()
    done, backend = run(main())
    assert done.status == "failed" and "очередь" in done.error and backend.calls == []


@pytest.mark.parametrize("bad", ["ffffffffffff", "../../etc/passwd", ""])
def test_unknown_or_malformed_task_id_is_a_driver_error(tmp_path, bad):
    drv, _ = make(tmp_path)
    with pytest.raises(DriverError, match="не найдена"):
        run(drv.task_status(bad, 0))


def test_list_tasks_newest_first(tmp_path):
    drv, _ = make(tmp_path)
    old = pending_task(drv.store, created_at="2026-10-01T09:00:00+00:00", post_id=POST)
    new = pending_task(drv.store, created_at="2026-10-01T10:00:00+00:00", post_id=POST)
    assert [t.task_id for t in run(drv.list_tasks(10))] == [new["task_id"], old["task_id"]]


@pytest.mark.parametrize("phase, word", [("queued", "очереди"), ("submitting", "отправляется"),
                                         ("generating", "генерирует"), ("downloading", "скачивается")])
def test_phase_is_visible_in_the_message(phase, word):
    from grok_imagine_mcp.drivers import to_info
    msg = to_info({"task_id": "a" * 12, "kind": "video", "driver": "web", "status": "pending", "phase": phase,
                   "submitted": True, "created_at": now_iso()}).message
    assert word in msg and "grok_task_status" in msg


def test_status_reports_login_chrome_and_ready_states(tmp_path):
    async def one(state):
        drv, _ = make(tmp_path, FakeBackend(state=state))
        return await drv.status()
    logged_out = run(one(BackendState(chrome_up=True, logged_in=False, profile="PROF")))
    assert logged_out.ready is False and "войдите" in logged_out.detail and "PROF" in logged_out.detail
    no_window = run(one(BackendState(chrome_up=False, logged_in=None, profile="PROF")))
    assert no_window.ready is True and "не запущено" in no_window.detail
    fine = run(one(BackendState(chrome_up=True, logged_in=True, profile="PROF")))
    assert fine.ready is True and fine.live is True and fine.driver == "web"
    assert "условиям xAI" in fine.detail
