"""Job lifecycle: creation, validation, dispatch, cooperative cancellation,
progress reporting and exactly-once finalization (PRD §14–§16).

JobManager is the only component that starts jobs (RULES §5). Job state lives
in the DB; only the task handle, the per-job cancel event and the per-account
exclusivity slot live in memory. Final transitions go through the guarded
``transition_job`` CAS, so finalization is write-once even when cancellation
races completion.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.config import Config
from app.core.account_service import ServiceError
from app.core.events import EventBus, JobFinishedEvent, JobProgressEvent
from app.core.models import (
    FINAL_JOB_STATUSES,
    AccountStatus,
    Check,
    CheckStatus,
    Job,
    JobStatus,
    ResolvedEntity,
)
from app.db import repositories as repo
from app.db.database import Database
from app.security.crypto import CryptoError, SessionCrypto
from app.tg.client_pool import ClientPool
from app.tg.errors import ErrorKind
from app.tg.transfer import ProgressSnapshot, TransferParams

logger = logging.getLogger(__name__)

__all__ = ["JobManager"]

if TYPE_CHECKING:
    from app.tg.transfer import TransferEngine

PreflightRunner = Callable[..., Awaitable[list[Check]]]

#: Minimum spacing between two engine-driven progress DB writes; a phase
#: change always writes immediately (brief: ">= 2 s apart or on phase change").
_PROGRESS_DB_MIN_INTERVAL = 2.0

#: Shared Arabic messages (bot/texts.py renders them verbatim).
MSG_ACCOUNT_NOT_FOUND = "الحساب غير موجود."
MSG_SESSION_INVALID = "جلسة الحساب غير صالحة، أعد تسجيل الدخول إلى الحساب."
MSG_ACCOUNT_LIMITED = "الحساب محدود من تيليجرام حالياً، أعد المحاولة لاحقاً."
MSG_ACCOUNT_LIMITED_UNTIL = "الحساب محدود من تيليجرام حالياً، أعد المحاولة بعد {time}."
MSG_ACCOUNT_BUSY = (
    "هناك عملية نقل جارية بالفعل على هذا الحساب، انتظر انتهاءها أو ألغِها أولاً."
)
MSG_USER_AT_CAPACITY = "وصلت إلى الحد الأقصى لعدد العمليات الجارية في الوقت نفسه."
MSG_NOT_GROUPISH = "المصدر والهدف يجب أن يكونا مجموعتين مدعومتين للنقل."
MSG_UNEXPECTED = "حدث خطأ غير متوقع أثناء التنفيذ."

_LTS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_limited_until(value: str | None) -> datetime | None:
    """Parse a stored limited_until timestamp; None when absent/unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _LTS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_remaining(remaining: timedelta) -> str:
    total_minutes = max(1, int(remaining.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} ساعة و {minutes} دقيقة"
    return f"{minutes} دقيقة"


class JobManager:
    def __init__(
        self,
        db: Database,
        pool: ClientPool,
        crypto: SessionCrypto,
        bus: EventBus,
        config: Config,
        engine: "TransferEngine",
        *,
        preflight_runner: PreflightRunner | None = None,
    ) -> None:
        self._db = db
        self._pool = pool
        self._crypto = crypto
        self._bus = bus
        self._config = config
        self._engine = engine
        # Injectable so tests stay offline; resolves lazily to the real
        # app.tg.preflight.run_preflight in production.
        self._preflight = preflight_runner or self._default_preflight
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancel_events: dict[int, asyncio.Event] = {}
        self._active_accounts: set[int] = set()
        self._account_lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(config.max_concurrent_jobs)

    # ------------------------------------------------------------------ public

    async def recover(self) -> int:
        """Boot recovery: jobs left mid-flight by a previous process become
        ``interrupted``. Must run before any new job starts (RULES §5)."""
        return await repo.recover_interrupted_jobs(self._db)

    async def create_job(
        self,
        owner_id: int,
        account_id: int,
        source: ResolvedEntity,
        dest: ResolvedEntity,
    ) -> Job:
        record = await repo.get_account(self._db, owner_id, account_id)
        if record is None:
            raise ServiceError(MSG_ACCOUNT_NOT_FOUND)
        if record.status is AccountStatus.UNAUTHORIZED:
            raise ServiceError(MSG_SESSION_INVALID)
        if record.status is AccountStatus.LIMITED:
            until = _parse_limited_until(record.limited_until)
            if until is None:
                raise ServiceError(MSG_ACCOUNT_LIMITED)
            remaining = until - datetime.now(timezone.utc)
            if remaining.total_seconds() > 0:
                raise ServiceError(
                    MSG_ACCOUNT_LIMITED_UNTIL.format(
                        time=_format_remaining(remaining)
                    )
                )
        async with self._account_lock:
            account_busy = account_id in self._active_accounts
        if (
            account_busy
            or await repo.count_active_jobs_for_account(self._db, account_id) > 0
        ):
            raise ServiceError(MSG_ACCOUNT_BUSY)
        if (
            await repo.count_active_jobs_for_user(self._db, owner_id)
            >= self._config.max_running_jobs_per_user
        ):
            raise ServiceError(MSG_USER_AT_CAPACITY)
        if not source.is_groupish or not dest.is_groupish:
            raise ServiceError(MSG_NOT_GROUPISH)

        job_id = await repo.insert_job(
            self._db,
            owner_id=owner_id,
            account_id=account_id,
            source_ref=source.raw_ref,
            dest_ref=dest.raw_ref,
            source_title=source.title,
            dest_title=dest.title,
        )
        started = await repo.transition_job(
            self._db, job_id, (JobStatus.CREATED,), JobStatus.VALIDATING
        ) and await repo.transition_job(
            self._db, job_id, (JobStatus.VALIDATING,), JobStatus.QUEUED
        )
        if not started:  # pragma: no cover - row was just created
            await repo.transition_job(
                self._db,
                job_id,
                (JobStatus.CREATED, JobStatus.VALIDATING, JobStatus.QUEUED),
                JobStatus.FAILED,
                error=MSG_UNEXPECTED,
            )
            raise ServiceError(MSG_UNEXPECTED)

        self._cancel_events[job_id] = asyncio.Event()
        task = asyncio.create_task(
            self._run_job(job_id, owner_id, account_id, source, dest),
            name=f"job-{job_id}",
        )
        self._tasks[job_id] = task
        job = await repo.get_job(self._db, owner_id, job_id)
        assert job is not None  # inserted above
        return job

    async def cancel_job(self, owner_id: int, job_id: int) -> bool:
        """Cancel an owned job. queued/created jobs transition directly to
        ``cancelled``; running jobs get the cooperative cancel flag + event and
        the engine stops between invites/waits (``task.cancel()`` is only the
        shutdown backstop, RULES §5). Returns False for unknown, foreign or
        already-final jobs."""
        job = await repo.get_job(self._db, owner_id, job_id)
        if job is None or job.status in FINAL_JOB_STATUSES:
            return False
        if job.status in (JobStatus.CREATED, JobStatus.VALIDATING, JobStatus.QUEUED):
            ok = await repo.transition_job(
                self._db,
                job_id,
                (JobStatus.CREATED, JobStatus.VALIDATING, JobStatus.QUEUED),
                JobStatus.CANCELLED,
            )
            if ok:
                await self._bus.publish(
                    JobFinishedEvent(
                        job_id=job_id,
                        status=JobStatus.CANCELLED,
                        invited=job.invited,
                        skipped=job.skipped,
                        failed=job.failed,
                        error=None,
                    )
                )
                return True
            # Lost a race (the job just started running or was finalized);
            # fall through and re-read below.
            job = await repo.get_job(self._db, owner_id, job_id)
            if job is None or job.status in FINAL_JOB_STATUSES:
                return False
        await repo.set_cancel_requested(self._db, job_id)
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        return True

    async def get_job(self, owner_id: int, job_id: int) -> Job | None:
        return await repo.get_job(self._db, owner_id, job_id)

    async def list_jobs(self, owner_id: int, limit: int = 10) -> list[Job]:
        return await repo.list_jobs(self._db, owner_id, limit=limit)

    async def shutdown(self) -> None:
        """Cancel every live job task (backstop path) and await them; the
        client pool is drained by the composition root, not here."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ runner

    async def _run_job(
        self,
        job_id: int,
        owner_id: int,
        account_id: int,
        source: ResolvedEntity,
        dest: ResolvedEntity,
    ) -> None:
        loop = asyncio.get_running_loop()
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
        slot_held = False
        finalized = False
        try:
            async with self._capacity:
                if not await repo.transition_job(
                    self._db, job_id, (JobStatus.QUEUED,), JobStatus.RUNNING,
                    started=True,
                ):
                    return  # cancelled while queued; cancel_job published the event
                async with self._account_lock:
                    if account_id in self._active_accounts:  # pragma: no cover
                        finalized = await self._finalize(
                            job_id, owner_id, account_id,
                            JobStatus.FAILED, error=MSG_ACCOUNT_BUSY,
                        )
                        return
                    self._active_accounts.add(account_id)
                    slot_held = True

                record = await repo.get_account(self._db, owner_id, account_id)
                if record is None:  # pragma: no cover - guarded by create_job
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.FAILED, error=MSG_ACCOUNT_NOT_FOUND,
                    )
                    return
                try:
                    session = self._crypto.decrypt(record.session_encrypted)
                except CryptoError:
                    await repo.set_account_status(
                        self._db, account_id, AccountStatus.UNAUTHORIZED
                    )
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.FAILED, error=MSG_SESSION_INVALID,
                    )
                    return

                client = await self._pool.get(account_id, session)
                checks = await self._preflight(
                    client, source, dest, max_members=self._config.max_members_per_job
                )
                first_fail = next(
                    (c for c in checks if c.status is CheckStatus.FAIL), None
                )
                if first_fail is not None:
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.FAILED, error=first_fail.message,
                    )
                    return

                params = TransferParams(
                    source=source,
                    dest=dest,
                    max_members=self._config.max_members_per_job,
                    invite_delay=self._config.invite_delay_seconds,
                    invite_jitter=self._config.invite_delay_jitter_seconds,
                    flood_wait_max=self._config.flood_wait_max_seconds,
                    deadline=loop.time() + self._config.job_timeout_seconds,
                )
                result = await self._engine.run(
                    client, params, self._progress_callback(job_id, loop), cancel_event
                )
                counters: dict[str, Any] = {
                    "total": result.total_seen,
                    "invited": result.invited,
                    "failed": result.failed,
                    "skipped": sum(result.skip_reasons.values()),
                    "skip_reasons": result.skip_reasons,
                }
                if result.was_cancelled:
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.CANCELLED, error=None, **counters,
                    )
                elif result.abort_kind is not None or result.abort_message is not None:
                    await self._mark_account_fatal(job_id, account_id, result.abort_kind)
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.FAILED, error=result.abort_message or MSG_UNEXPECTED,
                        **counters,
                    )
                else:
                    finalized = await self._finalize(
                        job_id, owner_id, account_id,
                        JobStatus.COMPLETED, error=None, **counters,
                    )
        except asyncio.CancelledError:
            # Backstop path (shutdown). The cooperative path never cancels the
            # task; finalize here unless the job already reached a final state.
            logger.warning("job %d: runner task cancelled (backstop)", job_id)
            if not finalized:
                finalized = await self._finalize(
                    job_id, owner_id, account_id, JobStatus.CANCELLED, error=None
                )
        except Exception:
            logger.exception("job %d: unexpected error in runner task", job_id)
            if not finalized:
                finalized = await self._finalize(
                    job_id, owner_id, account_id, JobStatus.FAILED, error=MSG_UNEXPECTED
                )
        finally:
            if slot_held:
                async with self._account_lock:
                    self._active_accounts.discard(account_id)
            self._cancel_events.pop(job_id, None)
            self._tasks.pop(job_id, None)

    async def _mark_account_fatal(
        self, job_id: int, account_id: int, kind: ErrorKind | None
    ) -> None:
        """Consume the engine's account-fatal classification (PRD §16):
        PeerFlood puts the account into a limited cooldown; a revoked session
        flags the account for re-login and drops the dead client. A marking
        failure is logged, never allowed to break finalization."""
        try:
            if kind is ErrorKind.PEER_FLOOD:
                until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self._config.peer_flood_cooldown_seconds)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                await repo.set_account_status(
                    self._db, account_id, AccountStatus.LIMITED, limited_until=until
                )
                await repo.audit(
                    self._db, "account_limited", account_id=account_id,
                    detail={"reason": "peer_flood", "until": until},
                )
                logger.warning("account %d limited until %s (PeerFlood)", account_id, until)
            elif kind is ErrorKind.AUTH_REVOKED:
                await repo.set_account_status(
                    self._db, account_id, AccountStatus.UNAUTHORIZED
                )
                await self._pool.discard(account_id)
                await repo.audit(
                    self._db, "account_unauthorized", account_id=account_id,
                    detail={"reason": "auth_revoked"},
                )
                logger.warning("account %d marked unauthorized (revoked session)", account_id)
        except Exception:
            logger.warning(
                "job %d: failed to mark account %d fatal", job_id, account_id,
                exc_info=True,
            )

    async def _finalize(
        self,
        job_id: int,
        owner_id: int,
        account_id: int,
        status: JobStatus,
        *,
        error: str | None,
        total: int | None = None,
        invited: int | None = None,
        skipped: int | None = None,
        failed: int | None = None,
        skip_reasons: dict[str, int] | None = None,
    ) -> bool:
        """Exactly-once finalization: the guarded CAS from ``running`` is
        write-once, so a cancel racing completion can never double-finalize.
        Returns True when this call won the finalization."""
        won = await repo.transition_job(
            self._db,
            job_id,
            (JobStatus.RUNNING,),
            status,
            error=error if error is not None else ...,
            finished=True,
        )
        if not won:
            return False  # someone else already finalized (e.g. boot recovery)
        counters = {
            "total": total,
            "invited": invited,
            "skipped": skipped,
            "failed": failed,
            "skip_reasons": skip_reasons,
        }
        try:
            await repo.update_job_progress(self._db, job_id, **counters)
        except Exception:  # pragma: no cover - defensive
            logger.warning("job %d: final counter write failed", job_id, exc_info=True)
        try:
            await self._bus.publish(
                JobFinishedEvent(
                    job_id=job_id,
                    status=status,
                    invited=invited or 0,
                    skipped=skipped or 0,
                    failed=failed or 0,
                    error=error,
                )
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning("job %d: finished event publish failed", job_id, exc_info=True)
        await repo.audit(
            self._db,
            "job_finished",
            owner_id=owner_id,
            account_id=account_id,
            job_id=job_id,
            detail={"status": status.value},
        )
        return True

    # ------------------------------------------------------------------ helpers

    def _progress_callback(
        self, job_id: int, loop: asyncio.AbstractEventLoop
    ) -> Callable[[ProgressSnapshot], Awaitable[None]]:
        """Exception-safe ``on_progress``: throttled DB writes (>= 2 s apart or
        on phase change) plus a JobProgressEvent on every engine snapshot. A
        reporting failure is logged, never allowed to kill the job (the engine
        propagates ``on_progress`` exceptions)."""
        state = {"last_write": 0.0, "last_phase": ""}

        async def on_progress(snapshot: ProgressSnapshot) -> None:
            phase_changed = snapshot.phase != state["last_phase"]
            state["last_phase"] = snapshot.phase
            if phase_changed or (
                loop.time() - state["last_write"] >= _PROGRESS_DB_MIN_INTERVAL
            ):
                try:
                    await repo.update_job_progress(
                        self._db,
                        job_id,
                        status_detail=snapshot.note or None,
                        total=snapshot.total,
                        invited=snapshot.invited,
                        skipped=snapshot.skipped,
                        failed=snapshot.failed,
                    )
                    state["last_write"] = loop.time()
                except Exception:
                    logger.warning(
                        "job %d: progress DB write failed", job_id, exc_info=True
                    )
            try:
                await self._bus.publish(
                    JobProgressEvent(
                        job_id=job_id,
                        phase=snapshot.phase,
                        done=snapshot.done,
                        total=snapshot.total,
                        invited=snapshot.invited,
                        skipped=snapshot.skipped,
                        failed=snapshot.failed,
                        wait_left=snapshot.wait_left,
                        note=snapshot.note,
                    )
                )
            except Exception:  # pragma: no cover - publish never raises
                logger.warning(
                    "job %d: progress event publish failed", job_id, exc_info=True
                )

        return on_progress

    @staticmethod
    async def _default_preflight(
        client: Any,
        source: ResolvedEntity,
        dest: ResolvedEntity,
        *,
        max_members: int,
    ) -> list[Check]:
        from app.tg.preflight import run_preflight  # lazy: keeps core import light

        return await run_preflight(client, source, dest, max_members=max_members)
