"""Owner-scoped data access. Every user-owned query filters by owner_id
(RULES §4) — isolation is enforced here, not in handlers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from app.core.models import (
    FINAL_JOB_STATUSES,
    Account,
    AccountStatus,
    Job,
    JobStatus,
)
from app.db.database import Database

logger = logging.getLogger("app.db.repositories")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- users


async def upsert_user(db: Database, user_id: int) -> None:
    ts = now_iso()
    await db.execute(
        "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
        (user_id, ts, ts),
    )


async def is_user_blocked(db: Database, user_id: int) -> bool:
    row = await db.fetch_one("SELECT is_blocked FROM users WHERE id=?", (user_id,))
    return bool(row and row["is_blocked"])


# ---------------------------------------------------------------- accounts


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """Full account row including the encrypted session (never logged)."""

    id: int
    owner_id: int
    phone: str
    tg_user_id: int
    tg_username: str | None
    display_name: str
    session_encrypted: str
    status: AccountStatus
    limited_until: str | None
    added_at: str
    last_validated_at: str | None

    def to_public(self) -> Account:
        return Account(
            id=self.id,
            owner_id=self.owner_id,
            phone=self.phone,
            tg_user_id=self.tg_user_id,
            tg_username=self.tg_username,
            display_name=self.display_name,
            status=self.status,
            limited_until=self.limited_until,
            added_at=self.added_at,
            last_validated_at=self.last_validated_at,
        )


def _account_row(row) -> AccountRecord:
    return AccountRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        phone=row["phone"],
        tg_user_id=row["tg_user_id"],
        tg_username=row["tg_username"],
        display_name=row["display_name"],
        session_encrypted=row["session_encrypted"],
        status=AccountStatus(row["status"]),
        limited_until=row["limited_until"],
        added_at=row["added_at"],
        last_validated_at=row["last_validated_at"],
    )


async def upsert_account(
    db: Database,
    *,
    owner_id: int,
    phone: str,
    tg_user_id: int,
    tg_username: str | None,
    display_name: str,
    session_encrypted: str,
) -> int:
    """Insert the account, or replace the session if the same owner already
    added the same Telegram account. Returns the account row id."""
    ts = now_iso()
    async with db.tx() as conn:
        cur = await conn.execute(
            "SELECT id FROM accounts WHERE owner_id=? AND tg_user_id=?",
            (owner_id, tg_user_id),
        )
        existing = await cur.fetchone()
        if existing:
            await conn.execute(
                "UPDATE accounts SET phone=?, tg_username=?, display_name=?, "
                "session_encrypted=?, status='active', limited_until=NULL, "
                "last_validated_at=NULL WHERE id=?",
                (phone, tg_username, display_name, session_encrypted, existing["id"]),
            )
            return int(existing["id"])
        cur = await conn.execute(
            "INSERT INTO accounts (owner_id, phone, tg_user_id, tg_username, "
            "display_name, session_encrypted, status, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (owner_id, phone, tg_user_id, tg_username, display_name,
             session_encrypted, ts),
        )
        return int(cur.lastrowid or 0)


async def get_account(db: Database, owner_id: int, account_id: int) -> AccountRecord | None:
    row = await db.fetch_one(
        "SELECT * FROM accounts WHERE id=? AND owner_id=?", (account_id, owner_id)
    )
    return _account_row(row) if row else None


async def list_accounts(db: Database, owner_id: int) -> list[AccountRecord]:
    rows = await db.fetch_all(
        "SELECT * FROM accounts WHERE owner_id=? ORDER BY id", (owner_id,)
    )
    return [_account_row(r) for r in rows]


async def set_account_status(
    db: Database,
    account_id: int,
    status: AccountStatus,
    *,
    limited_until: str | None = None,
) -> None:
    await db.execute(
        "UPDATE accounts SET status=?, limited_until=? WHERE id=?",
        (status.value, limited_until, account_id),
    )


async def touch_account_validated(db: Database, account_id: int) -> None:
    await db.execute(
        "UPDATE accounts SET last_validated_at=? WHERE id=?", (now_iso(), account_id)
    )


async def delete_account(db: Database, owner_id: int, account_id: int) -> bool:
    cur = await db.conn.execute(
        "DELETE FROM accounts WHERE id=? AND owner_id=?", (account_id, owner_id)
    )
    return cur.rowcount > 0


async def delete_account_with_audit(db: Database, owner_id: int, account_id: int) -> bool:
    """Delete an account and write its audit entry in one transaction.

    Job history survives the deletion: since migration v2 jobs.account_id is
    nullable with ON DELETE SET NULL, so existing job rows keep their
    statistics with a NULL account link (PRD §20)."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "DELETE FROM accounts WHERE id=? AND owner_id=?", (account_id, owner_id)
        )
        if cur.rowcount == 0:
            return False
        await audit(
            db, "account_removed", owner_id=owner_id, account_id=account_id, conn=conn,
        )
        return True


# ---------------------------------------------------------------- jobs


def _job_row(row) -> Job:
    return Job(
        id=row["id"],
        owner_id=row["owner_id"],
        account_id=row["account_id"],
        source_ref=row["source_ref"],
        dest_ref=row["dest_ref"],
        source_title=row["source_title"],
        dest_title=row["dest_title"],
        status=JobStatus(row["status"]),
        status_detail=row["status_detail"],
        total=row["total"],
        invited=row["invited"],
        skipped=row["skipped"],
        failed=row["failed"],
        skip_reasons=json.loads(row["skip_reasons"] or "{}"),
        error=row["error"],
    )


async def insert_job(
    db: Database,
    *,
    owner_id: int,
    account_id: int,
    source_ref: str,
    dest_ref: str,
    source_title: str = "",
    dest_title: str = "",
) -> int:
    return await db.execute(
        "INSERT INTO jobs (owner_id, account_id, source_ref, dest_ref, "
        "source_title, dest_title, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'created', ?)",
        (owner_id, account_id, source_ref, dest_ref, source_title, dest_title, now_iso()),
    )


async def get_job(db: Database, owner_id: int, job_id: int) -> Job | None:
    row = await db.fetch_one(
        "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner_id)
    )
    return _job_row(row) if row else None


async def get_job_any_owner(db: Database, job_id: int) -> Job | None:
    """Internal use only (recovery, reporter)."""
    row = await db.fetch_one("SELECT * FROM jobs WHERE id=?", (job_id,))
    return _job_row(row) if row else None


async def list_jobs(db: Database, owner_id: int, limit: int = 10) -> list[Job]:
    rows = await db.fetch_all(
        "SELECT * FROM jobs WHERE owner_id=? ORDER BY id DESC LIMIT ?", (owner_id, limit)
    )
    return [_job_row(r) for r in rows]


async def transition_job(
    db: Database,
    job_id: int,
    from_statuses: Iterable[JobStatus],
    to_status: JobStatus,
    *,
    status_detail: str | None = ...,
    error: str | None = ...,
    started: bool = False,
    finished: bool = False,
) -> bool:
    """Guarded compare-and-set transition; final states are write-once."""
    from_list = ",".join("?" * len(from_statuses))
    sets = ["status=?"]
    params: list[Any] = [to_status.value]
    if status_detail is not ...:
        sets.append("status_detail=?")
        params.append(status_detail)
    if error is not ...:
        sets.append("error=?")
        params.append(error)
    if started:
        sets.append("started_at=COALESCE(started_at, ?)")
        params.append(now_iso())
    if finished:
        sets.append("finished_at=?")
        params.append(now_iso())
    # Placeholder order in the SQL is: SET columns, then id=?, then the
    # status IN (...) list — parameters must follow that exact order.
    # Final states are write-once (RULES §5): a job in a final state never
    # leaves it, even if a caller lists a final status in from_statuses.
    final_list = ",".join("?" * len(FINAL_JOB_STATUSES))
    params.append(job_id)
    params.extend(s.value for s in from_statuses)
    params.extend(sorted(s.value for s in FINAL_JOB_STATUSES))
    cur = await db.conn.execute(
        f"UPDATE jobs SET {', '.join(sets)} "
        f"WHERE id=? AND status IN ({from_list}) AND status NOT IN ({final_list})",
        params,
    )
    return cur.rowcount > 0


async def update_job_progress(
    db: Database,
    job_id: int,
    *,
    status_detail: str | None = None,
    total: int | None = None,
    invited: int | None = None,
    skipped: int | None = None,
    failed: int | None = None,
    skip_reasons: dict[str, int] | None = None,
) -> None:
    sets: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("status_detail", status_detail),
        ("total", total),
        ("invited", invited),
        ("skipped", skipped),
        ("failed", failed),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if skip_reasons is not None:
        sets.append("skip_reasons=?")
        params.append(json.dumps(skip_reasons, ensure_ascii=False))
    if not sets:
        return
    params.append(job_id)
    await db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)


async def set_cancel_requested(db: Database, job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET cancel_requested=1 WHERE id=? AND cancel_requested=0", (job_id,)
    )


async def cancel_requested(db: Database, job_id: int) -> bool:
    row = await db.fetch_one(
        "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
    )
    return bool(row and row["cancel_requested"])


async def count_active_jobs_for_account(db: Database, account_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM jobs WHERE account_id=? AND status IN "
        "('created','validating','queued','running')",
        (account_id,),
    )
    return int(row["c"]) if row else 0


async def count_active_jobs_for_user(db: Database, owner_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM jobs WHERE owner_id=? AND status IN "
        "('validating','queued','running')",
        (owner_id,),
    )
    return int(row["c"]) if row else 0


async def recover_interrupted_jobs(db: Database) -> int:
    """Boot recovery: any job left mid-flight by a previous process becomes
    `interrupted`. Runs before any new job may start (RULES §5)."""
    cur = await db.conn.execute(
        "UPDATE jobs SET status='interrupted', finished_at=?, "
        "error=COALESCE(error, 'تم إيقاف العملية بسبب إعادة تشغيل الخادم.') "
        "WHERE status IN ('created','validating','queued','running')",
        (now_iso(),),
    )
    n = cur.rowcount
    if n:
        logger.warning("Boot recovery: marked %d in-flight job(s) as interrupted", n)
    return n


# ---------------------------------------------------------------- audit


async def audit(
    db: Database,
    event: str,
    *,
    owner_id: int | None = None,
    account_id: int | None = None,
    job_id: int | None = None,
    detail: dict[str, Any] | None = None,
    conn: Any | None = None,
) -> None:
    """Append an audit row; pass ``conn`` to participate in an open
    transaction instead of autocommitting."""
    executor = conn if conn is not None else db
    await executor.execute(
        "INSERT INTO audit_log (ts, owner_id, account_id, job_id, event, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            owner_id,
            account_id,
            job_id,
            event,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )
