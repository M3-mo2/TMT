"""Tests for app.db.repositories: owner scoping, guarded transitions, recovery."""

from __future__ import annotations

import json
from typing import Any

from app.core.models import AccountStatus, JobStatus
from app.db.database import Database
from app.db import repositories as repo


async def add_user(db: Database, user_id: int) -> None:
    await repo.upsert_user(db, user_id)


async def add_account(
    db: Database,
    owner_id: int = 1,
    tg_user_id: int = 100,
    session: str = "enc-session",
) -> int:
    await add_user(db, owner_id)
    return await repo.upsert_account(
        db,
        owner_id=owner_id,
        phone="+15550001111",
        tg_user_id=tg_user_id,
        tg_username="tg",
        display_name="TG",
        session_encrypted=session,
    )


async def add_job(
    db: Database,
    owner_id: int = 1,
    account_id: int | None = None,
    **kwargs: Any,
) -> int:
    if account_id is None:
        account_id = await add_account(db, owner_id=owner_id)
    return await repo.insert_job(
        db,
        owner_id=owner_id,
        account_id=account_id,
        source_ref="@src",
        dest_ref="@dst",
        **kwargs,
    )


async def force_status(db: Database, job_id: int, status: JobStatus) -> None:
    await db.execute("UPDATE jobs SET status=? WHERE id=?", (status.value, job_id))


# ---------------------------------------------------------------- users


async def test_upsert_user_is_idempotent(db: Database) -> None:
    is_new = await repo.upsert_user(db, 42)
    assert is_new is True
    is_new_again = await repo.upsert_user(db, 42)
    assert is_new_again is False
    rows = await db.fetch_all("SELECT id FROM users WHERE id=42")
    assert [r["id"] for r in rows] == [42]


async def test_is_user_blocked(db: Database) -> None:
    await add_user(db, 1)
    assert not await repo.is_user_blocked(db, 1)
    await db.execute("UPDATE users SET is_blocked=1 WHERE id=1")
    assert await repo.is_user_blocked(db, 1)
    assert not await repo.is_user_blocked(db, 999)  # unknown user


async def test_upsert_user_stores_and_preserves_names(db: Database) -> None:
    await repo.upsert_user(db, 1, first_name="سارة", last_name="محمد", username="sarah")
    # a name-less re-upsert (e.g. account_service save_login) must not clobber them
    await repo.upsert_user(db, 1)
    row = await repo.get_user(db, 1)
    assert row is not None
    assert row["first_name"] == "سارة"
    assert row["last_name"] == "محمد"
    assert row["username"] == "sarah"


async def test_get_user_returns_none_for_missing(db: Database) -> None:
    assert await repo.get_user(db, 999) is None


async def test_set_user_blocked_toggles(db: Database) -> None:
    await add_user(db, 1)
    await repo.set_user_blocked(db, 1, True)
    assert (await repo.get_user(db, 1))["is_blocked"] == 1
    await repo.set_user_blocked(db, 1, False)
    assert (await repo.get_user(db, 1))["is_blocked"] == 0


async def test_user_job_stats_are_scoped_to_owner(db: Database) -> None:
    await add_user(db, 1)
    await add_user(db, 2)
    a1 = await add_account(db, owner_id=1)
    j1 = await add_job(db, owner_id=1, account_id=a1)
    j2 = await add_job(db, owner_id=1, account_id=a1)
    j3 = await add_job(db, owner_id=2, account_id=a1)
    await force_status(db, j1, JobStatus.COMPLETED)
    await force_status(db, j2, JobStatus.FAILED)
    await force_status(db, j3, JobStatus.RUNNING)
    assert await repo.count_accounts_for_user(db, 1) == 1
    assert await repo.count_jobs_for_user(db, 1) == 2
    assert await repo.count_completed_jobs_for_user(db, 1) == 1
    assert await repo.count_failed_jobs_for_user(db, 1) == 1
    assert await repo.count_active_jobs_for_user(db, 1) == 0
    assert await repo.count_active_jobs_for_user(db, 2) == 1


# ---------------------------------------------------------------- accounts


async def test_account_upsert_insert_then_replace_session(db: Database) -> None:
    account_id = await add_account(db, owner_id=1, tg_user_id=100, session="old")
    await repo.set_account_status(
        db, account_id, AccountStatus.LIMITED, limited_until="2026-01-01T00:00:00Z"
    )
    await repo.touch_account_validated(db, account_id)

    same = await repo.upsert_account(
        db,
        owner_id=1,
        phone="+15550002222",
        tg_user_id=100,
        tg_username="tg2",
        display_name="TG2",
        session_encrypted="new",
    )
    assert same == account_id
    record = await repo.get_account(db, 1, same)
    assert record is not None
    assert record.session_encrypted == "new"
    assert record.phone == "+15550002222"
    assert record.status is AccountStatus.ACTIVE
    assert record.limited_until is None
    assert record.last_validated_at is None

    other = await add_account(db, owner_id=1, tg_user_id=200, session="enc")
    assert other != account_id


async def test_owner_isolation_get_and_delete(db: Database) -> None:
    account_id = await add_account(db, owner_id=1)
    assert await repo.get_account(db, 2, account_id) is None
    assert not await repo.delete_account(db, 2, account_id)
    assert await repo.get_account(db, 1, account_id) is not None
    assert await repo.delete_account(db, 1, account_id)
    assert await repo.get_account(db, 1, account_id) is None


async def test_delete_account_with_audit(db: Database) -> None:
    account_id = await add_account(db, owner_id=1)
    job_id = await add_job(db, owner_id=1, account_id=account_id)

    assert not await repo.delete_account_with_audit(db, 2, account_id)
    audits = await db.fetch_all(
        "SELECT event FROM audit_log WHERE event='account_removed'"
    )
    assert audits == []  # failed delete writes no audit row

    assert await repo.delete_account_with_audit(db, 1, account_id)
    audits = await db.fetch_all(
        "SELECT owner_id, account_id FROM audit_log WHERE event='account_removed'"
    )
    assert [(a["owner_id"], a["account_id"]) for a in audits] == [(1, account_id)]
    # job history survives with a NULL account link (migration v2)
    job = await db.fetch_one("SELECT account_id FROM jobs WHERE id=?", (job_id,))
    assert job is not None and job["account_id"] is None


async def test_set_account_status_and_touch_validated(db: Database) -> None:
    account_id = await add_account(db)
    record = await repo.get_account(db, 1, account_id)
    assert record is not None
    assert record.last_validated_at is None

    await repo.set_account_status(
        db, account_id, AccountStatus.LIMITED, limited_until="2026-09-07T00:00:00Z"
    )
    await repo.touch_account_validated(db, account_id)
    record = await repo.get_account(db, 1, account_id)
    assert record is not None
    assert record.status is AccountStatus.LIMITED
    assert record.limited_until == "2026-09-07T00:00:00Z"
    assert record.last_validated_at is not None


# ---------------------------------------------------------------- jobs


async def test_job_insert_get_list_owner_scoping(db: Database) -> None:
    job1 = await add_job(db, owner_id=1)
    job2 = await add_job(db, owner_id=1)
    job3 = await add_job(db, owner_id=2)

    job = await repo.get_job(db, 1, job1)
    assert job is not None
    assert job.status is JobStatus.CREATED
    assert job.source_ref == "@src"
    assert await repo.get_job(db, 2, job1) is None  # wrong owner

    mine = await repo.list_jobs(db, 1)
    assert [j.id for j in mine] == [job2, job1]  # newest first, own only
    assert [j.id for j in await repo.list_jobs(db, 1, limit=1)] == [job2]
    assert [j.id for j in await repo.list_jobs(db, 2)] == [job3]


async def test_transition_job_guarded_cas(db: Database) -> None:
    job_id = await add_job(db)

    assert not await repo.transition_job(
        db, job_id, [JobStatus.RUNNING], JobStatus.COMPLETED, finished=True
    )  # wrong from-status

    assert await repo.transition_job(
        db, job_id, [JobStatus.CREATED, JobStatus.QUEUED], JobStatus.RUNNING,
        started=True,
    )
    job = await repo.get_job_any_owner(db, job_id)
    assert job is not None and job.status is JobStatus.RUNNING
    row = await db.fetch_one("SELECT started_at FROM jobs WHERE id=?", (job_id,))
    assert row["started_at"] is not None

    assert await repo.transition_job(
        db, job_id, [JobStatus.RUNNING], JobStatus.COMPLETED, finished=True
    )
    # Final states are write-once.
    assert not await repo.transition_job(
        db, job_id, [JobStatus.RUNNING, JobStatus.COMPLETED], JobStatus.FAILED,
        error="late failure", finished=True,
    )
    job = await repo.get_job_any_owner(db, job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED
    assert job.error is None
    row = await db.fetch_one(
        "SELECT finished_at, error FROM jobs WHERE id=?", (job_id,)
    )
    assert row["finished_at"] is not None


async def test_update_job_progress_partial_counters(db: Database) -> None:
    job_id = await add_job(db)
    await repo.update_job_progress(
        db, job_id, total=10, invited=3, status_detail="جارٍ الإضافة"
    )
    job = await repo.get_job_any_owner(db, job_id)
    assert job is not None
    assert (job.total, job.invited, job.skipped, job.failed) == (10, 3, 0, 0)
    assert job.status_detail == "جارٍ الإضافة"

    await repo.update_job_progress(
        db,
        job_id,
        skipped=2,
        failed=1,
        skip_reasons={"privacy": 2, "bot": 1},
    )
    job = await repo.get_job_any_owner(db, job_id)
    assert job is not None
    assert (job.skipped, job.failed) == (2, 1)
    assert job.skip_reasons == {"privacy": 2, "bot": 1}
    row = await db.fetch_one("SELECT skip_reasons FROM jobs WHERE id=?", (job_id,))
    assert json.loads(row["skip_reasons"]) == {"privacy": 2, "bot": 1}


async def test_count_active_jobs_for_account_and_user(db: Database) -> None:
    account1 = await add_account(db, owner_id=1, tg_user_id=100)
    account2 = await add_account(db, owner_id=1, tg_user_id=200)
    await add_user(db, 2)

    j1 = await add_job(db, owner_id=1, account_id=account1)
    j2 = await add_job(db, owner_id=1, account_id=account1)
    j3 = await add_job(db, owner_id=1, account_id=account2)
    j4 = await add_job(db, owner_id=2, account_id=account2)

    await force_status(db, j1, JobStatus.RUNNING)
    await force_status(db, j2, JobStatus.QUEUED)
    await force_status(db, j3, JobStatus.VALIDATING)
    await force_status(db, j4, JobStatus.COMPLETED)

    assert await repo.count_active_jobs_for_account(db, account1) == 2
    assert await repo.count_active_jobs_for_account(db, account2) == 1
    assert await repo.count_active_jobs_for_user(db, 1) == 3
    assert await repo.count_active_jobs_for_user(db, 2) == 0  # completed not active


async def test_recover_interrupted_jobs_marks_exactly_once(db: Database) -> None:
    account_id = await add_account(db)
    running = await add_job(db, account_id=account_id)
    created = await add_job(db, account_id=account_id)
    completed = await add_job(db, account_id=account_id)
    await force_status(db, completed, JobStatus.COMPLETED)

    assert await repo.recover_interrupted_jobs(db) == 2
    running_job = await repo.get_job_any_owner(db, running)
    created_job = await repo.get_job_any_owner(db, created)
    completed_job = await repo.get_job_any_owner(db, completed)
    assert running_job is not None and running_job.status is JobStatus.INTERRUPTED
    assert running_job.error is not None
    assert created_job is not None and created_job.status is JobStatus.INTERRUPTED
    assert completed_job is not None and completed_job.status is JobStatus.COMPLETED
    row = await db.fetch_one("SELECT finished_at FROM jobs WHERE id=?", (running,))
    assert row["finished_at"] is not None

    # Second boot recovery must not touch already-final jobs.
    assert await repo.recover_interrupted_jobs(db) == 0


async def test_set_cancel_requested_is_idempotent(db: Database) -> None:
    job_id = await add_job(db)
    assert not await repo.cancel_requested(db, job_id)
    await repo.set_cancel_requested(db, job_id)
    await repo.set_cancel_requested(db, job_id)
    assert await repo.cancel_requested(db, job_id)
    row = await db.fetch_one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
    assert row["cancel_requested"] == 1


async def test_audit_writes_rows(db: Database) -> None:
    await repo.audit(db, "job.started", owner_id=1, job_id=7, detail={"x": 1})
    row = await db.fetch_one(
        "SELECT * FROM audit_log WHERE event='job.started'"
    )
    assert row is not None
    assert row["owner_id"] == 1
    assert row["job_id"] == 7
    assert json.loads(row["detail"]) == {"x": 1}
    assert row["ts"]
