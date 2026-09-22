"""Tests for app.core.backup_manager: snapshot, schedule, run, cancel, restore,
retention, sweep, recovery. Real DB + real SQLite backup API, offline only."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Config
from app.core.backup_manager import BackupManager
from app.core.events import EventBus, SystemEvent
from app.db import repositories as repo
from app.db.database import Database


def make_config(**overrides) -> Config:
    kwargs = dict(
        bot_token="123456:TEST", api_id=1, api_hash="0123456789abcdef",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _iso_offset(seconds: int) -> str:
    """ISO-8601 UTC string offset from now by *seconds*."""
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return make_config(data_dir=tmp_path, db_path=str(tmp_path / "bot.db"))


@pytest.fixture
def received_events() -> list[SystemEvent]:
    return []


# ------------------------------------------------------------------- snapshot


async def test_create_backup_run_now_writes_real_db_with_data(
    db: Database, config: Config, bus: EventBus,
) -> None:
    await repo.upsert_user(db, 1, first_name="alice")
    await repo.upsert_user(db, 2, first_name="bob")

    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await mgr.create_backup(label="snapshot", run_now=True, admin_id=1)
    await mgr.wait_done(bid, timeout=10)
    await mgr.shutdown()

    row = await repo.get_backup(db, bid)
    assert row["status"] == "completed"
    assert row["file_path"] is not None
    assert Path(row["file_path"]).exists()
    assert row["size_bytes"] > 0

    # The dump must contain the users table with our data
    dst = sqlite3.connect(row["file_path"])
    try:
        cur = dst.execute("SELECT first_name FROM users ORDER BY id")
        names = [r[0] for r in cur.fetchall()]
    finally:
        dst.close()
    assert names == ["alice", "bob"]


# ------------------------------------------------------------------- sweeper


async def test_sweeper_picks_due_scheduled_and_completes(
    db: Database, config: Config, bus: EventBus,
) -> None:
    await repo.upsert_user(db, 1, first_name="carol")

    mgr = BackupManager(db, config, bus, config.data_dir)
    # Minimum valid sweep interval is 5 (config constraint); sweeper runs once
    # immediately, processes the due backup, then sleeps 5s.
    short = make_config(
        data_dir=config.data_dir, backup_sweep_interval=5,
    )
    mgr._config = short

    due = _iso_offset(-10)
    bid = await repo.create_backup(
        db, label="due", scheduled_for=due, admin_id=1,
    )

    mgr.start_sweeper()
    # Give the sweeper a moment to process the due backup.
    await asyncio.sleep(0.3)
    await mgr.stop_sweeper()
    await mgr.shutdown()

    row = await repo.get_backup(db, bid)
    assert row["status"] == "completed"
    assert row["file_path"] is not None
    assert Path(row["file_path"]).exists()


async def test_sweeper_ignores_future_scheduled(
    db: Database, config: Config, bus: EventBus,
) -> None:
    await repo.upsert_user(db, 1, first_name="dave")

    mgr = BackupManager(db, config, bus, config.data_dir)
    short = make_config(
        data_dir=config.data_dir, backup_sweep_interval=5,
    )
    mgr._config = short

    future = _iso_offset(3600)
    bid = await repo.create_backup(
        db, label="future", scheduled_for=future, admin_id=1,
    )

    mgr.start_sweeper()
    await asyncio.sleep(0.3)
    await mgr.stop_sweeper()
    await mgr.shutdown()

    row = await repo.get_backup(db, bid)
    assert row["status"] == "scheduled"


# ------------------------------------------------------------------- cancel


async def test_cancel_scheduled_backup(db: Database, config: Config, bus: EventBus) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await repo.create_backup(
        db, label="x", scheduled_for=_iso_offset(-1), admin_id=1,
    )
    ok = await mgr.cancel(bid)
    assert ok is True
    row = await repo.get_backup(db, bid)
    assert row["status"] == "cancelled"
    await mgr.shutdown()


async def test_cancel_completed_returns_false(
    db: Database, config: Config, bus: EventBus, tmp_path: Path,
) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await mgr.create_backup(label="x", run_now=True, admin_id=1)
    await mgr.wait_done(bid, timeout=10)
    ok = await mgr.cancel(bid)
    assert ok is False
    await mgr.shutdown()


# ------------------------------------------------------------------- restore


async def test_restore_overwrites_live_db_and_sees_restored_data(
    db: Database, config: Config, bus: EventBus, tmp_path: Path,
) -> None:
    """Snapshot A (carol), add carol to live, snapshot B — then restore A
    and confirm carol is gone from the live DB."""
    mgr = BackupManager(db, config, bus, config.data_dir)

    # Snapshot A with one user
    await repo.upsert_user(db, 1, first_name="alice")
    bid_a = await mgr.create_backup(label="A", run_now=True, admin_id=1)
    await mgr.wait_done(bid_a, timeout=10)

    # Mutate the live DB (add carol, delete alice)
    await repo.upsert_user(db, 2, first_name="carol")
    await db.execute("DELETE FROM users WHERE id=1")

    # Restore A over the live DB
    result = await mgr.restore(bid_a, admin_id=1)
    assert result == "restored"

    # Live aiosqlite conn should now see snapshot A's state (alice, no carol)
    cur = await db.fetch_one("SELECT first_name FROM users ORDER BY id")
    assert cur is not None and cur["first_name"] == "alice"
    carol = await db.fetch_one("SELECT id FROM users WHERE first_name='carol'")
    assert carol is None

    await mgr.shutdown()


async def test_restore_not_completed_returns_not_completed(
    db: Database, config: Config, bus: EventBus,
) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await repo.create_backup(db, label="p", admin_id=1)
    result = await mgr.restore(bid, admin_id=1)
    assert result == "not_completed"
    await mgr.shutdown()


async def test_restore_missing_returns_not_found(
    db: Database, config: Config, bus: EventBus,
) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    result = await mgr.restore(999, admin_id=1)
    assert result == "not_found"
    await mgr.shutdown()


# ------------------------------------------------------------------- retention


async def test_retention_trims_oldest_beyond_keep_last(
    db: Database, config: Config, bus: EventBus,
) -> None:
    limited = make_config(
        data_dir=config.data_dir, backup_keep_last=3,
    )
    mgr = BackupManager(db, config, bus, config.data_dir)
    mgr._config = limited

    await repo.upsert_user(db, 1, first_name="z")
    bids = []
    for i in range(5):
        bid = await mgr.create_backup(label=f"snap{i}", run_now=True, admin_id=1)
        bids.append(bid)
        await mgr.wait_done(bid, timeout=10)
        await asyncio.sleep(0.05)  # stagger created_at

    # After completion + retention, only 3 should remain
    all_backups = await repo.list_backups(db)
    assert len(all_backups) == 3
    completed = await repo.count_backups(db, status="completed")
    assert completed == 3
    await mgr.shutdown()


# ------------------------------------------------------------------- recover


async def test_recover_marks_abandoned_running_as_failed(
    db: Database, config: Config, bus: EventBus,
) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await repo.create_backup(db, label="abandoned", admin_id=1)
    await repo.set_backup_status(
        db, bid, "running", started_at=repo.now_iso(),
    )

    n = await mgr.recover()
    assert n == 1

    row = await repo.get_backup(db, bid)
    assert row["status"] == "failed"
    assert row["error"] is not None
    await mgr.shutdown()


# ------------------------------------------------------------------- events


async def test_backup_completion_publishes_system_event(
    db: Database, config: Config, bus: EventBus, received_events: list,
) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)

    async def collect(event: SystemEvent) -> None:
        received_events.append(event)

    bus.subscribe(collect)

    bid = await mgr.create_backup(label="evt", run_now=True, admin_id=1)
    await mgr.wait_done(bid, timeout=10)
    await mgr.shutdown()

    types = [e.event_type for e in received_events]
    assert "backup_started" in types
    assert "backup_completed" in types


# ------------------------------------------------------------------- idempotency


async def test_run_backup_is_idempotent_on_completed(db: Database, config, bus) -> None:
    mgr = BackupManager(db, config, bus, config.data_dir)
    bid = await mgr.create_backup(label="idem", run_now=True, admin_id=1)
    await mgr.wait_done(bid, timeout=10)

    # Running again on a completed backup is a no-op (no new file, no status change)
    path_before = (await repo.get_backup(db, bid))["file_path"]
    await mgr.run_backup(bid)
    path_after = (await repo.get_backup(db, bid))["file_path"]
    assert path_before == path_after
    await mgr.shutdown()
