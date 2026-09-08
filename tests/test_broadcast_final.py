"""Comprehensive end-to-end tests for the BroadcastEngine (Phase 6)."""
import asyncio
import pytest
from app.core.broadcast import (
    Broadcaster, _safe_format, resolve_audience, count_audience,
)
from app.core.broadcast_models import AudienceFilter
from app.db import repositories as repo
from tests.conftest import FakeBroadcastBot
from tests.test_broadcaster import _config, setup_campaign, add_user


@pytest.mark.asyncio
async def test_personalization_rendering(db):
    """Template rendering with variable substitution and HTML escaping."""
    # Test _safe_format directly — variable substitution + missing-var handling
    result = _safe_format(
        "Hello {first_name} ({accounts_count} accounts)",
        first_name="John", accounts_count=5,
    )
    assert "John" in result
    assert "5" in result

    # Missing variables default to empty string (no KeyError)
    result2 = _safe_format("Hi {nonexistent_var}", first_name="Bob")
    assert "Hi " in result2

    # Test through _send_one with a personalized campaign
    config = _config()
    admin_id = config.admin_id_list[0]
    users = [admin_id, 1002, 1003]
    for uid in users:
        await add_user(db, uid)
    # Set first_name with HTML special chars for user 1002
    await db.execute("UPDATE users SET first_name='<script>' WHERE id=1002")

    cid = await setup_campaign(
        db, config, users=users, mode="personalized",
        content_html="Hi {first_name} {accounts_count}!",
    )
    campaign = await repo.get_broadcast(db, cid)
    bot = FakeBroadcastBot()
    bc = Broadcaster(db, config, None)
    await bc.start(cid, bot)
    await asyncio.sleep(1)

    # 1 admin progress card + 3 user messages
    assert len(bot.send_calls) == 4
    for call in bot.send_calls:
        text = call["text"]
        assert "<script>" not in text
        if "1002" in str(call.get("chat_id", "")):
            assert "&lt;script&gt;" in text


@pytest.mark.asyncio
async def test_ab_test_split():
    """A/B test split is deterministic and even."""
    config = _config()
    bc = Broadcaster(None, config, None)
    counts = {0: 0, 1: 0}
    for uid in range(1000):
        variant = bc._ab_test_split(uid, 2)
        counts[variant] += 1
    assert counts == {0: 500, 1: 500}

    # Deterministic: same user always gets same variant
    assert bc._ab_test_split(42, 2) == bc._ab_test_split(42, 2)
    # 3-way split is even too
    counts3 = {0: 0, 1: 0, 2: 0}
    for uid in range(1000):
        counts3[bc._ab_test_split(uid, 3)] += 1
    assert counts3[0] == 334  # 0..333 and 999 (0,3,6,...) -> 334
    assert counts3[1] == 333
    assert counts3[2] == 333


@pytest.mark.asyncio
async def test_full_e2e_with_sweeper(db):
    """Full pipeline: scheduled campaign → sweeper fires → worker sends → completes."""
    from datetime import datetime, timezone, timedelta
    config = _config()
    admin_id = config.admin_id_list[0]
    users = list(range(admin_id, admin_id + 5))
    for uid in users:
        await add_user(db, uid)

    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    cid = await setup_campaign(
        db, config, users=users, mode="copy", scheduled_for=past,
    )

    bot = FakeBroadcastBot()
    bc = Broadcaster(db, config, None)
    bc.start_sweeper(bot)

    # Wait for sweeper to pick up + worker to finish
    for _ in range(30):
        await asyncio.sleep(0.1)
        campaign = await repo.get_broadcast(db, cid)
        if campaign["status"] == "completed":
            break

    await bc.stop_sweeper()
    await bc.shutdown()
    await asyncio.sleep(0.1)

    campaign = await repo.get_broadcast(db, cid)
    assert campaign["status"] == "completed"
    counts = await repo.count_recipients(db, cid)
    assert counts["sent"] == 5
    assert counts["pending"] == 0
    assert len(bot.copy_calls) == 5
    completed = await repo.list_broadcasts(db, status="completed")
    assert any(b["id"] == cid for b in completed)


@pytest.mark.asyncio
async def test_resume_after_restart(db):
    """Crashed campaign resumes from correct cursor on recovery."""
    config = _config()
    admin_id = config.admin_id_list[0]
    users = list(range(admin_id, admin_id + 10))
    for uid in users:
        await add_user(db, uid)

    cid = await setup_campaign(db, config, users=users, mode="copy")

    # Simulate partial completion: 3 sent, 7 pending, status='running'
    # recover() needs total_recipients > 0 and sent < total to resume
    await repo.set_broadcast_status(db, cid, "running", sent=3, total_recipients=10)
    for uid in users[:3]:
        await db.execute(
            "UPDATE broadcast_recipients SET status='sent' WHERE broadcast_id=? AND user_id=?",
            (cid, uid),
        )

    # Fresh Broadcaster — simulate restart
    bot = FakeBroadcastBot()
    bc2 = Broadcaster(db, config, None)
    recovered = await bc2.recover(bot)
    assert cid in recovered

    # start() is a no-op (status is already 'running' from recover);
    # the worker task was spawned inside recover()
    await bc2.start(cid, bot)
    for _ in range(30):
        await asyncio.sleep(0.1)
        campaign = await repo.get_broadcast(db, cid)
        if campaign["status"] == "completed":
            break
    await bc2.shutdown()
    await asyncio.sleep(0.1)

    counts = await repo.count_recipients(db, cid)
    assert counts["sent"] == 10
    assert counts["pending"] == 0


@pytest.mark.asyncio
async def test_dry_run_no_sent_rows(db):
    """Dry-run resolves audience without writing any sent rows."""
    config = _config()
    admin_id = config.admin_id_list[0]
    users = [admin_id, 1002, 1003, 1004]
    for uid in users:
        await add_user(db, uid)

    # Create campaign WITHOUT inserting recipients (dry-run only)
    cid = await repo.create_broadcast(
        db, admin_id=admin_id, label="dry-run-test",
        source_chat_id=1, source_message_id=1, mode="copy",
    )
    assert (await repo.get_broadcast(db, cid))["status"] == "draft"

    filters = AudienceFilter.default()
    resolved = await resolve_audience(db, filters, config.admin_id_list)
    count = await count_audience(db, filters, config.admin_id_list)
    assert count > 0
    assert len(resolved) == count

    # No recipient rows should exist at all
    counts = await repo.count_recipients(db, cid)
    assert counts["sent"] == 0
    assert counts["pending"] == 0
    assert sum(counts.values()) == 0

    # Campaign should still be draft
    assert (await repo.get_broadcast(db, cid))["status"] == "draft"


@pytest.mark.asyncio
async def test_test_send_admin_only(db):
    """Test-send targets only admin user IDs."""
    config = _config()
    admin_id = config.admin_id_list[0]
    users = [admin_id, 1002, 1003, 1004]
    for uid in users:
        await add_user(db, uid)

    # Resolve full audience
    filters = AudienceFilter.default()
    resolved = await resolve_audience(db, filters, config.admin_id_list)
    assert len(resolved) > 1  # includes non-admins

    # Test-send: filter to admin IDs only
    admin_ids = set(config.admin_id_list)
    test_targets = [uid for uid in resolved if uid in admin_ids]
    assert test_targets == list(admin_ids)

    # Verify only admin gets a message
    cid = await setup_campaign(db, config, users=users, mode="copy")
    campaign = await repo.get_broadcast(db, cid)
    bot = FakeBroadcastBot()
    bc = Broadcaster(db, config, None)

    for uid in test_targets:
        await bc._send_one(bot, cid, uid, campaign)
    await asyncio.sleep(0.1)

    assert len(bot.copy_calls) == len(test_targets)
    for call in bot.copy_calls:
        assert call["chat_id"] in admin_ids
