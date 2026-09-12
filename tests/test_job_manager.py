"""Tests for app.core.job_manager: validation, dispatch, progress, cancellation,
exactly-once finalization, recovery, shutdown. Real DB + real crypto, fake
pool/client/engine and an injected preflight runner (offline only)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import Config
from app.core.account_service import ServiceError
from app.core.events import EventBus, JobFinishedEvent, JobProgressEvent
from app.core.job_manager import JobManager
from app.core.models import AccountStatus, Check, CheckStatus, JobStatus, ResolvedEntity
from app.db import repositories as repo
from app.db.database import Database
from app.security.crypto import SessionCrypto
from app.tg.errors import ErrorKind
from app.tg.transfer import ProgressSnapshot, TransferResult
from tests.fakes import FakeClientPool

OWNER = 1
PLAIN_SESSION = "plain-telethon-session-string"

Preflight = Callable[..., Awaitable[list[Check]]]


def make_config(**overrides: Any) -> Config:
    return Config(
        bot_token="123456:TEST-TOKEN", api_id=1, api_hash="0123456789abcdef",
        **overrides,
    )


@pytest.fixture
def pool() -> FakeClientPool:
    return FakeClientPool()


def entity(
    id: int = 111, kind: str = "supergroup", title: str = "Src",
    is_megagroup: bool = False, raw_ref: str = "@src",
) -> ResolvedEntity:
    return ResolvedEntity(
        id=id, kind=kind, title=title, is_megagroup=is_megagroup, raw_ref=raw_ref
    )


async def ok_preflight(client: Any, source: ResolvedEntity, dest: ResolvedEntity,
                       *, max_members: int) -> list[Check]:
    return [Check(key="dest_diff", status=CheckStatus.PASS, message="ok")]


class FakeEngine:
    """Scriptable TransferEngine double; records every run call."""

    def __init__(
        self,
        result: TransferResult | None = None,
        snapshots: tuple[ProgressSnapshot, ...] = (),
        *,
        wait_for_cancel: bool = False,
        exc: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.result = result or TransferResult(
            invited=0, failed=0, total_seen=0, skip_reasons={},
            was_cancelled=False, abort_kind=None, abort_message=None,
        )
        self.snapshots = snapshots
        self.wait_for_cancel = wait_for_cancel
        self.exc = exc
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def run(self, client: Any, params: Any, on_progress: Callable[[ProgressSnapshot], Awaitable[None]],
                  cancel_event: asyncio.Event) -> TransferResult:
        self.calls.append({"client": client, "params": params, "cancel": cancel_event})
        for snap in self.snapshots:
            await on_progress(snap)
        if self.wait_for_cancel:
            while not cancel_event.is_set():
                await asyncio.sleep(0.01)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


def completed_result(
    invited: int = 2, failed: int = 1, total_seen: int = 5,
    skip_reasons: dict[str, int] | None = None,
) -> TransferResult:
    return TransferResult(
        invited=invited, failed=failed, total_seen=total_seen,
        skip_reasons=skip_reasons or {"bot": 2}, was_cancelled=False,
        abort_kind=None, abort_message=None,
    )


class Harness:
    def __init__(self, db: Database, crypto: SessionCrypto, pool: FakeClientPool,
                 config: Config, engine: FakeEngine, preflight: Preflight) -> None:
        self.bus = EventBus()
        self.finished: list[JobFinishedEvent] = []
        self.progress: list[JobProgressEvent] = []
        self.bus.subscribe(self._on_finish)
        self.bus.subscribe(self._on_progress)
        self.jm = JobManager(
            db, pool, crypto, self.bus, config, engine,
            preflight_runner=preflight,
        )

    async def _on_finish(self, event: Any) -> None:
        if isinstance(event, JobFinishedEvent):
            self.finished.append(event)

    async def _on_progress(self, event: Any) -> None:
        if isinstance(event, JobProgressEvent):
            self.progress.append(event)


async def make_account(db: Database, crypto: SessionCrypto, owner_id: int = OWNER,
                       tg_user_id: int = 777) -> int:
    await repo.upsert_user(db, owner_id)
    return await repo.upsert_account(
        db, owner_id=owner_id, phone="+15550001111", tg_user_id=tg_user_id,
        tg_username="tguser", display_name="TG User",
        session_encrypted=crypto.encrypt(PLAIN_SESSION),
    )


async def wait_for_status(db: Database, job_id: int, *statuses: JobStatus,
                          timeout: float = 5.0) -> JobStatus:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = await db.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
        assert row is not None
        status = JobStatus(row["status"])
        if status in statuses:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {statuses}")


# ---------------------------------------------------------------- validation


async def test_create_job_requires_owned_account(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    engine = FakeEngine()
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    with pytest.raises(ServiceError):  # foreign owner
        await h.jm.create_job(OWNER + 1, account_id, entity(), entity(id=222, raw_ref="@d"))
    with pytest.raises(ServiceError):  # unknown account
        await h.jm.create_job(OWNER, account_id + 500, entity(), entity(id=222, raw_ref="@d"))
    assert engine.calls == []


async def test_create_job_rejects_unauthorized_account(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    await repo.set_account_status(db, account_id, AccountStatus.UNAUTHORIZED)
    h = Harness(db, crypto, FakeClientPool(), make_config(), FakeEngine(), ok_preflight)
    with pytest.raises(ServiceError):
        await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))


async def test_create_job_rejects_future_limit_with_remaining_time(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    until = datetime.now(timezone.utc) + timedelta(minutes=90)
    await repo.set_account_status(
        db, account_id, AccountStatus.LIMITED,
        limited_until=until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    h = Harness(db, crypto, FakeClientPool(), make_config(), FakeEngine(), ok_preflight)
    with pytest.raises(ServiceError) as excinfo:
        await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    assert "1 ساعة و 29 دقيقة" in excinfo.value.message


async def test_create_job_allows_expired_limit(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    await repo.set_account_status(
        db, account_id, AccountStatus.LIMITED,
        limited_until=past.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    h = Harness(db, crypto, FakeClientPool(), make_config(),
                FakeEngine(result=completed_result()), ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    status = await wait_for_status(db, job.id, JobStatus.COMPLETED)
    assert status is JobStatus.COMPLETED
    await h.jm.shutdown()


async def test_create_job_rejects_second_job_on_same_account(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    h = Harness(db, crypto, FakeClientPool(), make_config(),
                FakeEngine(wait_for_cancel=True), ok_preflight)
    first = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, first.id, JobStatus.RUNNING)
    with pytest.raises(ServiceError):
        await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    assert await h.jm.cancel_job(OWNER, first.id)
    await h.jm.shutdown()


async def test_create_job_enforces_per_user_capacity(db, crypto: SessionCrypto) -> None:
    acc_a = await make_account(db, crypto, tg_user_id=777)
    acc_b = await make_account(db, crypto, tg_user_id=888)
    h = Harness(db, crypto, FakeClientPool(), make_config(),
                FakeEngine(wait_for_cancel=True), ok_preflight)
    first = await h.jm.create_job(OWNER, acc_a, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, first.id, JobStatus.RUNNING)
    with pytest.raises(ServiceError):
        await h.jm.create_job(OWNER, acc_b, entity(), entity(id=222, raw_ref="@d"))
    assert await h.jm.cancel_job(OWNER, first.id)
    await h.jm.shutdown()


async def test_create_job_rejects_non_groupish_entities(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    h = Harness(db, crypto, FakeClientPool(), make_config(), FakeEngine(), ok_preflight)
    with pytest.raises(ServiceError):
        await h.jm.create_job(
            OWNER, account_id,
            entity(id=333, kind="channel", is_megagroup=False, raw_ref="@c"),
            entity(id=222, raw_ref="@d"),
        )


# ---------------------------------------------------------------- happy path


async def test_happy_path_completed_with_progress_and_counters(
    db, crypto: SessionCrypto, pool: FakeClientPool
) -> None:
    account_id = await make_account(db, crypto)
    snapshots = (
        ProgressSnapshot(phase="fetching_dest", done=0, total=0, invited=0,
                         skipped=0, failed=0),
        ProgressSnapshot(phase="inviting", done=1, total=5, invited=1, skipped=0,
                         failed=0),
        ProgressSnapshot(phase="inviting", done=3, total=5, invited=2, skipped=1,
                         failed=0),
    )
    engine = FakeEngine(result=completed_result(), snapshots=snapshots)
    h = Harness(db, crypto, pool, make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))

    status = await wait_for_status(db, job.id, JobStatus.COMPLETED)
    assert status is JobStatus.COMPLETED
    assert job.status in (JobStatus.QUEUED, JobStatus.RUNNING)  # returned early

    # client came from the pool with the decrypted session string
    assert pool.gets == [(account_id, PLAIN_SESSION)]
    assert engine.calls and engine.calls[0]["client"] is pool.clients[account_id]
    params = engine.calls[0]["params"]
    assert params.max_members == 2000
    assert params.deadline > 0

    final = await h.jm.get_job(OWNER, job.id)
    assert final is not None
    assert (final.invited, final.failed, final.total, final.skipped) == (2, 1, 5, 2)
    assert final.skip_reasons == {"bot": 2}

    phases = [e.phase for e in h.progress]
    assert phases == ["fetching_dest", "inviting", "inviting"]
    assert [(e.invited, e.failed) for e in h.progress] == [(0, 0), (1, 0), (2, 0)]

    assert len(h.finished) == 1
    assert h.finished[0].status is JobStatus.COMPLETED
    assert h.finished[0].error is None
    assert h.jm._active_accounts == set()  # slot released
    assert pool.discarded == []  # client stays cached in the pool


async def test_progress_db_writes_throttled_but_events_not(
    db, crypto: SessionCrypto, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = await make_account(db, crypto)
    writes = {"n": 0}
    real = repo.update_job_progress

    async def counting(*args: Any, **kwargs: Any) -> None:
        writes["n"] += 1
        await real(*args, **kwargs)

    monkeypatch.setattr(repo, "update_job_progress", counting)
    snapshots = tuple(
        ProgressSnapshot(phase="inviting", done=i, total=10, invited=i, skipped=0,
                         failed=0)
        for i in range(1, 9)
    )
    engine = FakeEngine(result=completed_result(invited=8, failed=0, total_seen=10,
                                                skip_reasons={"bot": 2}),
                        snapshots=snapshots)
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.COMPLETED)
    assert len(h.progress) == 8  # every snapshot published
    assert writes["n"] == 2  # one throttled in-run write (same phase) + one final write
    await h.jm.shutdown()


# ---------------------------------------------------------------- preflight


async def test_preflight_fail_fails_job_with_arabic_message(
    db, crypto: SessionCrypto, pool: FakeClientPool
) -> None:
    account_id = await make_account(db, crypto)
    fail = Check(key="source_type", status=CheckStatus.FAIL,
                 message="النوع غير مدعوم: القنوات التي لا تقبل الأعضاء والحسابات الشخصية لا يمكن استخدامها.")
    engine = FakeEngine()

    async def failing_preflight(client: Any, source: ResolvedEntity,
                                dest: ResolvedEntity, *, max_members: int) -> list[Check]:
        assert client is pool.clients[account_id]
        return [fail]

    h = Harness(db, crypto, pool, make_config(), engine, failing_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    status = await wait_for_status(db, job.id, JobStatus.FAILED)
    assert status is JobStatus.FAILED
    assert engine.calls == []  # engine never ran
    final = await h.jm.get_job(OWNER, job.id)
    assert final is not None and final.error == fail.message
    assert len(h.finished) == 1 and h.finished[0].status is JobStatus.FAILED


async def test_engine_exception_fails_job_with_generic_message(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    engine = FakeEngine(exc=RuntimeError("boom"))
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    status = await wait_for_status(db, job.id, JobStatus.FAILED)
    assert status is JobStatus.FAILED
    final = await h.jm.get_job(OWNER, job.id)
    assert final is not None
    assert final.error == "حدث خطأ غير متوقع أثناء التنفيذ."
    assert h.jm._active_accounts == set()


# ---------------------------------------------------------------- cancellation


async def test_cancel_running_job_is_cooperative_and_exactly_once(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    engine = FakeEngine(
        result=TransferResult(invited=0, failed=0, total_seen=0, skip_reasons={},
                              was_cancelled=True, abort_kind=None, abort_message=None),
        wait_for_cancel=True,
    )
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.RUNNING)

    assert await h.jm.cancel_job(OWNER, job.id) is True
    status = await wait_for_status(db, job.id, JobStatus.CANCELLED)
    assert status is JobStatus.CANCELLED
    assert await h.jm.cancel_job(OWNER, job.id) is False  # already final
    assert await h.jm.cancel_job(OWNER + 1, job.id) is False  # foreign owner

    # exactly one final event, one final state
    assert len(h.finished) == 1
    assert h.finished[0].status is JobStatus.CANCELLED
    row = await db.fetch_one(
        "SELECT status, cancel_requested FROM jobs WHERE id=?", (job.id,)
    )
    assert row is not None and row["cancel_requested"] == 1
    assert h.jm._active_accounts == set()
    await h.jm.shutdown()


async def test_cancel_queued_job_transitions_directly(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    h = Harness(db, crypto, FakeClientPool(), make_config(), FakeEngine(), ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    # The dispatch task may already have moved the job to running, so the
    # direct CAS transition can legitimately lose the race; whichever way it
    # lands there must be exactly one final state and one final event.
    await h.jm.cancel_job(OWNER, job.id)
    status = await wait_for_status(db, job.id, JobStatus.CANCELLED, JobStatus.COMPLETED)
    assert len(h.finished) == 1
    assert h.finished[0].status is status
    assert await h.jm.cancel_job(OWNER, job.id) is False  # already final
    await h.jm.shutdown()


async def test_cancel_racing_completion_never_double_finalizes(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    # engine ignores the cancel event and completes anyway
    engine = FakeEngine(result=completed_result(), delay=0.05)
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.RUNNING)
    await h.jm.cancel_job(OWNER, job.id)  # sets flag; engine still completes
    status = await wait_for_status(db, job.id, JobStatus.COMPLETED)
    assert status is JobStatus.COMPLETED  # engine result wins, no overwrite

    row = await db.fetch_one("SELECT status FROM jobs WHERE id=?", (job.id,))
    assert row is not None and row["status"] == "completed"
    assert len(h.finished) == 1  # one JobFinishedEvent, no double-finalize
    assert h.finished[0].status is JobStatus.COMPLETED
    await h.jm.shutdown()


# ---------------------------------------------------------------- recovery


async def test_recover_marks_in_flight_jobs_interrupted(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    h = Harness(db, crypto, FakeClientPool(), make_config(), FakeEngine(), ok_preflight)
    ids = []
    for status in (JobStatus.CREATED, JobStatus.QUEUED, JobStatus.RUNNING):
        job_id = await repo.insert_job(
            db, owner_id=OWNER, account_id=account_id, source_ref="@s", dest_ref="@d"
        )
        await db.execute("UPDATE jobs SET status=? WHERE id=?",
                         (status.value, job_id))
        ids.append(job_id)
    done_id = await repo.insert_job(
        db, owner_id=OWNER, account_id=account_id, source_ref="@s", dest_ref="@d"
    )
    await db.execute("UPDATE jobs SET status='completed' WHERE id=?", (done_id,))

    recovered = await h.jm.recover()
    assert recovered == 3
    for job_id in ids:
        job = await h.jm.get_job(OWNER, job_id)
        assert job is not None and job.status is JobStatus.INTERRUPTED
    done = await h.jm.get_job(OWNER, done_id)
    assert done is not None and done.status is JobStatus.COMPLETED


# ---------------------------------------------------------------- shutdown


async def test_shutdown_cancels_live_tasks_cleanly(db, crypto: SessionCrypto) -> None:
    account_id = await make_account(db, crypto)
    h = Harness(db, crypto, FakeClientPool(), make_config(),
                FakeEngine(wait_for_cancel=True), ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.RUNNING)

    await h.jm.shutdown()  # must return, not hang
    await asyncio.sleep(0.05)
    assert all(t.done() for t in h.jm._tasks.values())
    status = await wait_for_status(db, job.id, JobStatus.CANCELLED)
    assert status is JobStatus.CANCELLED  # backstop finalize keeps DB consistent
    assert h.jm._active_accounts == set()


# ---------------------------------------------------------------- account-fatal

async def test_peer_flood_abort_marks_account_limited(
    db, crypto: SessionCrypto
) -> None:
    """Final review Critical fix: the engine's account_fatal classification
    must mark the account (PRD §16), not just fail the job."""
    account_id = await make_account(db, crypto)
    engine = FakeEngine(
        result=TransferResult(
            invited=0, failed=0, total_seen=3, skip_reasons={},
            was_cancelled=False, abort_kind=ErrorKind.PEER_FLOOD,
            abort_message="قام تيليجرام بتقييد هذا الحساب من إضافة أعضاء مؤقتاً.",
        )
    )
    h = Harness(db, crypto, FakeClientPool(), make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.FAILED)

    row = await db.fetch_one("SELECT status, limited_until FROM accounts WHERE id=?", (account_id,))
    assert row["status"] == AccountStatus.LIMITED.value
    assert row["limited_until"] is not None
    # limited_until is in the future
    until = datetime.strptime(row["limited_until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert until > datetime.now(timezone.utc)

    # The cooldown is enforced: a new job on the same account is refused.
    with pytest.raises(ServiceError):
        await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))


async def test_auth_revoked_abort_marks_account_unauthorized_and_discards_client(
    db, crypto: SessionCrypto
) -> None:
    account_id = await make_account(db, crypto)
    pool = FakeClientPool()
    engine = FakeEngine(
        result=TransferResult(
            invited=0, failed=0, total_seen=0, skip_reasons={},
            was_cancelled=False, abort_kind=ErrorKind.AUTH_REVOKED,
            abort_message="انتهت صلاحية تسجيل دخول الحساب أو تم إلغاؤه، يجب إعادة إضافة الحساب.",
        )
    )
    h = Harness(db, crypto, pool, make_config(), engine, ok_preflight)
    job = await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))
    await wait_for_status(db, job.id, JobStatus.FAILED)

    row = await db.fetch_one("SELECT status, limited_until FROM accounts WHERE id=?", (account_id,))
    assert row["status"] == AccountStatus.UNAUTHORIZED.value
    assert row["limited_until"] is None
    assert account_id in pool.discarded  # dead client dropped
    # Not limited: re-login (re-add) is the remedy, not a cooldown.
    with pytest.raises(ServiceError):  # unauthorized refused at create_job
        await h.jm.create_job(OWNER, account_id, entity(), entity(id=222, raw_ref="@d"))


# ---------------------------------------------------------------- _job_final_event mapping


def test_job_final_event_severity_matches_docs() -> None:
    """Severity per docs/notifications §3.1: completed=info, failed=error,
    cancelled=warning, interrupted=warning."""
    from app.core.job_manager import _job_final_event

    assert _job_final_event(JobStatus.COMPLETED, 1, 0, 0, 0, None) == (
        "job_completed", "info", "✅|اكتملت عملية نقل",
    )
    assert _job_final_event(JobStatus.FAILED, 1, 0, 0, 0, "x") == (
        "job_failed", "error", "×|فشلت عملية نقل",
    )
    assert _job_final_event(JobStatus.CANCELLED, 1, 0, 0, 0, None) == (
        "job_cancelled", "warning", "↺|ألغيت عملية نقل",
    )
    assert _job_final_event(JobStatus.INTERRUPTED, 1, 0, 0, 0, "x") == (
        "job_interrupted", "warning", "⟡|وقفت عملية نقل",
    )


def test_job_final_body_uses_correct_arabic() -> None:
    """The body must say 'فشل' (failed), not a name like 'خالد'."""
    from app.core.job_manager import _job_final_body

    body = _job_final_body(5, JobStatus.FAILED, 10, 3, 2, "timeout")
    assert "فشل" in body
    assert "خالد" not in body
