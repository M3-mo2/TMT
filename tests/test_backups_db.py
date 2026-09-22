"""Tests for the V11 backup schema + backup repository functions."""

from __future__ import annotations

import pytest

from app.db import repositories as repo
from app.db.database import Database


async def add_admin(db: Database, admin_id: int = 1) -> None:
    await repo.upsert_user(db, admin_id)


# -------------------------------------------------------------------- migration


async def test_migration_v11_creates_backup_tables(db: Database) -> None:
    tables = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "backups" in tables

    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(backups)")}
    assert {
        "id", "label", "kind", "scheduled_for", "status", "file_path",
        "size_bytes", "error", "created_by", "created_at",
        "started_at", "finished_at",
    } <= cols

    indexes = {
        r["name"]
        for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_backups_status" in indexes
    assert "idx_backups_scheduled" in indexes


# ------------------------------------------------------------------- create/read


async def test_create_backup_pending_by_default(db: Database) -> None:
    await add_admin(db, 1)
    bid = await repo.create_backup(db, label="test", kind="manual", admin_id=1)
    assert bid > 0
    row = await repo.get_backup(db, bid)
    assert row is not None
    assert row["label"] == "test"
    assert row["kind"] == "manual"
    assert row["status"] == "pending"
    assert row["created_by"] == 1
    assert row["created_at"] is not None
    assert row["scheduled_for"] is None


async def test_create_backup_scheduled_when_scheduled_for_set(db: Database) -> None:
    await add_admin(db, 1)
    bid = await repo.create_backup(
        db, label="future", kind="scheduled",
        scheduled_for="2030-01-01T00:00:00Z", admin_id=1,
    )
    row = await repo.get_backup(db, bid)
    assert row is not None
    assert row["status"] == "scheduled"
    assert row["scheduled_for"] == "2030-01-01T00:00:00Z"


async def test_get_backup_missing_returns_none(db: Database) -> None:
    assert await repo.get_backup(db, 999) is None


# --------------------------------------------------------------- list / filter


async def test_list_backups_newest_first(db: Database) -> None:
    await add_admin(db, 1)
    b1 = await repo.create_backup(db, label="old", admin_id=1)
    b2 = await repo.create_backup(db, label="new", admin_id=1)
    b3 = await repo.create_backup(db, label="newer", admin_id=1)
    result = await repo.list_backups(db)
    assert [b["id"] for b in result] == [b3, b2, b1]


async def test_list_backups_filter_by_status(db: Database) -> None:
    await add_admin(db, 1)
    bid_1 = await repo.create_backup(db, label="p", admin_id=1)
    bid_2 = await repo.create_backup(db, label="c", admin_id=1)
    await repo.set_backup_status(db, bid_1, "completed")

    result = await repo.list_backups(db, status="completed")
    assert [b["id"] for b in result] == [bid_1]

    pending = await repo.list_backups(db, status="pending")
    assert [b["id"] for b in pending] == [bid_2]


# --------------------------------------------------- due scheduled (sweeper query)


async def test_list_due_scheduled_picks_past_only(db: Database) -> None:
    await add_admin(db, 1)
    past = await repo.create_backup(
        db, label="due", scheduled_for="2000-01-01T00:00:00Z", admin_id=1,
    )
    future = await repo.create_backup(
        db, label="later", scheduled_for="2099-01-01T00:00:00Z", admin_id=1,
    )
    _ = await repo.create_backup(db, label="draft", admin_id=1)

    due = await repo.list_due_scheduled_backups(db)
    assert [b["id"] for b in due] == [past]


async def test_list_due_scheduled_empty(db: Database) -> None:
    assert await repo.list_due_scheduled_backups(db) == []


# --------------------------------------------------------- set_backup_status


async def test_set_backup_status_rejects_unknown_status(db: Database) -> None:
    await add_admin(db, 1)
    bid = await repo.create_backup(db, label="x", admin_id=1)
    with pytest.raises(ValueError, match="Unknown backup status"):
        await repo.set_backup_status(db, bid, "bogus")


async def test_set_backup_status_rejects_unknown_column(db: Database) -> None:
    await add_admin(db, 1)
    bid = await repo.create_backup(db, label="x", admin_id=1)
    with pytest.raises(ValueError, match="Unknown backup column"):
        await repo.set_backup_status(db, bid, "running", bogus=1)


async def test_set_backup_status_updates_counters(db: Database) -> None:
    await add_admin(db, 1)
    bid = await repo.create_backup(db, label="x", admin_id=1)
    await repo.set_backup_status(
        db, bid, "completed",
        file_path="/tmp/snapshot.db", size_bytes=4096,
        finished_at=repo.now_iso(),
    )
    row = await repo.get_backup(db, bid)
    assert row is not None
    assert row["status"] == "completed"
    assert row["file_path"] == "/tmp/snapshot.db"
    assert row["size_bytes"] == 4096
    assert row["finished_at"] is not None


async def test_count_backups(db: Database) -> None:
    await add_admin(db, 1)
    await repo.create_backup(db, label="a", admin_id=1)
    await repo.create_backup(db, label="b", admin_id=1)
    assert await repo.count_backups(db) == 2
    assert await repo.count_backups(db, status="pending") == 2
    assert await repo.count_backups(db, status="completed") == 0
