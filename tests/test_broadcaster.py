"""Tests for app.core.broadcast: Broadcaster, classify_error, _progress_text.

Offline — uses a FakeBroadcastBot (no network) and a real Database (tmp_path).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import CopyMessage, SendMessage

from app.config import Config
from app.core.broadcast import (
    Broadcaster,
    _progress_text,
    classify_error,
    count_audience,
    resolve_audience,
)
from app.core.broadcast_models import (
    AudienceFilter,
    BroadcastStatus,
    ErrorKind,
    RecipientStatus,
)
from app.core.events import EventBus
from app.db import repositories as repo
from app.db.database import Database


# ---------------------------------------------------------------- helpers


def _config(**overrides: Any) -> Config:
    """Build a Config with broadcast-friendly defaults for tests."""
    kwargs: dict[str, Any] = dict(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1001",
        max_bcast_concurrency=3,
        bcast_max_rate_per_second=1000,  # effectively unlimited for tests
        bcast_flood_retry_threshold=60,
        bcast_retry_attempts=3,
        bcast_retry_backoff_base=0.01,  # fast backoff for tests
        bcast_edit_interval=0.1,
        bcast_batch_size=50,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


class FakeBroadcastBot:
    """Scriptable bot double for broadcast tests."""

    def __init__(
        self,
        *,
        fail_for: dict[int, BaseException] | None = None,
        delay: float = 0.0,
        call_delay: float = 0.0,
    ) -> None:
        self.copy_calls: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []
        self.fail_for: dict[int, BaseException] = fail_for or {}
        self.delay = delay
        self.call_delay = call_delay
        self._next_msg_id = 1000

    async def copy_message(
        self, chat_id: int, from_chat_id: int, message_id: int, **kwargs: Any,
    ) -> Any:
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        if chat_id in self.fail_for:
            exc = self.fail_for[chat_id]
            if isinstance(exc, TelegramRetryAfter):
                raise exc
            raise exc
        self.copy_calls.append({
            "chat_id": chat_id, "from_chat_id": from_chat_id,
            "message_id": message_id,
        })
        self._next_msg_id += 1
        return SimpleNamespace(message_id=self._next_msg_id)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.send_calls.append({"chat_id": chat_id, "text": text})
        self._next_msg_id += 1
        return SimpleNamespace(message_id=self._next_msg_id)

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any,
    ) -> Any:
        self.edit_calls.append({
            "chat_id": chat_id, "message_id": message_id, "text": text,
        })

    async def send_message_with_inline_keyboard(self, *args: Any, **kwargs: Any) -> Any:
        pass


@pytest.fixture
def bot_factory():
    """Returns a callable that creates a FakeBroadcastBot."""
    bots: list[FakeBroadcastBot] = []

    def _make(**kwargs: Any) -> FakeBroadcastBot:
        b = FakeBroadcastBot(**kwargs)
        bots.append(b)
        return b

    return _make


@pytest.fixture
def config() -> Config:
    return _config()


async def add_user(db: Database, user_id: int, *, is_blocked: int = 0,
                   updated_at: str = "2026-09-07T00:00:00Z") -> None:
    await repo.upsert_user(db, user_id)
    if is_blocked:
        await db.execute("UPDATE users SET is_blocked=1 WHERE id=?", (user_id,))
    if updated_at:
        await db.execute("UPDATE users SET updated_at=? WHERE id=?", (updated_at, user_id))


async def setup_campaign(db: Database, config: Config, *, users: list[int],
                         **bcast_kwargs: Any) -> int:
    """Insert users + admin, create a broadcast campaign, insert recipients."""
    await repo.upsert_user(db, config.admin_id_list[0])  # ensure admin exists
    for uid in users:
        await add_user(db, uid)
    defaults: dict[str, Any] = dict(
        admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    defaults.update(bcast_kwargs)
    cid = await repo.create_broadcast(db, **defaults)
    await repo.insert_recipients(db, cid, users)
    return cid


# ---------------------------------------------------------------- classify_error


def test_classify_error_forbidden_blocked() -> None:
    exc = TelegramForbiddenError(
        CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
        "Forbidden: bot was blocked by the user",
    )
    assert classify_error(exc) is ErrorKind.PERMANENT_BLOCKED


def test_classify_error_forbidden_deactivated() -> None:
    exc = TelegramForbiddenError(
        CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
        "Forbidden: user is deactivated",
    )
    assert classify_error(exc) is ErrorKind.PERMANENT_BLOCKED


def test_classify_error_forbidden_chat_not_found() -> None:
    exc = TelegramForbiddenError(
        CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
        "Forbidden: chat not found",
    )
    assert classify_error(exc) is ErrorKind.PERMANENT_BLOCKED


def test_classify_error_forbidden_other() -> None:
    exc = TelegramForbiddenError(
        CopyMessage(chat_id=1, from_chat_id=2, message_id=3),
        "Forbidden: can't access",
    )
    assert classify_error(exc) is ErrorKind.PERMANENT_FAIL


def test_classify_error_retry_after_small() -> None:
    exc = TelegramRetryAfter(
        SendMessage(chat_id=1, text="x"), "Too Many Requests", retry_after=5,
    )
    assert classify_error(exc) is ErrorKind.RETRY_FLOOD


def test_classify_error_retry_after_large() -> None:
    exc = TelegramRetryAfter(
        SendMessage(chat_id=1, text="x"), "Too Many Requests", retry_after=120,
    )
    assert classify_error(exc) is ErrorKind.RETRY_DELAYED


def test_classify_error_server_error() -> None:
    exc = TelegramServerError(
        SendMessage(chat_id=1, text="x"), "Internal Server Error",
    )
    assert classify_error(exc) is ErrorKind.RETRY_TRANSIENT


def test_classify_error_generic_api_error() -> None:
    exc = TelegramAPIError(
        SendMessage(chat_id=1, text="x"), "Bad Request: chat not found",
    )
    assert classify_error(exc) is ErrorKind.PERMANENT_FAIL


def test_classify_error_plain_exception() -> None:
    exc = RuntimeError("something broke")
    assert classify_error(exc) is ErrorKind.PERMANENT_FAIL


def test_classify_error_does_not_swallow_cancelled() -> None:
    exc = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        classify_error(exc)


# ---------------------------------------------------------------- _progress_text


def test_progress_text_uses_approved_symbols_only() -> None:
    text = _progress_text(sent=5, blocked=1, failed=2, skipped=0, total=10)
    # Must not contain any emoji outside the approved set in test_texts.py
    # Approved: 👤🔰💳✅✨📚🔐🏷 and symbols ✓ !×›=•#—…|←↓⇐―⇜⤸𑗁📱≡⤸⋆⟡↢
    # This text should ONLY use ⟡, ×, !, ›, ✓ (no media/user emoji)
    assert "⟡" in text
    assert "✅" not in text  # progress text should not reuse ✅ (kept for card-only)
    assert "<code>5</code>" in text
    assert "<code>1</code>" in text
    assert "<code>2</code>" in text


def test_progress_text_shows_total() -> None:
    text = _progress_text(sent=0, blocked=0, failed=0, skipped=0, total=42)
    assert "<code>42</code>" in text


# ---------------------------------------------------------------- Broadcaster


async def test_broadcaster_start_and_send_all(
    db: Database, config: Config, bot_factory,
) -> None:
    """Happy path: all recipients receive the message."""
    bot = bot_factory()
    cid = await setup_campaign(db, config, users=[1, 2])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == BroadcastStatus.COMPLETED.value
    assert campaign["sent"] == 2
    assert len(bot.copy_calls) == 2
    assert all(c["from_chat_id"] == 999 and c["message_id"] == 42 for c in bot.copy_calls)


async def test_broadcaster_handles_blocked_user(
    db: Database, config: Config, bot_factory,
) -> None:
    """User 3 raises Forbidden → marked blocked."""
    bot = bot_factory(fail_for={
        3: TelegramForbiddenError(
            SendMessage(chat_id=3, text="x"), "Forbidden: bot was blocked by the user",
        ),
    })
    cid = await setup_campaign(db, config, users=[2, 3])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == BroadcastStatus.COMPLETED.value
    assert campaign["sent"] == 1
    assert campaign["blocked"] == 1
    assert len(bot.copy_calls) == 1  # only user 2 succeeded


async def test_broadcaster_retries_flood(
    db: Database, config: Config, bot_factory,
) -> None:
    """TelegramRetryAfter(retry_after=1) → sleep 1s, retry once, succeeds."""
    await repo.upsert_user(db, config.admin_id_list[0])
    await add_user(db, 1)

    flood_bot = FakeBroadcastBot()
    original_copy = flood_bot.copy_message

    call_count = [0]
    async def flood_then_ok(chat_id, from_chat_id, message_id, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise TelegramRetryAfter(
                SendMessage(chat_id=chat_id, text="x"),
                "Too Many Requests: retry after 1", retry_after=1,
            )
        flood_bot.copy_calls.append({
            "chat_id": chat_id, "from_chat_id": from_chat_id,
            "message_id": message_id,
        })
        return SimpleNamespace(message_id=1)
    flood_bot.copy_message = flood_then_ok

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, [1])

    bc = Broadcaster(db, _config(bcast_flood_retry_threshold=60, bcast_retry_backoff_base=0.01), EventBus())
    await bc.start(cid, flood_bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["sent"] == 1
    assert call_count[0] == 2  # first failed, second succeeded


async def test_broadcaster_handles_permanent_fail(
    db: Database, config: Config, bot_factory,
) -> None:
    """Non-retryable error → marked failed."""
    bot = bot_factory(fail_for={
        1: TelegramAPIError(
            SendMessage(chat_id=1, text="x"), "Bad Request: chat not found",
        ),
    })
    cid = await setup_campaign(db, config, users=[1])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    if bc._tasks.get(cid):
        await asyncio.wait_for(bc._tasks[cid], timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["failed"] == 1
    assert campaign["sent"] == 0


async def test_broadcaster_cancel_mid_broadcast(
    db: Database, config: Config, bot_factory,
) -> None:
    """Cancel sets status to cancelled; remaining recipients stay pending."""
    bot = bot_factory()
    users = list(range(1, 21))
    # Insert all users + admin, create campaign, insert recipients
    await repo.upsert_user(db, config.admin_id_list[0])
    for uid in users:
        await add_user(db, uid, updated_at="2026-09-07T00:00:00Z")

    cid = await repo.create_broadcast(
        db, admin_id=config.admin_id_list[0], label="test",
        source_chat_id=999, source_message_id=42,
        mode="copy", filter_json=json.dumps(AudienceFilter.default().to_dict()),
    )
    await repo.insert_recipients(db, cid, users)

    # Patch bot.copy_message to be slow so we can cancel mid-broadcast
    original_copy = bot.copy_message
    async def slow_copy(chat_id, from_chat_id, message_id, **kwargs):
        await asyncio.sleep(0.05)
        bot.copy_calls.append({"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})
        return SimpleNamespace(message_id=1)
    bot.copy_message = slow_copy

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    await asyncio.sleep(0.15)  # let some sends happen
    await bc.cancel(cid, bot)

    # Wait for the campaign task to fully complete (finalization may write DB)
    task = bc._tasks.get(cid)
    if task:
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == BroadcastStatus.CANCELLED.value
    assert campaign["sent"] < 20  # didn't finish
    remaining = await repo.list_pending_recipients(db, cid, 1000)
    assert len(remaining) > 0  # some were never sent
    await bc.shutdown()


async def test_broadcaster_rate_limiting(
    db: Database, bot_factory,
) -> None:
    """max_bcast_concurrency=2 → no more than 2 concurrent copy_message calls."""
    bot = bot_factory()
    users = list(range(1, 7))

    rl_config = _config(max_bcast_concurrency=2, bcast_max_rate_per_second=1000)
    cid = await setup_campaign(db, rl_config, users=users)

    # Track concurrent calls
    current = 0
    peak = 0
    lock = asyncio.Lock()

    async def tracking_copy(chat_id, from_chat_id, message_id, **kwargs):
        nonlocal current, peak
        async with lock:
            current += 1
            peak = max(peak, current)
        await asyncio.sleep(0.05)  # hold briefly
        async with lock:
            current -= 1
        bot.copy_calls.append({"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})
        return SimpleNamespace(message_id=1)
    bot.copy_message = tracking_copy

    bc = Broadcaster(db, rl_config, EventBus())
    await bc.start(cid, bot)
    task = bc._tasks.get(cid)
    if task:
        await asyncio.wait_for(task, timeout=15)
    await bc.shutdown()

    assert peak <= 2, f"peak concurrency was {peak}, expected <= 2"
    assert len(bot.copy_calls) == 6


async def test_broadcaster_recover_resumes_incomplete(
    db: Database, config: Config, bot_factory,
) -> None:
    """recover() resumes a campaign with unprocessed recipients."""
    bot = bot_factory()
    # Patch to slow so the campaign is still running when we "crash"
    original_copy = bot.copy_message
    async def slow_copy(chat_id, from_chat_id, message_id, **kwargs):
        await asyncio.sleep(0.5)  # slow enough to still be running
        return await original_copy(chat_id, from_chat_id, message_id, **kwargs)
    bot.copy_message = slow_copy

    cid = await setup_campaign(db, config, users=[1, 2, 3])

    bc = Broadcaster(db, config, EventBus())
    await bc.start(cid, bot)
    await asyncio.sleep(0.1)  # let it start

    # Simulate crash: set status back to 'running' with partial progress
    await repo.set_broadcast_status(db, cid, "running", sent=1)
    # Mark user 1 as sent, 2 and 3 still pending
    await repo.update_recipient_status(db, cid, 1, "sent")
    await repo.update_recipient_status(db, cid, 2, "pending")
    await repo.update_recipient_status(db, cid, 3, "pending")

    # Cancel the original task via the cancel event + task cancellation
    cancel_evt = bc._cancel_events.get(cid)
    if cancel_evt:
        cancel_evt.set()
    task = bc._tasks.get(cid)
    if task:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    bc._tasks.pop(cid, None)
    bc._cancel_events.pop(cid, None)
    bc._pause_events.pop(cid, None)
    bc._token_buckets.pop(cid, None)
    bc._filters.pop(cid, None)

    # Create a NEW broadcaster to simulate restart
    bc2 = Broadcaster(db, config, EventBus())
    resumed = await bc2.recover(bot)
    assert cid in resumed

    rtask = bc2._tasks.get(cid)
    if rtask:
        await asyncio.wait_for(rtask, timeout=10)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["sent"] == 3  # all 3 eventually sent
    await bc2.shutdown()
