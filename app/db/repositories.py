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


async def upsert_user(
    db: Database,
    user_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
) -> bool:
    """Insert the user, or refresh their contact name + ``updated_at``.

    Name columns use ``COALESCE`` so a name-less caller (``account_service``
    save_login) refreshes ``updated_at`` without ever clobbering a name that the
    middleware already captured (RULES §3: never lose data silently).

    Returns ``True`` when the user was *new* (first contact), ``False`` when
    this was an update to an existing row.  Callers that do not need the
    distinction (all current callers except the middleware) may simply ignore
    the return value."""
    ts = now_iso()
    existing = await db.fetch_one(
        "SELECT 1 FROM users WHERE id=?", (user_id,)
    )
    await db.execute(
        "INSERT INTO users (id, first_name, last_name, username, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "first_name=COALESCE(excluded.first_name, first_name), "
        "last_name=COALESCE(excluded.last_name, last_name), "
        "username=COALESCE(excluded.username, username), "
        "updated_at=excluded.updated_at",
        (user_id, first_name, last_name, username, ts, ts),
    )
    return existing is None


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


# ---------------------------------------------------------------- admin / channels


async def list_channels(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, channel_id, title, invite_link, is_active, type FROM channels ORDER BY type, id"
    )
    return [dict(row) for row in rows]


async def add_channel(
    db: Database, *, channel_id: int, title: str, invite_link: str, entry_type: str = "channel"
) -> int:
    return await db.execute(
        "INSERT INTO channels (channel_id, title, invite_link, type, is_active) VALUES (?, ?, ?, ?, 1)",
        (channel_id, title, invite_link, entry_type),
    )


async def toggle_channel(db: Database, channel_db_id: int) -> bool:
    cur = await db.conn.execute(
        "UPDATE channels SET is_active = 1 - is_active WHERE id=?", (channel_db_id,)
    )
    return cur.rowcount > 0


async def delete_channel(db: Database, channel_db_id: int) -> bool:
    cur = await db.conn.execute(
        "DELETE FROM channels WHERE id=?", (channel_db_id,)
    )
    return cur.rowcount > 0


async def active_channels(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT channel_id, title, invite_link, type FROM channels WHERE is_active=1 ORDER BY type, id"
    )
    return [dict(row) for row in rows]


async def list_channels_by_type(db: Database, entry_type: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, channel_id, title, invite_link, is_active, type FROM channels WHERE type=? ORDER BY id",
        (entry_type,),
    )
    return [dict(row) for row in rows]


async def count_channels_by_type(db: Database, entry_type: str) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM channels WHERE type=? AND is_active=1",
        (entry_type,),
    )
    return int(row["c"]) if row else 0


async def is_gate_cleared(db: Database, user_id: int) -> bool:
    row = await db.fetch_one("SELECT gate_cleared FROM users WHERE id=?", (user_id,))
    return bool(row["gate_cleared"]) if row else False


async def set_gate_cleared(db: Database, user_id: int) -> None:
    await db.execute("UPDATE users SET gate_cleared=1 WHERE id=?", (user_id,))


async def reset_user_gates(db: Database) -> None:
    """Reset gate_cleared for all users — call when mandatory channels change."""
    await db.execute("UPDATE users SET gate_cleared=0")


async def count_users(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM users")
    return int(row["c"]) if row else 0


async def count_jobs(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM jobs")
    return int(row["c"]) if row else 0


async def count_completed_jobs(db: Database) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM jobs WHERE status=?",
        (JobStatus.COMPLETED.value,),
    )
    return int(row["c"]) if row else 0


async def list_all_users(db: Database, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, first_name, last_name, username, created_at, updated_at, is_blocked "
        "FROM users ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(row) for row in rows]


async def get_user(db: Database, user_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT id, first_name, last_name, username, created_at, updated_at, is_blocked "
        "FROM users WHERE id=?",
        (user_id,),
    )
    return dict(row) if row else None


async def set_user_blocked(db: Database, user_id: int, blocked: bool) -> None:
    if blocked:
        await db.execute(
            "UPDATE users SET is_blocked=1 WHERE id=? AND is_blocked=0", (user_id,)
        )
    else:
        await db.execute(
            "UPDATE users SET is_blocked=0 WHERE id=? AND is_blocked=1", (user_id,)
        )


async def count_accounts_for_user(db: Database, owner_id: int) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM accounts WHERE owner_id=?", (owner_id,))
    return int(row["c"]) if row else 0


async def count_jobs_for_user(db: Database, owner_id: int) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS c FROM jobs WHERE owner_id=?", (owner_id,))
    return int(row["c"]) if row else 0


async def count_completed_jobs_for_user(db: Database, owner_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM jobs WHERE owner_id=? AND status=?",
        (owner_id, JobStatus.COMPLETED.value),
    )
    return int(row["c"]) if row else 0


async def count_failed_jobs_for_user(db: Database, owner_id: int) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM jobs WHERE owner_id=? AND status=?",
        (owner_id, JobStatus.FAILED.value),
    )
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------- broadcasts


# Columns that ``set_broadcast_status`` is allowed to update beyond ``status``.
_BCAST_COUNTER_COLUMNS: frozenset[str] = frozenset(
    {
        "total_recipients",
        "sent",
        "blocked",
        "failed",
        "skipped",
        "cancelled",
        "avg_rate",
        "scheduled_for",
        "started_at",
        "finished_at",
        "error",
    }
)


async def create_broadcast(
    db: Database,
    *,
    admin_id: int,
    label: str,
    source_chat_id: int,
    source_message_id: int,
    mode: str = "copy",
    content_html: str | None = None,
    filter_json: str | None = None,
    scheduled_for: str | None = None,
    ab_test_id: int | None = None,
    draft_data: str | None = None,
    recurrence_rule: str | None = None,
) -> int:
    """Persist a new broadcast campaign.

    Returns the new ``broadcasts.id`` surrogate key.  ``created_at`` is set
    in application code via ``now_iso()`` (RULES §6: no SQLite-isms in the
    application layer).  ``filter_json`` stores the serialized
    ``AudienceFilter`` so ``Broadcaster.recover()`` can resume after a
    restart.

    If ``scheduled_for`` is provided the campaign starts in ``scheduled``
    status; otherwise it starts in ``draft`` status.
    """
    status = "scheduled" if scheduled_for is not None else "draft"
    return await db.execute(
        "INSERT INTO broadcasts "
        "(admin_id, label, source_chat_id, source_message_id, "
        "mode, content_html, filter_json, status, scheduled_for, "
        "ab_test_id, draft_data, recurrence_rule, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            admin_id,
            label,
            source_chat_id,
            source_message_id,
            mode,
            content_html,
            filter_json,
            status,
            scheduled_for,
            ab_test_id,
            draft_data,
            recurrence_rule,
            now_iso(),
        ),
    )


async def get_broadcast(db: Database, broadcast_id: int) -> dict[str, Any] | None:
    row = await db.fetch_one("SELECT * FROM broadcasts WHERE id=?", (broadcast_id,))
    return dict(row) if row else None


async def set_broadcast_status(
    db: Database,
    broadcast_id: int,
    status: str,
    **counters: Any,
) -> None:
    """Update ``broadcasts.status`` plus any allowed counter columns.

    Keyword arguments are validated against ``_BCAST_COUNTER_COLUMNS`` so a
    typo surfaces immediately instead of silently doing nothing.
    """
    sets: list[str] = ["status=?"]
    params: list[Any] = [status]
    for col, val in counters.items():
        if col not in _BCAST_COUNTER_COLUMNS:
            raise ValueError(f"Unknown broadcast column for set_broadcast_status: {col}")
        sets.append(f"{col}=?")
        params.append(val)
    params.append(broadcast_id)
    await db.execute(
        f"UPDATE broadcasts SET {', '.join(sets)} WHERE id=?", params
    )


async def list_broadcasts(
    db: Database, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if status is None:
        rows = await db.fetch_all(
            "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)
        )
    else:
        rows = await db.fetch_all(
            "SELECT * FROM broadcasts WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    return [dict(row) for row in rows]


async def list_scheduled_broadcasts(db: Database) -> list[dict[str, Any]]:
    """Return broadcasts with status='scheduled' whose scheduled_for <= now,
    ordered by scheduled_for ASC.  Called by the sweeper."""
    rows = await db.fetch_all(
        "SELECT * FROM broadcasts WHERE status='scheduled' AND scheduled_for <= ? "
        "ORDER BY scheduled_for ASC",
        (now_iso(),),
    )
    return [dict(row) for row in rows]


async def insert_recipients(
    db: Database, broadcast_id: int, user_ids: Iterable[int]
) -> None:
    """Bulk-insert recipient user IDs.

    Idempotent: ``ON CONFLICT DO NOTHING`` skips duplicates so re-running
    ``start`` never double-sends (BroadcastEngine.md §2.2).  All rows go
    in a single transaction (RULES §6: multi-row writes).
    """
    ids = list(user_ids)
    if not ids:
        return
    async with db.tx() as conn:
        await conn.executemany(
            "INSERT INTO broadcast_recipients (broadcast_id, user_id) "
            "VALUES (?, ?) ON CONFLICT(broadcast_id, user_id) DO NOTHING",
            [(broadcast_id, uid) for uid in ids],
        )


async def list_pending_recipients(
    db: Database, broadcast_id: int, limit: int
) -> list[int]:
    """Return user IDs of recipients still in ``pending`` status.

    Ordered by ``user_id ASC`` for deterministic cursor-based paging (the
    caller tracks the last-seen ID).  Skips any recipient already in a
    terminal/final status.
    """
    rows = await db.fetch_all(
        "SELECT user_id FROM broadcast_recipients "
        "WHERE broadcast_id=? AND status='pending' "
        "ORDER BY user_id ASC LIMIT ?",
        (broadcast_id, limit),
    )
    return [int(row["user_id"]) for row in rows]


async def update_recipient_status(
    db: Database,
    broadcast_id: int,
    user_id: int,
    status: str,
    *,
    error: str | None = None,
    increment_attempts: bool = False,
) -> None:
    """Set a recipient's status, optionally recording an error and bumping
    ``attempt_count``.  ``last_attempt_at`` is always refreshed because any
    status update represents a send attempt."""
    sets: list[str] = ["status=?"]
    params: list[Any] = [status]
    if error is not None:
        sets.append("last_error=?")
        params.append(error)
    if increment_attempts:
        sets.append("attempt_count=attempt_count+1")
    sets.append("last_attempt_at=?")
    params.append(now_iso())
    params.append(broadcast_id)
    params.append(user_id)
    await db.execute(
        f"UPDATE broadcast_recipients SET {', '.join(sets)} "
        "WHERE broadcast_id=? AND user_id=?",
        params,
    )


async def create_exclusion_list(
    db: Database, broadcast_id: int, user_ids: Iterable[int]
) -> None:
    """Bulk-insert exclusion user IDs (idempotent, single transaction)."""
    ids = list(user_ids)
    if not ids:
        return
    async with db.tx() as conn:
        await conn.executemany(
            "INSERT INTO broadcast_exclusions (broadcast_id, user_id) "
            "VALUES (?, ?) ON CONFLICT(broadcast_id, user_id) DO NOTHING",
            [(broadcast_id, uid) for uid in ids],
        )


async def list_exclusion_ids(db: Database, broadcast_id: int) -> list[int]:
    rows = await db.fetch_all(
        "SELECT user_id FROM broadcast_exclusions WHERE broadcast_id=? ORDER BY user_id",
        (broadcast_id,),
    )
    return [int(row["user_id"]) for row in rows]


async def count_recipients(db: Database, broadcast_id: int) -> dict[str, int]:
    """Aggregate recipient statuses for a broadcast into a single dict.

    Always returns all six keys (including ``delivered``) so callers never
    need to handle missing keys.
    """
    row = await db.fetch_one(
        "SELECT "
        "COUNT(CASE WHEN status='pending' THEN 1 END) AS pending, "
        "COUNT(CASE WHEN status='sent' THEN 1 END) AS sent, "
        "COUNT(CASE WHEN status='blocked' THEN 1 END) AS blocked, "
        "COUNT(CASE WHEN status='failed' THEN 1 END) AS failed, "
        "COUNT(CASE WHEN status='skipped' THEN 1 END) AS skipped, "
        "COUNT(CASE WHEN status='delivered' THEN 1 END) AS delivered "
        "FROM broadcast_recipients WHERE broadcast_id=?",
        (broadcast_id,),
    )
    if row is None:
        return {
            "pending": 0,
            "sent": 0,
            "blocked": 0,
            "failed": 0,
            "skipped": 0,
            "delivered": 0,
        }
    return {
        "pending": int(row["pending"] or 0),
        "sent": int(row["sent"] or 0),
        "blocked": int(row["blocked"] or 0),
        "failed": int(row["failed"] or 0),
        "skipped": int(row["skipped"] or 0),
        "delivered": int(row["delivered"] or 0),
    }


# ---------------------------------------------------------------- notifications


async def create_notification(
    db: Database,
    *,
    owner_id: int,
    event_type: str,
    severity: str = "info",
    title: str = "",
    body: str = "",
    data: dict[str, Any] | None = None,
) -> int:
    """Insert a notification row for *owner_id* (an admin id).

    Returns the new notification id.  ``created_at`` is set in application code
    via ``now_iso()`` (RULES §6)."""
    return await db.execute(
        "INSERT INTO notifications "
        "(owner_id, event_type, severity, title, body, data, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            owner_id,
            event_type,
            severity,
            title,
            body,
            json.dumps(data or {}, ensure_ascii=False),
            now_iso(),
        ),
    )


async def list_notifications(
    db: Database,
    owner_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    include_dismissed: bool = False,
) -> list[dict[str, Any]]:
    """Return notification rows for *owner_id*, newest first.

    ``unread_only`` filters to rows where ``read_at IS NULL``.
    ``include_dismissed`` controls whether dismissed rows appear (default:
    excluded, mirroring a UI inbox).  ``offset`` supports pagination."""
    where: list[str] = ["owner_id=?"]
    params: list[Any] = [owner_id]
    if unread_only:
        where.append("read_at IS NULL")
    if not include_dismissed:
        where.append("dismissed = 0")
    clause = " AND ".join(where)
    rows = await db.fetch_all(
        f"SELECT * FROM notifications WHERE {clause} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [dict(row) for row in rows]


async def count_unread_notifications(db: Database, owner_id: int) -> int:
    """Count unread, non-dismissed notifications for *owner_id*."""
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM notifications "
        "WHERE owner_id=? AND read_at IS NULL AND dismissed=0",
        (owner_id,),
    )
    return int(row["c"]) if row else 0


async def mark_notification_read(db: Database, notification_id: int) -> bool:
    """Mark a single notification as read; returns True if a row was updated."""
    cur = await db.conn.execute(
        "UPDATE notifications SET read_at=? "
        "WHERE id=? AND read_at IS NULL",
        (now_iso(), notification_id),
    )
    return cur.rowcount > 0


async def mark_all_notifications_read(db: Database, owner_id: int) -> int:
    """Mark all unread notifications for *owner_id* as read; returns affected count."""
    cur = await db.conn.execute(
        "UPDATE notifications SET read_at=? "
        "WHERE owner_id=? AND read_at IS NULL",
        (now_iso(), owner_id),
    )
    return cur.rowcount


async def dismiss_notification(db: Database, notification_id: int) -> bool:
    """Dismiss (hide) a notification; returns True if a row was updated."""
    cur = await db.conn.execute(
        "UPDATE notifications SET dismissed=1, read_at=COALESCE(read_at, ?) "
        "WHERE id=? AND dismissed=0",
        (now_iso(), notification_id),
    )
    return cur.rowcount > 0


async def delete_notification(db: Database, notification_id: int) -> bool:
    """Permanently delete a notification row; returns True if a row was deleted."""
    cur = await db.conn.execute(
        "DELETE FROM notifications WHERE id=?", (notification_id,)
    )
    return cur.rowcount > 0


async def set_notification_setting(
    db: Database, owner_id: int, event_type: str, enabled: bool
) -> None:
    """Enable or disable notifications of *event_type* for *owner_id*.

    Idempotent upsert (no SQLite-isms, portable to PostgreSQL)."""
    await db.execute(
        "INSERT INTO notification_settings (owner_id, event_type, enabled) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(owner_id, event_type) DO UPDATE SET enabled=excluded.enabled",
        (owner_id, event_type, 1 if enabled else 0),
    )


async def is_notification_enabled(
    db: Database, owner_id: int, event_type: str
) -> bool:
    """Check whether notifications of *event_type* are enabled for *owner_id*.

    Missing rows default to enabled (fail-open at insert time when the
    NotificationService logs a row, so the default is the safe one)."""
    row = await db.fetch_one(
        "SELECT enabled FROM notification_settings "
        "WHERE owner_id=? AND event_type=?",
        (owner_id, event_type),
    )
    if row is None:
        return True
    return bool(row["enabled"])
