"""Tests for app.db.database: connection, migrations, transactions, FKs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from aiosqlite import Row

from app.db.database import Database
from app.db.migrations import _V1, _split_statements


async def test_connect_applies_migrations(db: Database) -> None:
    rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    tables = {
        r["name"]
        for r in await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"users", "accounts", "jobs", "audit_log", "channels", "schema_migrations"} <= tables


async def test_migration_v4_adds_user_name_columns(db: Database) -> None:
    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(users)")}
    assert {"first_name", "last_name", "username"}.issubset(cols)


async def test_reconnect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "again.db"
    db = Database(path)
    await db.connect()
    await db.close()

    db2 = Database(path)
    await db2.connect()
    try:
        rows = await db2.fetch_all("SELECT version FROM schema_migrations")
        assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    finally:
        await db2.close()


async def test_row_factory(db_row_factory_check: Database) -> None:
    row = await db_row_factory_check.fetch_one("SELECT 1 AS one")
    assert row is not None
    assert row["one"] == 1
    assert isinstance(row, Row)


async def test_tx_commits_on_success(db: Database) -> None:
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
            (1, "t", "t"),
        )
    row = await db.fetch_one("SELECT id FROM users WHERE id=1")
    assert row is not None and row["id"] == 1


async def test_tx_rolls_back_on_exception(db: Database) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
                (1, "t", "t"),
            )
            raise RuntimeError("boom")
    assert await db.fetch_one("SELECT id FROM users WHERE id=1") is None


async def test_foreign_keys_enforced(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO jobs (owner_id, account_id, source_ref, dest_ref, "
            "source_title, dest_title, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'created', ?)",
            (999, 999, "src", "dst", "", "", "t"),
        )


async def test_migration_v2_jobs_account_id_nullable(db: Database) -> None:
    await db.execute("INSERT INTO users (id, created_at, updated_at) VALUES (1,'t','t')")
    await db.execute(
        "INSERT INTO jobs (owner_id, account_id, source_ref, dest_ref, "
        "status, created_at) VALUES (1, NULL, '@s', '@d', 'created', 't')"
    )
    row = await db.fetch_one("SELECT account_id FROM jobs")
    assert row is not None and row["account_id"] is None


async def test_migration_v2_account_delete_sets_job_link_null(db: Database) -> None:
    """The v2 FK action needs PRAGMA foreign_keys=ON (set by Database.connect):
    deleting an account nulls the job links instead of refusing or deleting
    the history (PRD §20)."""
    await db.execute("INSERT INTO users (id, created_at, updated_at) VALUES (1,'t','t')")
    await db.execute(
        "INSERT INTO accounts (id, owner_id, phone, tg_user_id, display_name, "
        "session_encrypted, status, added_at) "
        "VALUES (5, 1, '+1555', 77, 'TG', 'enc', 'active', 't')"
    )
    await db.execute(
        "INSERT INTO jobs (owner_id, account_id, source_ref, dest_ref, "
        "status, created_at) VALUES (1, 5, '@s', '@d', 'completed', 't')"
    )
    await db.execute("DELETE FROM accounts WHERE id=5")

    job = await db.fetch_one("SELECT account_id, status FROM jobs")
    assert job is not None
    assert job["account_id"] is None  # link nulled by the FK action
    assert job["status"] == "completed"  # history survives
    # indexes were recreated on the rebuilt table
    indexes = {
        r["name"]
        for r in await db.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {"idx_jobs_owner_status", "idx_jobs_account_status"} <= indexes


async def test_v1_database_upgrades_to_v2_preserving_history(tmp_path: Path) -> None:
    """Upgrade path: an existing v1 database keeps its data through the v2
    rebuild (real deployments hold v1 rows)."""
    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(path, isolation_level=None)
    try:
        for statement in _split_statements(_V1):
            await conn.execute(statement)
        # schema_migrations is created by the migration runner, not by _V1
        await conn.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 't')"
        )
        await conn.execute("INSERT INTO users (id, created_at, updated_at) VALUES (1, 't', 't')")
        await conn.execute(
            "INSERT INTO accounts (id, owner_id, phone, tg_user_id, display_name, "
            "session_encrypted, status, added_at) "
            "VALUES (5, 1, '+1555', 77, 'TG', 'enc', 'active', 't')"
        )
        await conn.execute(
            "INSERT INTO jobs (id, owner_id, account_id, source_ref, dest_ref, "
            "status, invited, created_at) VALUES (9, 1, 5, '@s', '@d', 'completed', 3, 't')"
        )
    finally:
        await conn.close()

    db = Database(path)
    await db.connect()
    try:
        rows = await db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        job = await db.fetch_one(
            "SELECT account_id, status, invited FROM jobs WHERE id=9"
        )
        assert job is not None
        assert (job["account_id"], job["status"], job["invited"]) == (5, "completed", 3)
        # and the rebuilt FK nulls the link when the account goes away
        await db.execute("DELETE FROM accounts WHERE id=5")
        job = await db.fetch_one("SELECT account_id FROM jobs WHERE id=9")
        assert job is not None and job["account_id"] is None
    finally:
        await db.close()


# ---------------------------------------------------------------- v9: mandatory subscription


async def test_migration_v9_adds_gate_columns(db: Database) -> None:
    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(users)")}
    assert "gate_cleared" in cols
    cols = {c["name"] for c in await db.fetch_all("PRAGMA table_info(channels)")}
    assert "type" in cols


async def test_v9_gate_cleared_defaults_to_zero(db: Database) -> None:
    await db.execute(
        "INSERT INTO users (id, created_at, updated_at) VALUES (?, ?, ?)",
        (1, "t", "t"),
    )
    row = await db.fetch_one("SELECT gate_cleared FROM users WHERE id=1")
    assert row is not None and row["gate_cleared"] == 0


async def test_v9_channel_type_defaults_to_channel(db: Database) -> None:
    await db.execute(
        "INSERT INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?)",
        (-1001, "Test", "https://t.me/test"),
    )
    row = await db.fetch_one("SELECT type FROM channels WHERE channel_id=-1001")
    assert row is not None and row["type"] == "channel"


async def test_reset_user_gates_clears_all(db: Database) -> None:
    from app.db import repositories as repo
    for uid in (1, 2, 3):
        await db.execute(
            "INSERT INTO users (id, created_at, updated_at, gate_cleared) VALUES (?, ?, ?, 1)",
            (uid, "t", "t"),
        )
    assert await repo.is_gate_cleared(db, 1)
    assert await repo.is_gate_cleared(db, 2)
    await repo.reset_user_gates(db)
    assert not await repo.is_gate_cleared(db, 1)
    assert not await repo.is_gate_cleared(db, 2)


async def test_list_channels_by_type_and_count(db: Database) -> None:
    from app.db import repositories as repo
    await repo.add_channel(db, channel_id=-1001, title="ChA", invite_link="l1", entry_type="channel")
    await repo.add_channel(db, channel_id=-1002, title="ChB", invite_link="l2", entry_type="channel")
    await repo.add_channel(db, channel_id=-1003, title="GrA", invite_link="l3", entry_type="group")
    channels = await repo.list_channels_by_type(db, "channel")
    assert len(channels) == 2
    assert all(c["type"] == "channel" for c in channels)
    groups = await repo.list_channels_by_type(db, "group")
    assert len(groups) == 1
    assert groups[0]["type"] == "group"
    assert await repo.count_channels_by_type(db, "channel") == 2
    assert await repo.count_channels_by_type(db, "group") == 1
    # toggle a channel inactive — count should drop
    await repo.toggle_channel(db, channels[0]["id"])
    assert await repo.count_channels_by_type(db, "channel") == 1
