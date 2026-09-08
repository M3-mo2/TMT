"""Tests for Phase 6: personalization template rendering."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.config import Config
from app.core.broadcast import Broadcaster, _safe_format
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
        bcast_sweep_interval=30,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


# ---------------------------------------------------------------- _SafeFormatDict


def test_safe_format_missing_key_returns_empty() -> None:
    assert _safe_format("{a}{missing}", a="X") == "X"


def test_safe_format_with_all_keys() -> None:
    result = _safe_format("{first_name} {last_name}", first_name="Alice", last_name="Smith")
    assert result == "Alice Smith"


# ---------------------------------------------------------------- template rendering


async def test_personalized_mode_renders_template(db: Database, bot_factory) -> None:
    """Personalized mode renders {first_name} with actual user data."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1, first_name="Alice", last_name="Smith", username="alice")

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="personalized", content_html="Hello {first_name}!",
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    assert len([c for c in bot.send_calls if c["chat_id"] == 1]) == 1
    assert bot.send_calls[-1]["text"] == "Hello Alice!"
    await bc.shutdown()


async def test_personalized_mode_renders_multiple_variables(db: Database, bot_factory) -> None:
    """Template with multiple variables renders all correctly."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 42, first_name="Bob", last_name="Jones", username="bob")
    await db.execute(
        "INSERT INTO accounts (id, owner_id, phone, tg_user_id, display_name, "
        "session_encrypted, status, added_at) "
        "VALUES (1, 42, '+1555', 888, 'TG', 'enc', 'active', 't')"
    )

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="personalized",
        content_html="Hi {first_name} {last_name}! You have {accounts_count} account(s) and {jobs_count} jobs.",
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [42])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    assert len([c for c in bot.send_calls if c["chat_id"] == 42]) == 1
    text = bot.send_calls[-1]["text"]
    assert text == "Hi Bob Jones! You have 1 account(s) and 0 jobs."
    await bc.shutdown()


async def test_personalized_mode_escapes_user_values(db: Database, bot_factory) -> None:
    """User-supplied name values are HTML-escaped in the rendered template."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1, first_name="<script>alert(1)</script>")

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="personalized", content_html="<b>{first_name}</b>",
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    assert len([c for c in bot.send_calls if c["chat_id"] == 1]) == 1
    text = bot.send_calls[-1]["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "<b>" in text
    await bc.shutdown()


async def test_personalized_mode_missing_variable_is_empty(db: Database, bot_factory) -> None:
    """A template placeholder with no matching context variable renders as empty."""
    config = _config()
    bot = bot_factory()
    await repo.upsert_user(db, config.admin_id_list[0])
    await repo.upsert_user(db, 1, first_name="Alice")

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="personalized",
        content_html="Hi {first_name}! Your {nonexistent} field is here.",
        filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    assert len([c for c in bot.send_calls if c["chat_id"] == 1]) == 1
    text = bot.send_calls[-1]["text"]
    assert text == "Hi Alice! Your  field is here."
    await bc.shutdown()


async def test_user_context_returns_expected_fields(db: Database) -> None:
    """_user_context returns the correct dict for personalization."""
    config = _config()
    await repo.upsert_user(db, 1, first_name="Carol", last_name="Doe", username="carol")
    await db.execute(
        "INSERT INTO accounts (id, owner_id, phone, tg_user_id, display_name, "
        "session_encrypted, status, added_at) "
        "VALUES (1, 1, '+1555', 888, 'TG', 'enc', 'active', 't')"
    )

    bc = Broadcaster(db, config, EventBus())
    ctx = await bc._user_context(1)
    assert ctx == {
        "first_name": "Carol",
        "last_name": "Doe",
        "username": "carol",
        "accounts_count": 1,
        "jobs_count": 0,
    }
