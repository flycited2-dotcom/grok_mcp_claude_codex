"""Драйвер web: Grok Imagine через окно Chrome (подписка, без API-ключа)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .drivers import DriverError, load_task, now_iso, to_info
from .models import DriverStatus, ImageRequest, TaskInfo, VideoRequest
from .store import TaskStore
from .web_lock import FileLock
from .web_options import image_options, video_options
from .web_types import Generated, WebBackend

log = logging.getLogger("grok_imagine_mcp")

POLL_INTERVAL_S = 1.0
ORPHAN_GRACE_S = 300.0          # к таймауту очереди: столько задача может идти от блокировки до отправки


class _TaskReporter:
    """Пишет фазу и post_id в запись задачи, чтобы их видели все процессы сервера."""

    def __init__(self, store: TaskStore, task: dict):
        self._store, self._task = store, task

    def phase(self, name: str) -> None:
        self._task["phase"] = name
        self._store.save(self._task)

    def submitted(self, post_id: str) -> None:
        self._task.update(post_id=post_id, submitted=True, phase="generating")
        self._store.save(self._task)


class WebDriver:
    name = "web"

    def __init__(self, backend: WebBackend, store: TaskStore, output_dir: Path, lock_path: Path, *,
                 queue_timeout_s: float = 900.0, generation_timeout_s: float = 900.0,
                 sleep=asyncio.sleep, now: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.backend, self.store, self.output_dir, self.lock_path = backend, store, Path(output_dir), Path(lock_path)
        self.queue_timeout_s, self.generation_timeout_s = queue_timeout_s, generation_timeout_s
        self._sleep, self._now, self._utcnow = sleep, now, utcnow
        self._workers: dict[str, asyncio.Task] = {}

    # ---------- постановка ----------

    async def submit_video(self, req: VideoRequest) -> TaskInfo:
        opts, notes = video_options(req)                       # отказ до создания задачи
        return self._enqueue("video", req, opts, notes)

    async def submit_image(self, req: ImageRequest) -> TaskInfo:
        opts, notes = image_options(req)
        return self._enqueue("image", req, opts, notes)

    def _enqueue(self, kind: str, req, opts, notes: list[str]) -> TaskInfo:
        task = {"task_id": self.store.new_id(), "kind": kind, "driver": self.name, "status": "pending",
                "phase": "queued", "submitted": False, "created_at": now_iso(),
                "request": req.model_dump(mode="json", exclude_none=True), "notes": notes}
        self.store.save(task)
        self._spawn(task["task_id"], opts)
        return to_info(task)

    def _spawn(self, task_id: str, opts) -> None:
        worker = asyncio.get_running_loop().create_task(self._work(task_id, opts))
        self._workers[task_id] = worker

        def forget(done: asyncio.Task) -> None:
            if self._workers.get(task_id) is done:
                del self._workers[task_id]

        worker.add_done_callback(forget)

    # ---------- исполнитель ----------

    async def _work(self, task_id: str, opts) -> None:
        resume = opts is None
        lock = FileLock(self.lock_path)                        # своя блокировка на каждого исполнителя
        try:
            if not await lock.acquire(self.queue_timeout_s, sleep=self._sleep, now=self._now):
                if not resume:                                 # у возобновления исходный исполнитель может быть жив
                    self._fail(load_task(self.store, task_id),
                               f"очередь к окну Chrome не освободилась за {int(self.queue_timeout_s)} с; "
                               "запустите задачу заново")
                return
            try:
                task = load_task(self.store, task_id)
                if task["status"] != "pending":                # пока ждали очередь, задачу уже довели
                    return
                try:
                    generated = await asyncio.wait_for(
                        self._call_backend(task, opts, _TaskReporter(self.store, task)), self.generation_timeout_s)
                    self._finish(task, generated)
                except asyncio.TimeoutError:
                    self._fail(task, f"Grok не завершил генерацию за {int(self.generation_timeout_s)} с")
                except DriverError as e:
                    self._fail(task, str(e))
                except Exception as e:
                    self._fail(task, f"{type(e).__name__}: {e}")
            finally:
                lock.release()
        except Exception:                                      # сбой самого хранилища: исполнитель не должен молчать
            log.exception("web-драйвер: исполнитель задачи %s упал", task_id)

    async def _call_backend(self, task: dict, opts, reporter) -> Generated:
        if task.get("post_id"):
            return await self.backend.resume(task["post_id"], task["kind"], reporter)
        reporter.phase("submitting")
        return await self.backend.generate(opts, reporter)

    def _finish(self, task: dict, generated: Generated) -> None:
        dest = self.output_dir / f"{task['task_id']}{generated.ext}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        try:
            part.write_bytes(generated.data)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)                       # оборванная запись не оставляет полфайла
        task.update(status="done", output_path=str(dest), error=None)
        task.pop("phase", None)
        self.store.save(task)

    def _fail(self, task: dict, message: str) -> None:
        task.update(status="failed", error=message)
        task.pop("phase", None)
        self.store.save(task)

    # ---------- статус ----------

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        task = load_task(self.store, task_id)
        if task["status"] == "pending" and task_id not in self._workers:
            task = self._adopt(task)
        deadline = self._now() + wait_s
        while task["status"] == "pending" and self._now() < deadline:
            await self._sleep(POLL_INTERVAL_S)
            task = load_task(self.store, task_id)
        return to_info(task)

    def _adopt(self, task: dict) -> dict:
        """Задача «висит», а исполнителя в этом процессе нет: сервер перезапускали или она чужая."""
        if task.get("post_id"):
            self._spawn(task["task_id"], None)                 # дожимаем по post_id
            return task
        age = (self._utcnow() - datetime.fromisoformat(task["created_at"])).total_seconds()
        if age > self.queue_timeout_s + ORPHAN_GRACE_S:
            self._fail(task, "задача прервана до отправки в Grok (сервер был перезапущен); запустите её заново")
        return task

    async def list_tasks(self, limit: int) -> list[TaskInfo]:
        return [to_info(t) for t in self.store.list(limit)]

    async def status(self) -> DriverStatus:
        st = await self.backend.state()
        if st.logged_in is False:
            detail = f"Grok просит войти в аккаунт: откройте окно Chrome (профиль {st.profile}) и войдите вручную."
        elif not st.chrome_up:
            detail = ("Окно Chrome для Grok не запущено: оно откроется при первой генерации. "
                      "Первый вход в аккаунт выполните вручную в этом окне.")
        else:
            detail = "Генерации идут через окно Chrome по подписке Grok."
        detail += (f" Результаты: {self.output_dir}. "
                   "Автоматизация веб-интерфейса подписки может не соответствовать условиям xAI.")
        return DriverStatus(driver=self.name, ready=st.logged_in is not False, live=True, detail=detail)
