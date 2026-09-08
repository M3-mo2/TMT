"""Tests for Phase 6: A/B testing."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Config
from app.core.broadcast import Broadcaster
from app.core.broadcast_models import BroadcastStatus
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
        bcast_sweep_interval=30,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


# ---------------------------------------------------------------- create_ab_test


async def test_create_ab_test_creates_rows(db: Database) -> None:
    """create_ab_test inserts one ab_tests row + one Broadcast per variant."""
    config = _config()
    await repo.upsert_user(db, config.admin_id_list[0])

    bc = Broadcaster(db, config, EventBus())
    ab_test_id = await bc.create_ab_test(
        name="welcome_ab",
        splits=[
            ("copy", None),
            ("personalized", "Hi {first_name}!"),
        ],
        admin_id=config.admin_id_list[0],
        source_chat_id=999,
        source_message_id=42,
    )

    assert ab_test_id > 0

    ab_rows = await db.fetch_all("SELECT * FROM ab_tests WHERE id=?", (ab_test_id,))
    assert len(ab_rows) == 1
    assert ab_rows[0]["name"] == "welcome_ab"

    bcast_rows = await db.fetch_all(
        "SELECT * FROM broadcasts WHERE ab_test_id=?", (ab_test_id,)
    )
    assert len(bcast_rows) == 2
    for b in bcast_rows:
        assert b["ab_test_id"] == ab_test_id
        assert b["status"] == BroadcastStatus.DRAFT.value
        assert b["source_chat_id"] == 999
        assert b["source_message_id"] == 42

    modes = {b["mode"] for b in bcast_rows}
    assert modes == {"copy", "personalized"}

    events = await db.fetch_all("SELECT event FROM audit_log WHERE event='ab_test_created'")
    assert len(events) == 1
    await bc.shutdown()


async def test_create_ab_test_returns_id(db: Database) -> None:
    config = _config()
    await repo.upsert_user(db, config.admin_id_list[0])
    bc = Broadcaster(db, config, EventBus())

    ab_test_id = await bc.create_ab_test(
        name="split_test",
        splits=[("copy", None), ("personalized", "Variant B")],
        admin_id=config.admin_id_list[0],
        source_chat_id=1,
        source_message_id=1,
    )
    assert ab_test_id > 0
    await bc.shutdown()


# ---------------------------------------------------------------- split algorithm


async def test_ab_test_split_distributes_users(db: Database) -> None:
    """_ab_test_split assigns each user to exactly one variant deterministically."""
    config = _config()
    bc = Broadcaster(db, config, EventBus())

    num_variants = 4
    users = range(1, 101)
    assignments = {bc._ab_test_split(uid, num_variants) for uid in users}
    assert assignments == set(range(num_variants))

    for uid in users:
        assert bc._ab_test_split(uid, num_variants) == bc._ab_test_split(uid, num_variants)
    await bc.shutdown()


async def test_ab_test_split_single_variant(db: Database) -> None:
    """With 1 variant, all users go to variant 0."""
    config = _config()
    bc = Broadcaster(db, config, EventBus())
    for uid in range(1, 50):
        assert bc._ab_test_split(uid, 1) == 0
    await bc.shutdown()


async def test_ab_test_split_zero_variants(db: Database) -> None:
    """Edge case: 0 variants returns 0."""
    config = _config()
    bc = Broadcaster(db, config, EventBus())
    assert bc._ab_test_split(42, 0) == 0
    await bc.shutdown()
