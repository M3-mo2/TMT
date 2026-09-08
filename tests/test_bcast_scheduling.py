"""Tests for Phase 5: sweeper + enhanced recovery."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.config import Config
from app.core.broadcast import Broadcaster
from app.core.broadcast_models import AudienceFilter, BroadcastStatus
from app.core.events import EventBus
from app.db import repositories as repo
from app.db.database import Database


def _config(**overrides: Any) -> Config:
    kwargs: dict[str, Any] = dict(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1001",
        max_bcast_concurrency=3,
        bcast_max_rate_per_second=1000,
        bcast_flood_retry_threshold=60,
        bcast_retry_attempts=3,
        bcast_retry_backoff_base=0.01,
        bcast_edit_interval=0.1,
        bcast_batch_size=50,
        bcast_sweep_interval=5,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


async def add_user(db: Database, user_id: int) -> None:
    await repo.upsert_user(db, user_id)


async def setup_scheduled(
    db: Database, config: Config, *, scheduled_for: str, mode: str = "copy",
    content_html: str | None = None, user_ids: list[int] | None = None,
) -> int:
    await repo.upsert_user(db, config.admin_id_list[0])
    for uid in user_ids or []:
        await add_user(db, uid)
    cid = await repo.create_broadcast(
        db,
        admin_id=config.admin_id_list[0],
        label="scheduled-test",
        source_chat_id=999,
        source_message_id=42,
        mode=mode,
        content_html=content_html,
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
        scheduled_for=scheduled_for,
    )
    if user_ids:
        await repo.insert_recipients(db, cid, user_ids)
    return cid


# ---------------------------------------------------------------- sweeper


async def test_sweeper_promotes_scheduled(db: Database, bot_factory) -> None:
    """A campaign with scheduled_for in the past is promoted to running."""
    config = _config(bcast_sweep_interval=5)
    bot = bot_factory(call_delay=0.5)  # slow enough to stay "running"
    cid = await setup_scheduled(
        db, config, scheduled_for="2000-01-01T00:00:00Z",
        user_ids=[1, 2],
    )

    bc = Broadcaster(db, config, EventBus())
    bc.start_sweeper(bot)
    try:
        await asyncio.sleep(0.3)

        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == BroadcastStatus.RUNNING.value

        if bc._tasks.get(cid):
            await asyncio.wait_for(bc._tasks[cid], timeout=10)
        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == BroadcastStatus.COMPLETED.value
        assert campaign["sent"] == 2
    finally:
        await bc.stop_sweeper()
        await bc.shutdown()


async def test_sweeper_skips_future_scheduled(db: Database, bot_factory) -> None:
    """A campaign scheduled in the future is NOT promoted."""
    config = _config(bcast_sweep_interval=5)
    bot = bot_factory()
    cid = await setup_scheduled(
        db, config, scheduled_for="2099-01-01T00:00:00Z",
    )

    bc = Broadcaster(db, config, EventBus())
    bc.start_sweeper(bot)
    try:
        await asyncio.sleep(0.3)
        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == "scheduled"
        assert cid not in bc._tasks
    finally:
        await bc.stop_sweeper()
        await bc.shutdown()


async def test_stop_sweeper_cancels_task(db: Database, bot_factory) -> None:
    """stop_sweeper cancels and clears the sweep task."""
    config = _config(bcast_sweep_interval=3600)
    bot = bot_factory()
    bc = Broadcaster(db, config, EventBus())
    bc.start_sweeper(bot)
    assert bc._sweep_task is not None
    assert not bc._sweep_task.done()

    await bc.stop_sweeper()
    assert bc._sweep_task is None

    await bc.stop_sweeper()


# ---------------------------------------------------------------- enhanced recovery


async def test_recover_marks_no_pending_as_failed(db: Database, bot_factory) -> None:
    """recover() marks a running campaign with no pending recipients as failed."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1)

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])
    await repo.set_broadcast_status(
        db, cid, "running",
        total_recipients=1, sent=1, finished_at=None,
    )
    await repo.update_recipient_status(db, cid, 1, "sent")

    bc = Broadcaster(db, config, EventBus())
    resumed = await bc.recover(bot)
    assert cid not in resumed

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == BroadcastStatus.FAILED.value
    assert campaign["error"] == "no pending recipients"
    await bc.shutdown()


async def test_recover_audits_events(db: Database, bot_factory) -> None:
    """recover() writes audit_log entries."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1)

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])
    await repo.set_broadcast_status(
        db, cid, "running",
        total_recipients=1, sent=1, finished_at=None,
    )
    await repo.update_recipient_status(db, cid, 1, "sent")

    bc = Broadcaster(db, config, EventBus())
    await bc.recover(bot)

    events = await db.fetch_all("SELECT event FROM audit_log ORDER BY ts")
    event_names = [r["event"] for r in events]
    assert "broadcast_recovered_failed" in event_names
    await bc.shutdown()


async def test_recover_resumes_incomplete(db: Database, bot_factory) -> None:
    """recover() resumes a campaign with pending recipients."""
    config = _config(bcast_retry_backoff_base=0.01)
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    for uid in [1, 2, 3]:
        await repo.upsert_user(db, uid)

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1, 2, 3])
    await repo.set_broadcast_status(
        db, cid, "running",
        total_recipients=3, sent=1,
    )
    await repo.update_recipient_status(db, cid, 1, "sent")

    bc = Broadcaster(db, config, EventBus())
    resumed = await bc.recover(bot)
    assert cid in resumed

    task = bc._tasks.get(cid)
    if task:
        await asyncio.wait_for(task, timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["sent"] == 3
    assert campaign["status"] == BroadcastStatus.COMPLETED.value
    await bc.shutdown()


async def test_cancel_persists_status_immediately(db: Database, bot_factory) -> None:
    """cancel() writes status='cancelled' to DB immediately, not just the event."""
    config = _config()
    bot = bot_factory(call_delay=0.5)  # slow enough to still be running
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1)
    await repo.upsert_user(db, 2)

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1, 2])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    await asyncio.sleep(0.2)
    await bc.cancel(cid, bot)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == BroadcastStatus.CANCELLED.value

    events = await db.fetch_all("SELECT event FROM audit_log ORDER BY ts")
    event_names = [r["event"] for r in events]
    assert "broadcast_cancelled" in event_names

    await bc.shutdown()
