"""Batch change-password jobs for the web UI."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from change_password import ChangePasswordError, change_password
from random_profile import random_profile

from .manager import (
    _current_proxy_knobs,
    _hydrate_proxy_pool_from_settings,
    _is_proxy_network_error,
    _mask_proxy,
    _resolve_job_proxy,
)

if TYPE_CHECKING:
    from .sse_mux import SseMux


_log = logging.getLogger(__name__)
_sse_mux: "SseMux | None" = None

JobStatus = str
_DEFAULT_JOB_TIMEOUT = 240.0
_MAX_LOG_LINES = 800


def set_change_password_sse_mux(mux: "SseMux") -> None:
    global _sse_mux
    _sse_mux = mux


def _extract_plan(session_data: dict[str, Any] | None) -> str | None:
    if not isinstance(session_data, dict):
        return None
    for key in ("plan", "accountPlan"):
        value = session_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    account = session_data.get("account")
    if isinstance(account, dict):
        value = account.get("planType")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _generate_new_password() -> str:
    value = random_profile().get("password")
    if not isinstance(value, str) or not value:
        raise ChangePasswordError("random password generator returned empty password")
    return value


@dataclass
class ChangePasswordJob:
    id: str
    email: str
    current_password: str
    secret: str | None = None
    initial_session: str | None = None
    new_password: str | None = None
    check_plan_only: bool = False
    skip_password_change: bool = False
    change_2fa_only: bool = False
    status: JobStatus = "queued"
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    session_data: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    _active_proxy: str | None = field(default=None, repr=False)
    _active_proxy_line: str | None = field(default=None, repr=False)
    _proxy_knobs: dict | None = field(default=None, repr=False)

    @property
    def mode(self) -> str:
        if self.check_plan_only:
            return "check_plan"
        if self.change_2fa_only:
            return "change_2fa"
        if self.skip_password_change:
            return "session_only"
        return "change_password"

    def to_dict(self) -> dict[str, Any]:
        offer = (self.session_data or {}).get("offer_details")
        return {
            "id": self.id,
            "email": self.email,
            "mode": self.mode,
            "status": self.status,
            "error": self.error,
            "has_session": self.session_data is not None,
            "has_new_password": bool(self.new_password),
            "has_initial_session": bool(self.initial_session),
            "plan": _extract_plan(self.session_data),
            "offer_details": offer,
            "has_momo": bool(offer and offer.get("has_momo")),
            "checkout_url": (offer or {}).get("checkout_url"),
            "momo_url": (offer or {}).get("momo_url") or (offer or {}).get("checkout_url"),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": (
                (self.finished_at or time.time()) - self.started_at
                if self.started_at else None
            ),
            "log_count": len(self.log_lines),
        }

    def to_dict_full(self) -> dict[str, Any]:
        data = self.to_dict()
        data["log_lines"] = list(self.log_lines)
        data["session_data"] = self.session_data
        data["current_password"] = self.current_password
        data["new_password"] = self.new_password
        data["secret"] = (self.session_data or {}).get("new_secret") or (self.session_data or {}).get("secret") or self.secret
        return data


class ChangePasswordJobManager:
    """In-memory worker pool for API-only password changes."""

    def __init__(self, *, max_concurrent: int = 1):
        self.jobs: dict[str, ChangePasswordJob] = {}
        self.order: list[str] = []
        self._max = max_concurrent
        self._job_timeout = _DEFAULT_JOB_TIMEOUT
        self._use_proxy = True
        self._tasks: dict[str, asyncio.Task] = {}
        self._job_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._worker_started = False
        self._shutting_down = False

    @property
    def max_concurrent(self) -> int:
        return self._max

    def set_max_concurrent(self, value: int) -> None:
        if value < 1 or value > 10:
            raise ValueError("max_concurrent phai trong [1, 10]")
        self._max = value
        self._ensure_workers()

    @property
    def job_timeout(self) -> float:
        return self._job_timeout

    def set_job_timeout(self, seconds: float) -> None:
        if seconds < 30 or seconds > 900:
            raise ValueError("job_timeout phai trong [30, 900]")
        self._job_timeout = float(seconds)

    @property
    def use_proxy(self) -> bool:
        return self._use_proxy

    def set_use_proxy(self, value: bool) -> None:
        self._use_proxy = bool(value)

    def apply_settings(self, settings: dict) -> None:
        _hydrate_proxy_pool_from_settings(settings)
        if "chpwd.max_concurrent" in settings:
            value = int(settings["chpwd.max_concurrent"])
            if 1 <= value <= 10:
                self._max = value
        if "chpwd.job_timeout" in settings:
            value = float(settings["chpwd.job_timeout"])
            if 30 <= value <= 900:
                self._job_timeout = value
        if "chpwd.use_proxy" in settings:
            self._use_proxy = bool(settings["chpwd.use_proxy"])
        self._ensure_workers()

    def _broadcast(self, event: dict[str, Any]) -> None:
        if _sse_mux is not None:
            _sse_mux.publish("change_password", event)

    def _broadcast_job(self, job: ChangePasswordJob) -> None:
        self._broadcast({"type": "job", "job": job.to_dict()})

    def _job_log(self, job: ChangePasswordJob, msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        job.log_lines.append(line)
        if len(job.log_lines) > _MAX_LOG_LINES:
            job.log_lines = job.log_lines[-_MAX_LOG_LINES:]
        self._broadcast({"type": "log", "job_id": job.id, "line": line})

    def _ensure_workers(self) -> None:
        if not self._worker_started:
            self._worker_started = True
        self._workers = [task for task in self._workers if not task.done()]
        while len(self._workers) < self._max:
            self._workers.append(asyncio.create_task(self._worker_loop()))
        while len(self._workers) > self._max:
            self._workers.pop().cancel()

    async def _worker_loop(self) -> None:
        try:
            while True:
                job_id = await self._job_queue.get()
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue
                task = asyncio.create_task(self._run_job(job))
                self._tasks[job_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelled():
                        raise
                    if task.cancelled():
                        continue
                    raise
                finally:
                    self._tasks.pop(job_id, None)
        except asyncio.CancelledError:
            pass

    def add_jobs(
        self,
        lines: list[str],
        *,
        new_password: str | None = None,
        check_plan_only: bool = False,
        skip_password_change: bool = False,
        change_2fa_only: bool = False,
    ) -> list[ChangePasswordJob]:
        existing = {
            job.email.lower()
            for job in self.jobs.values()
            if job.status in ("queued", "running")
        }
        out: list[ChangePasswordJob] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                line = line.replace("\t", "|")
            parts = line.split("|", 3)
            if len(parts) < 2:
                job = ChangePasswordJob(
                    id=uuid.uuid4().hex[:12],
                    email="<invalid>",
                    current_password="",
                    status="error",
                    error=f"format sai, can email|current_password|2fa_secret|session_json: {line[:80]}",
                    finished_at=time.time(),
                )
                self.jobs[job.id] = job
                self.order.append(job.id)
                self._broadcast_job(job)
                out.append(job)
                continue

            email = parts[0].strip()
            current_password = parts[1].strip()
            secret = (parts[2].strip() or None) if len(parts) >= 3 else None
            
            initial_session = None
            if len(parts) >= 4:
                val = parts[3].strip()
                if val.startswith("{") or val.startswith("ey") or len(val) > 100:
                    initial_session = val
            if not email or not current_password:
                job = ChangePasswordJob(
                    id=uuid.uuid4().hex[:12],
                    email=email or "<invalid>",
                    current_password=current_password,
                    secret=secret,
                    initial_session=initial_session,
                    status="error",
                    error="email/current_password are required",
                    finished_at=time.time(),
                )
                self.jobs[job.id] = job
                self.order.append(job.id)
                self._broadcast_job(job)
                out.append(job)
                continue
            if email.lower() in existing:
                continue
            existing.add(email.lower())

            resolved_new_password = str(new_password or "").strip() or None
            if not resolved_new_password and not check_plan_only and not skip_password_change and not change_2fa_only:
                resolved_new_password = _generate_new_password()

            job = ChangePasswordJob(
                id=uuid.uuid4().hex[:12],
                email=email,
                current_password=current_password,
                secret=secret,
                initial_session=initial_session,
                new_password=resolved_new_password,
                check_plan_only=check_plan_only,
                skip_password_change=skip_password_change,
                change_2fa_only=change_2fa_only,
            )
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._broadcast_job(job)
            out.append(job)

        self._ensure_workers()
        for job in out:
            if job.status == "queued":
                self._job_queue.put_nowait(job.id)
        return out

    async def stop_all(self) -> int:
        while not self._job_queue.empty():
            try:
                self._job_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job.status in ("queued", "running"):
                task = self._tasks.get(job_id)
                if task and not task.done():
                    task.cancel()
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
                count += 1
        return count

    def shutdown(self) -> None:
        self._shutting_down = True
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        for worker in self._workers:
            if not worker.done():
                worker.cancel()
        self._workers.clear()

    def clear_finished(self) -> int:
        removed = 0
        for job_id in list(self.order):
            job = self.jobs.get(job_id)
            if job and job.status in ("success", "error", "cancelled"):
                self.jobs.pop(job_id, None)
                self.order.remove(job_id)
                self._tasks.pop(job_id, None)
                removed += 1
        if removed:
            self._broadcast({"type": "clear_finished", "removed": removed})
        return removed

    def remove_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        self.jobs.pop(job_id, None)
        if job_id in self.order:
            self.order.remove(job_id)
        self._tasks.pop(job_id, None)
        self._broadcast({"type": "remove", "job_id": job_id})
        return True

    async def retry_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status == "running":
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        job.status = "queued"
        job.error = None
        job.session_data = None
        job.started_at = None
        job.finished_at = None
        self._job_log(job, "-- retry --")
        self._broadcast_job(job)
        self._ensure_workers()
        self._job_queue.put_nowait(job.id)
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.jobs[job_id].to_dict() for job_id in self.order if job_id in self.jobs]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return job.to_dict_full() if job else None

    def get_log(self, job_id: str) -> list[str]:
        job = self.jobs.get(job_id)
        return list(job.log_lines) if job else []

    async def _begin_job_proxy(self, job: ChangePasswordJob) -> str | None:
        from .twoproxy_pool import get_twoproxy_pool
        twoproxy = get_twoproxy_pool()
        if not self._use_proxy and not twoproxy.is_active():
            return None
        knobs = getattr(job, "_proxy_knobs", None) or _current_proxy_knobs()
        job._proxy_knobs = knobs
        url, line = await _resolve_job_proxy(lambda msg: self._job_log(job, msg), knobs=knobs)
        job._active_proxy = url
        job._active_proxy_line = line
        if line:
            self._job_log(job, f"[proxy] using {_mask_proxy(line)}")
        return url

    def _note_proxy_failure(self, job: ChangePasswordJob, exc_or_msg: object) -> None:
        line = job._active_proxy_line
        if line and _is_proxy_network_error(exc_or_msg):
            from .proxy_pool import get_proxy_pool

            if get_proxy_pool().mark_dead(line):
                self._job_log(job, f"[proxy] {_mask_proxy(line)} loi network - loai khoi pool")

    async def _run_job(self, job: ChangePasswordJob) -> None:
        try:
            if job.id not in self.jobs:
                return
            job.status = "running"
            job.started_at = time.time()
            self._broadcast_job(job)

            proxy = await self._begin_job_proxy(job)
            if job.new_password and not job.check_plan_only and not job.skip_password_change and not job.change_2fa_only:
                self._job_log(job, "[chpwd] new password prepared")

            result = await asyncio.wait_for(
                change_password(
                    email=job.email,
                    current_password=job.current_password,
                    new_password=job.new_password or "",
                    check_plan_only=job.check_plan_only,
                    skip_password_change=job.skip_password_change,
                    change_2fa_only=job.change_2fa_only,
                    secret=job.secret,
                    initial_session=job.initial_session,
                    proxy=proxy,
                    log=lambda msg: self._job_log(job, msg),
                ),
                timeout=self._job_timeout,
            )
            job.session_data = result
            if result.get("new_secret"):
                job.secret = result.get("new_secret")
            job.status = "success"
            job.finished_at = time.time()
            self._broadcast_job(job)
        except asyncio.TimeoutError:
            job.status = "error"
            job.error = f"timeout {self._job_timeout:.0f}s exceeded"
            job.finished_at = time.time()
            self._job_log(job, f"[fatal] {job.error}")
            self._broadcast_job(job)
        except asyncio.CancelledError:
            if not self._shutting_down and job.id in self.jobs:
                job.status = "cancelled"
                job.finished_at = time.time()
                self._broadcast_job(job)
            raise
        except ChangePasswordError as exc:
            job.status = "error"
            job.error = str(exc)
            if getattr(exc, "data", None):
                job.session_data = exc.data
            job.finished_at = time.time()
            self._note_proxy_failure(job, exc)
            self._job_log(job, f"[error] {exc}")
            self._broadcast_job(job)
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
            self._note_proxy_failure(job, exc)
            self._job_log(job, f"[fatal] {job.error}")
            self._broadcast_job(job)
        finally:
            self._tasks.pop(job.id, None)
            active_line = getattr(job, "_active_proxy_line", None)
            if active_line:
                from .twoproxy_pool import get_twoproxy_pool
                get_twoproxy_pool().release(active_line)


_change_password_manager: ChangePasswordJobManager | None = None


def get_change_password_manager() -> ChangePasswordJobManager:
    global _change_password_manager
    if _change_password_manager is None:
        _change_password_manager = ChangePasswordJobManager(max_concurrent=1)
    return _change_password_manager
