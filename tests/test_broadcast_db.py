"""Tests for broadcast campaign DB schema (Migration V5) and repository layer."""

from __future__ import annotations

import pytest

from app.db.database import Database
from app.db import repositories as repo


async def add_admin(db: Database, admin_id: int = 1) -> None:
    await repo.upsert_user(db, admin_id)


async def add_users(db: Database, count: int) -> list[int]:
    for uid in range(1, count + 1):
        await repo.upsert_user(db, uid)
    return list(range(1, count + 1))


async def make_broadcast(
    db: Database,
    admin_id: int = 1,
    label: str = "test",
    mode: str = "copy",
    content_html: str | None = None,
) -> int:
    return await repo.create_broadcast(
        db,
        admin_id=admin_id,
        label=label,
        source_chat_id=100,
        source_message_id=200,
        mode=mode,
        content_html=content_html,
    )


# ---------------------------------------------------------------- migration


async def test_migration_v5_creates_broadcast_tables(db: Database) -> None:
    tables = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"broadcasts", "broadcast_recipients", "broadcast_exclusions"} <= tables

    bcast_cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcasts)")}
    assert {
        "id", "admin_id", "label", "source_chat_id", "source_message_id",
        "mode", "content_html", "status", "scheduled_for",
        "ab_test_id", "draft_data", "recurrence_rule",
        "created_at", "started_at", "finished_at",
        "total_recipients", "sent", "blocked", "failed",
        "skipped", "cancelled", "avg_rate", "error",
    } <= bcast_cols

    recip_cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcast_recipients)")}
    assert {
        "broadcast_id", "user_id", "status", "attempt_count",
        "last_error", "last_attempt_at", "sent_at",
    } <= recip_cols

    excl_cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcast_exclusions)")}
    assert {"broadcast_id", "user_id"} <= excl_cols

    indexes = {
        r["name"]
        for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_bcast_status", "idx_brec_status", "idx_brec_user"} <= indexes


async def test_migration_v5_applies_after_reconnect(tmp_path) -> None:
    """Upgrading an existing v4 database must apply V5 cleanly."""
    import aiosqlite

    from app.db.migrations import _V1, _V2, _V3, _V4, _split_statements
    from app.db.database import Database as DB

    path = tmp_path / "upgrade.db"
    conn = await aiosqlite.connect(path, isolation_level=None)
    try:
        for script in (_V1, _V2, _V3, _V4):
            for statement in _split_statements(script):
                await conn.execute(statement)
        await conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 't')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (2, 't')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (3, 't')"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (4, 't')"
        )
    finally:
        await conn.close()

    db = DB(path)
    await db.connect()
    try:
        versions = [r["version"] for r in await db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

        cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcasts)")}
        assert "avg_rate" in cols
    finally:
        await db.close()


# ---------------------------------------------------------------- broadcasts


async def test_create_and_get_broadcast(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await repo.create_broadcast(
        db,
        admin_id=1,
        label="Test Broadcast",
        source_chat_id=100,
        source_message_id=200,
        mode="personalized",
        content_html="<b>Hello {first_name}</b>",
    )
    assert bcast_id > 0

    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["admin_id"] == 1
    assert row["label"] == "Test Broadcast"
    assert row["source_chat_id"] == 100
    assert row["source_message_id"] == 200
    assert row["mode"] == "personalized"
    assert row["content_html"] == "<b>Hello {first_name}</b>"
    assert row["status"] == "draft"
    assert row["created_at"] is not None
    assert row["total_recipients"] == 0


async def test_get_broadcast_returns_none_for_missing(db: Database) -> None:
    assert await repo.get_broadcast(db, 999) is None


async def test_set_broadcast_status_updates_counters(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db, mode="copy")

    await repo.set_broadcast_status(
        db, bcast_id, "running",
        total_recipients=100, started_at=repo.now_iso(),
    )
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["status"] == "running"
    assert row["total_recipients"] == 100
    assert row["started_at"] is not None

    await repo.set_broadcast_status(
        db, bcast_id, "completed",
        sent=95, blocked=2, failed=1, skipped=2, cancelled=0,
        finished_at=repo.now_iso(), avg_rate=25.0,
    )
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["sent"] == 95
    assert row["blocked"] == 2
    assert row["failed"] == 1
    assert row["skipped"] == 2
    assert row["cancelled"] == 0
    assert row["finished_at"] is not None
    assert row["avg_rate"] == 25.0


async def test_set_broadcast_status_rejects_unknown_column(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    with pytest.raises(ValueError, match="Unknown"):
        await repo.set_broadcast_status(db, bcast_id, "running", bogus=1)


async def test_list_broadcasts_filters_by_status(db: Database) -> None:
    await add_admin(db, 1)
    d1 = await make_broadcast(db, label="d1")
    d2 = await make_broadcast(db, label="d2")
    d3 = await make_broadcast(db, label="d3")
    await repo.set_broadcast_status(db, d1, "running")
    await repo.set_broadcast_status(db, d2, "scheduled")
    await repo.set_broadcast_status(db, d3, "completed")

    scheduled = await repo.list_broadcasts(db, status="scheduled")
    assert [b["id"] for b in scheduled] == [d2]
    running = await repo.list_broadcasts(db, status="running")
    assert [b["id"] for b in running] == [d1]
    completed = await repo.list_broadcasts(db, status="completed")
    assert [b["id"] for b in completed] == [d3]

    all_bcasts = await repo.list_broadcasts(db)
    assert [b["id"] for b in all_bcasts] == [d3, d2, d1]  # newest first

    limited = await repo.list_broadcasts(db, limit=2)
    assert len(limited) == 2


# ---------------------------------------------------------------- recipients


async def test_insert_recipients_deduplicates(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 3)
    bcast_id = await make_broadcast(db)

    await repo.insert_recipients(db, bcast_id, [1, 2, 3, 2, 1])

    count = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM broadcast_recipients WHERE broadcast_id=?",
        (bcast_id,),
    )
    assert count is not None and count["c"] == 3

    # Calling again must be a no-op
    await repo.insert_recipients(db, bcast_id, [1, 2, 3])
    count = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM broadcast_recipients WHERE broadcast_id=?",
        (bcast_id,),
    )
    assert count is not None and count["c"] == 3


async def test_insert_recipients_empty(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    await repo.insert_recipients(db, bcast_id, [])  # must not raise
    assert await repo.count_recipients(db, bcast_id) == {
        "pending": 0, "sent": 0, "blocked": 0, "failed": 0,
        "skipped": 0, "delivered": 0,
    }


async def test_list_pending_recipients_paging(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 15)
    bcast_id = await make_broadcast(db)
    await repo.insert_recipients(db, bcast_id, list(range(1, 16)))  # 15 recipients

    # Mark some as sent/blocked — they must NOT appear in pending
    await repo.update_recipient_status(db, bcast_id, 3, "sent")
    await repo.update_recipient_status(db, bcast_id, 7, "sent")
    await repo.update_recipient_status(db, bcast_id, 10, "blocked")

    # Pending: 1,2,4,5,6,8,9,11,12,13,14,15 (12 total)
    # First page (limit 5) — ordered by user_id ASC, skipping done
    page1 = await repo.list_pending_recipients(db, bcast_id, 5)
    assert page1 == [1, 2, 4, 5, 6]

    # Full page — all 12 pending, ordered ASC
    all_pending = await repo.list_pending_recipients(db, bcast_id, 100)
    assert all_pending == [1, 2, 4, 5, 6, 8, 9, 11, 12, 13, 14, 15]

    # Small limit pages correctly
    page2 = await repo.list_pending_recipients(db, bcast_id, 1)
    assert page2 == [1]


async def test_update_recipient_status_increments_attempts(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 1)
    bcast_id = await make_broadcast(db)
    await repo.insert_recipients(db, bcast_id, [1])

    # First failed attempt
    await repo.update_recipient_status(
        db, bcast_id, 1, "failed", error="boom", increment_attempts=True
    )
    row = await db.fetch_one(
        "SELECT status, attempt_count, last_error, last_attempt_at "
        "FROM broadcast_recipients WHERE broadcast_id=? AND user_id=?",
        (bcast_id, 1),
    )
    assert row is not None
    assert row["status"] == "failed"
    assert row["attempt_count"] == 1
    assert row["last_error"] == "boom"
    assert row["last_attempt_at"] is not None

    # Second attempt — succeeds this time
    await repo.update_recipient_status(
        db, bcast_id, 1, "sent", increment_attempts=True
    )
    row = await db.fetch_one(
        "SELECT status, attempt_count, last_error "
        "FROM broadcast_recipients WHERE broadcast_id=? AND user_id=?",
        (bcast_id, 1),
    )
    assert row is not None
    assert row["status"] == "sent"
    assert row["attempt_count"] == 2
    assert row["last_error"] == "boom"  # previous error preserved


async def test_update_recipient_status_without_increment(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 1)
    bcast_id = await make_broadcast(db)
    await repo.insert_recipients(db, bcast_id, [1])

    await repo.update_recipient_status(db, bcast_id, 1, "sent")
    row = await db.fetch_one(
        "SELECT status, attempt_count FROM broadcast_recipients "
        "WHERE broadcast_id=? AND user_id=?",
        (bcast_id, 1),
    )
    assert row is not None
    assert row["status"] == "sent"
    assert row["attempt_count"] == 0  # not incremented


async def test_update_recipient_status_not_found_is_noop(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    # No recipients inserted — update should not raise
    await repo.update_recipient_status(db, bcast_id, 999, "sent")


# ---------------------------------------------------------------- exclusions


async def test_create_exclusion_list_and_lookup(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 5)
    bcast_id = await make_broadcast(db)

    await repo.create_exclusion_list(db, bcast_id, [2, 4, 2, 4])  # dedup

    excluded = await repo.list_exclusion_ids(db, bcast_id)
    assert excluded == [2, 4]  # no duplicates, sorted ASC


async def test_create_exclusion_list_empty(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    await repo.create_exclusion_list(db, bcast_id, [])  # must not raise
    assert await repo.list_exclusion_ids(db, bcast_id) == []


# ---------------------------------------------------------------- counts


async def test_count_recipients_aggregates(db: Database) -> None:
    await add_admin(db, 1)
    await add_users(db, 10)
    bcast_id = await make_broadcast(db)
    await repo.insert_recipients(db, bcast_id, list(range(1, 11)))

    await repo.update_recipient_status(db, bcast_id, 1, "sent")
    await repo.update_recipient_status(db, bcast_id, 2, "sent")
    await repo.update_recipient_status(db, bcast_id, 3, "blocked")
    await repo.update_recipient_status(db, bcast_id, 4, "failed")
    await repo.update_recipient_status(db, bcast_id, 5, "skipped")
    await repo.update_recipient_status(db, bcast_id, 6, "delivered")
    # 7, 8, 9, 10 remain 'pending'

    counts = await repo.count_recipients(db, bcast_id)
    assert counts == {
        "pending": 4,
        "sent": 2,
        "blocked": 1,
        "failed": 1,
        "skipped": 1,
        "delivered": 1,
    }


async def test_count_recipients_empty_broadcast(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    counts = await repo.count_recipients(db, bcast_id)
    assert counts == {
        "pending": 0,
        "sent": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "delivered": 0,
    }


async def test_count_recipients_missing_broadcast(db: Database) -> None:
    counts = await repo.count_recipients(db, 999)
    assert counts == {
        "pending": 0,
        "sent": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "delivered": 0,
    }


# ----------------------------------------------------------------- Phase 5/6 migrations + repo


async def test_migration_v7_adds_draft_columns(db: Database) -> None:
    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcasts)")}
    assert "draft_data" in cols
    assert "recurrence_rule" in cols


async def test_migration_v8_adds_ab_test_tables(db: Database) -> None:
    tables = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "ab_tests" in tables
    assert "broadcast_templates" in tables

    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcasts)")}
    assert "ab_test_id" in cols

    tmpl_cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(broadcast_templates)")}
    assert {
        "id", "name", "content_html", "parse_mode", "is_personalized", "created_at",
    } <= tmpl_cols


async def test_create_broadcast_with_scheduled_for(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await repo.create_broadcast(
        db,
        admin_id=1,
        label="future",
        source_chat_id=100,
        source_message_id=200,
        scheduled_for="2030-01-01T00:00:00Z",
    )
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["scheduled_for"] == "2030-01-01T00:00:00Z"
    assert row["status"] == "scheduled"


async def test_create_broadcast_draft_by_default(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db, label="draft")
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["status"] == "draft"
    assert row["scheduled_for"] is None


async def test_list_scheduled_broadcasts_picks_due(db: Database) -> None:
    await add_admin(db, 1)
    past = await repo.create_broadcast(
        db, admin_id=1, label="past", source_chat_id=1, source_message_id=1,
        scheduled_for="2000-01-01T00:00:00Z",
    )
    future = await repo.create_broadcast(
        db, admin_id=1, label="future", source_chat_id=1, source_message_id=1,
        scheduled_for="2099-01-01T00:00:00Z",
    )
    _ = await repo.create_broadcast(
        db, admin_id=1, label="draft", source_chat_id=1, source_message_id=1,
    )

    scheduled = await repo.list_scheduled_broadcasts(db)
    assert [b["id"] for b in scheduled] == [past]


async def test_list_scheduled_broadcasts_empty(db: Database) -> None:
    result = await repo.list_scheduled_broadcasts(db)
    assert result == []


async def test_create_broadcast_with_ab_test_id(db: Database) -> None:
    await add_admin(db, 1)
    await repo.upsert_user(db, 1)
    await db.execute(
        "INSERT INTO ab_tests (name, created_at) VALUES ('test_ab', 't')"
    )
    row = await db.fetch_one("SELECT id FROM ab_tests WHERE name='test_ab'")
    ab_id = row["id"]

    bcast_id = await repo.create_broadcast(
        db, admin_id=1, label="variant", source_chat_id=1,
        source_message_id=1, mode="personalized", content_html="Hi {first_name}",
        ab_test_id=ab_id,
    )
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["ab_test_id"] == ab_id


async def test_set_broadcast_status_updates_scheduled_for(db: Database) -> None:
    await add_admin(db, 1)
    bcast_id = await make_broadcast(db)
    await repo.set_broadcast_status(
        db, bcast_id, "scheduled", scheduled_for="2030-06-01T12:00:00Z",
    )
    row = await repo.get_broadcast(db, bcast_id)
    assert row is not None
    assert row["status"] == "scheduled"
    assert row["scheduled_for"] == "2030-06-01T12:00:00Z"
