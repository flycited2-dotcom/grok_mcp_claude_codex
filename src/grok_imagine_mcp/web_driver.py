"""Драйвер web: Grok Imagine через окно Chrome (подписка, без API-ключа)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .cdp import CdpConnectionLost
from .drivers import DriverError, load_task, now_iso, to_info
from .models import DriverStatus, ImageRequest, TaskInfo, VideoRequest
from .store import TaskStore
from .web_lock import FileLock
from .web_options import image_options, video_options
from .web_types import Generated, WebBackend

log = logging.getLogger("grok_imagine_mcp")

POLL_INTERVAL_S = 1.0
# Пока исполнитель жив (в любом процессе), он раз в HEARTBEAT_S пишет метку в запись задачи. Задача без метки дольше
# HEARTBEAT_STALE_S никому не принадлежит: её можно дожать (есть post_id) или признать оборванной.
HEARTBEAT_S = 15.0
HEARTBEAT_STALE_S = 90.0
MAX_RESUME_ATTEMPTS = 3         # сколько раз подряд можно потерять связь с окном и всё равно дожимать по post_id
_NEVER = "1970-01-01T00:00:00+00:00"


class _TaskReporter:
    """Пишет фазу и post_id в запись задачи, чтобы их видели все процессы сервера."""

    def __init__(self, store: TaskStore, task: dict):
        self._store, self._task = store, task

    def phase(self, name: str) -> None:
        self._task["phase"] = name
        self._store.save(self._task)

    def submitted(self, post_id: str | None) -> None:
        self._task.update(submitted=True, phase="generating")
        if post_id:                                            # у картинки страницы поста нет: возобновлять нечем
            self._task["post_id"] = post_id
        self._store.save(self._task)


class WebDriver:
    name = "web"

    def __init__(self, backend: WebBackend, store: TaskStore, output_dir: Path, lock_path: Path, *,
                 queue_timeout_s: float = 3600.0, generation_timeout_s: float = 900.0,
                 heartbeat_s: float = HEARTBEAT_S, stale_after_s: float = HEARTBEAT_STALE_S,
                 sleep=asyncio.sleep, now: Callable[[], float] = time.monotonic,
                 utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.backend, self.store, self.output_dir, self.lock_path = backend, store, Path(output_dir), Path(lock_path)
        self.queue_timeout_s, self.generation_timeout_s = queue_timeout_s, generation_timeout_s
        self.heartbeat_s, self.stale_after_s = heartbeat_s, stale_after_s
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
                "phase": "queued", "submitted": False, "created_at": now_iso(), "heartbeat_at": self._stamp(),
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

    def _stamp(self) -> str:
        return self._utcnow().isoformat(timespec="seconds")

    async def _heartbeat(self, task: dict) -> None:
        """Пока исполнитель жив, запись задачи «дышит»: по этой метке другие процессы понимают, что задача не брошена."""
        while True:
            if task.get("status") == "pending":
                task["heartbeat_at"] = self._stamp()
                self.store.save(task)
            await asyncio.sleep(self.heartbeat_s)              # реальное время: не зависит от подставных часов очереди

    async def _work(self, task_id: str, opts) -> None:
        resume = opts is None
        lock = FileLock(self.lock_path)                        # своя блокировка на каждого исполнителя
        beat = None
        try:
            task = load_task(self.store, task_id)              # один словарь на всё: его обновляют и метка, и reporter
            beat = asyncio.get_running_loop().create_task(self._heartbeat(task))
            if not await lock.acquire(self.queue_timeout_s, sleep=self._sleep, now=self._now):
                if not resume:                                 # у возобновления исходный исполнитель может быть жив
                    self._fail(task, f"очередь к окну Chrome не освободилась за {int(self.queue_timeout_s)} с; "
                                     "запустите задачу заново")
                return
            try:
                fresh = load_task(self.store, task_id)
                task.clear()
                task.update(fresh)
                if task["status"] != "pending":                # пока ждали очередь, задачу уже довели
                    return
                try:
                    generated = await asyncio.wait_for(
                        self._call_backend(task, opts, _TaskReporter(self.store, task)), self.generation_timeout_s)
                    self._finish(task, generated)
                except asyncio.TimeoutError:
                    self._fail(task, f"Grok не завершил генерацию за {int(self.generation_timeout_s)} с")
                except CdpConnectionLost as e:
                    self._lost_connection(task, e)
                except DriverError as e:
                    self._fail(task, str(e))
                except Exception as e:
                    self._fail(task, f"{type(e).__name__}: {e}")
            finally:
                lock.release()
        except Exception:                                      # сбой самого хранилища: исполнитель не должен молчать
            log.exception("web-драйвер: исполнитель задачи %s упал", task_id)
        finally:
            if beat is not None:
                beat.cancel()
                await asyncio.gather(beat, return_exceptions=True)

    def _lost_connection(self, task: dict, error: Exception) -> None:
        """Окно закрыли или Chrome упал. Если запрос уже ушёл (есть post_id), лимит потрачен: задача остаётся
        pending с «протухшей» меткой, и ближайший grok_task_status дожмёт её по post_id (с новым окном)."""
        attempts = task.get("resume_attempts", 0) + 1
        if task.get("post_id") and attempts <= MAX_RESUME_ATTEMPTS:
            task.update(resume_attempts=attempts, heartbeat_at=_NEVER, phase="generating")
            self.store.save(task)
            log.warning("web-драйвер: связь с окном потеряна, задача %s будет дожата (попытка %d)", task["task_id"],
                        attempts)
        else:
            self._fail(task, f"связь с окном Chrome потеряна: {error}")

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
        if generated.note:
            task["notes"] = [*task.get("notes", []), generated.note]
        self.store.save(task)

    def _fail(self, task: dict, message: str) -> None:
        task.update(status="failed", error=message)
        task.pop("phase", None)
        self.store.save(task)

    # ---------- статус ----------

    async def task_status(self, task_id: str, wait_s: int) -> TaskInfo:
        task = load_task(self.store, task_id)
        deadline = self._now() + wait_s
        while True:
            if task["status"] == "pending" and task_id not in self._workers:
                task = self._adopt(task)                       # исполнитель мог умереть и посреди ожидания
            if task["status"] != "pending" or self._now() >= deadline:
                return to_info(task)
            await self._sleep(POLL_INTERVAL_S)
            task = load_task(self.store, task_id)

    def _adopt(self, task: dict) -> dict:
        """В этом процессе исполнителя нет. Если метка свежая, задачу ведёт другой процесс: не трогаем. Если
        протухла, её никто не ведёт: дожимаем по post_id или признаём оборванной."""
        beat = datetime.fromisoformat(task.get("heartbeat_at") or task["created_at"])
        if (self._utcnow() - beat).total_seconds() <= self.stale_after_s:
            return task
        if task.get("post_id"):
            self._spawn(task["task_id"], None)
            return task
        self._fail(task, "задача прервана: сервер был перезапущен до получения результата; запустите её заново")
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
