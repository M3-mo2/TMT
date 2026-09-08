"""Tests for Phase 4: admin broadcast UI router, keyboards, texts, and wiring.

Tests cover:
- callback constants
- keyboard builders
- text renderers
- handler functions (dashboard, compose, target toggle, dry run, test send,
  send now, schedule, history, view, cancel/pause/resume)
- _parse_schedule_time
- IsAdmin filter rejection
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.broadcast import (
    BcastFSM,
    _build_history_kb,
    _int_after,
    _parse_schedule_time,
    _toggle_filter,
    bcast_message_entered,
    bcast_scheduled_for_entered,
    cb_bcast_cancel_live,
    cb_bcast_draft_resume,
    cb_bcast_dry_run,
    cb_bcast_history,
    cb_bcast_history_page,
    cb_bcast_new,
    cb_bcast_pause_live,
    cb_bcast_resume_live,
    cb_bcast_schedule,
    cb_bcast_send_now,
    cb_bcast_target_x,
    cb_bcast_test_send,
    cb_bcast_view,
    cb_broadcast,
)
from app.bot.routers.admin.filters import IsAdmin
from app.bot.routers.admin.keyboards import (
    broadcast_center_kb,
    broadcast_confirm_kb,
    broadcast_history_kb,
    broadcast_live_kb,
    broadcast_target_kb,
)
from app.bot.texts import (
    bcast_status_label,
    render_audience_builder,
    render_bcast_preview,
    render_bcast_progress,
    render_bcast_summary,
    render_broadcast_center,
)
from app.config import Config
from app.core.broadcast_models import AudienceFilter
from app.db import repositories as repo
from app.db.database import Database


# ---------------------------------------------------------------- helpers


def _config(**overrides) -> Config:
    kwargs: dict = dict(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1001",
        bcast_max_rate_per_second=100,
        bcast_retry_attempts=3,
        bcast_retry_backoff_base=0.01,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


class FakeBroadcaster:
    """Records lifecycle calls instead of running real workers."""

    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.cancelled: list[tuple] = []
        self.paused: list = []
        self.resumed: list[tuple] = []

    async def start(self, campaign_id: int, bot) -> None:
        self.started.append((campaign_id, bot))

    async def cancel(self, campaign_id: int, bot) -> None:
        self.cancelled.append((campaign_id, bot))

    async def pause(self, campaign_id: int) -> bool:
        self.paused.append(campaign_id)
        return True

    async def resume(self, campaign_id: int, bot) -> bool:
        self.resumed.append((campaign_id, bot))
        return True


def make_cb(data: str, user_id: int = 1001, bot=None) -> MagicMock:
    """Create a fake CallbackQuery compatible with handlers + safe_edit."""
    cb = MagicMock()
    cb.data = data
    cb.id = "test-cb"
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Admin")
    cb.bot = bot
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.chat = SimpleNamespace(id=user_id, type="private")
    return cb


def make_message(text: str, user_id: int = 1001, bot=None) -> MagicMock:
    """Create a fake Message for FSM message handlers."""
    msg = MagicMock()
    msg.text = text
    msg.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Admin")
    msg.chat = SimpleNamespace(id=user_id, type="private")
    msg.message_id = 1
    msg.bot = bot
    msg.answer = AsyncMock()
    return msg


@pytest.fixture
def config() -> Config:
    return _config()


@pytest.fixture
def broadcaster() -> FakeBroadcaster:
    return FakeBroadcaster()


@pytest.fixture
def fsm_context() -> FSMContext:
    storage = MemoryStorage()
    ctx = FSMContext(
        storage=storage,
        key=StorageKey(chat_id=1001, user_id=1001, bot_id=1),
    )
    return ctx


@pytest.fixture
def safe_edit_mock():
    with patch(
        "app.bot.routers.admin.broadcast.safe_edit", new_callable=AsyncMock
    ) as mock:
        yield mock


# ---------------------------------------------------------------- users in DB


async def add_user(db: Database, user_id: int) -> None:
    await repo.upsert_user(db, user_id)


async def add_admin(db: Database, admin_id: int = 1001) -> None:
    await repo.upsert_user(db, admin_id)


# ---------------------------------------------------------------- callback constants


class TestCallbackConstants:
    def test_new_constants_exist(self) -> None:
        assert C.BCAST_NEW == "adm:bcast:new"
        assert C.BCAST_DRAFT_RESUME == "adm:bcast:draft:"
        assert C.BCAST_TARGET_X == "adm:bcast:target:"
        assert C.BCAST_DRY_RUN == "adm:bcast:dry_run"
        assert C.BCAST_TEST_SEND == "adm:bcast:test_send"
        assert C.BCAST_SEND_NOW == "adm:bcast:send_now"
        assert C.BCAST_SCHEDULE == "adm:bcast:schedule"
        assert C.BCAST_CANCEL_LIVE == "adm:bcast:cancel:"
        assert C.BCAST_PAUSE_LIVE == "adm:bcast:pause:"
        assert C.BCAST_RESUME_LIVE == "adm:bcast:resume:"
        assert C.BCAST_HISTORY == "adm:bcast:history"
        assert C.BCAST_VIEW == "adm:bcast:view:"

    def test_existing_constants_preserved(self) -> None:
        assert C.BCAST == "adm:bcast"
        assert C.BCAST_PAGE == "adm:bcast:page:"

    def test_int_after_parses_campaign_id(self) -> None:
        assert _int_after("adm:bcast:view:42", C.BCAST_VIEW) == 42
        assert _int_after("adm:bcast:cancel:99", C.BCAST_CANCEL_LIVE) == 99
        assert _int_after("adm:bcast:view:abc", C.BCAST_VIEW) is None


# ---------------------------------------------------------------- keyboard builders


class TestKeyboards:
    def test_broadcast_center_kb(self) -> None:
        kb = broadcast_center_kb()
        assert isinstance(kb, InlineKeyboardMarkup)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert C.BCAST_NEW in flat
        assert C.BCAST_HISTORY in flat
        assert C.MENU in flat

    def test_broadcast_target_kb_has_all_dimensions(self) -> None:
        filters = AudienceFilter.default()
        kb = broadcast_target_kb(filters)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        for dim in ("all", "active", "inactive", "blocked",
                     "accounts_min", "accounts_max",
                     "registered_days", "last_seen",
                     "exclude_admins", "exclude_previous"):
            assert f"{C.BCAST_TARGET_X}{dim}" in flat, f"missing dim: {dim}"
        assert C.BCAST_TEST_SEND in flat
        assert C.BCAST_DRY_RUN in flat
        assert C.BCAST_SEND_NOW in flat
        assert C.BCAST_SCHEDULE in flat
        assert C.BCAST in flat

    def test_broadcast_confirm_kb(self) -> None:
        kb = broadcast_confirm_kb(42)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert C.BCAST_SEND_NOW in flat
        assert C.BCAST_SCHEDULE in flat
        assert C.BCAST_TEST_SEND in flat
        assert f"{C.BCAST_VIEW}42" in flat

    def test_broadcast_live_kb(self) -> None:
        kb = broadcast_live_kb(42, "running")
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.BCAST_PAUSE_LIVE}42" in flat
        assert f"{C.BCAST_RESUME_LIVE}42" in flat
        assert f"{C.BCAST_CANCEL_LIVE}42" in flat

    def test_broadcast_history_kb_pagination(self) -> None:
        kb0 = broadcast_history_kb(0, 3)
        flat0 = [b.callback_data for row in kb0.inline_keyboard for b in row]
        assert f"{C.BCAST_PAGE}1" in flat0  # next page
        assert C.BCAST not in [b.callback_data for row in kb0.inline_keyboard[:-1] for b in row]

        kb2 = broadcast_history_kb(2, 3)
        flat2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
        assert f"{C.BCAST_PAGE}1" in flat2  # prev page
        assert C.BCAST in flat2  # back button


# ---------------------------------------------------------------- text renderers


class TestTextRenderers:
    def test_render_broadcast_center_empty(self) -> None:
        text = render_broadcast_center([], [], [], [])
        assert "لوحة البث" in text
        assert "مسودات" in text
        assert "مكتملة" in text

    def test_render_broadcast_center_with_campaigns(self) -> None:
        drafts = [{"id": 1, "label": "Test", "status": "draft"}]
        scheduled = [{"id": 2, "label": "Scheduled", "status": "scheduled"}]
        running = [{"id": 3, "label": "Running", "status": "running"}]
        completed = [{"id": 4, "label": "Done", "status": "completed"}]
        text = render_broadcast_center(drafts, scheduled, running, completed)
        assert "#1" in text and "Test" in text
        assert "#2" in text
        assert "#3" in text
        assert "#4" in text

    def test_render_broadcast_center_escapes_html(self) -> None:
        drafts = [{"id": 1, "label": "<script>alert(1)</script>", "status": "draft"}]
        text = render_broadcast_center(drafts, [], [], [])
        assert "&lt;script&gt;" in text
        assert "<script>" not in text

    def test_render_audience_builder(self) -> None:
        filters = AudienceFilter(target="active", exclude_admins=True)
        text = render_audience_builder(filters, 42)
        assert "42" in text
        assert "active" in text
        assert "✓" in text  # exclude_admins is True

    def test_render_audience_builder_none_values(self) -> None:
        filters = AudienceFilter.default()
        text = render_audience_builder(filters, 0)
        assert "—" in text  # None values shown as dash

    def test_render_bcast_preview(self) -> None:
        campaign = {"label": "Test", "mode": "copy", "id": 1}
        text = render_bcast_preview(campaign, 100, 10.0)
        assert "100" in text
        assert "10" in text
        assert "نسخة" in text

    def test_render_bcast_progress(self) -> None:
        campaign = {
            "id": 5, "sent": 10, "blocked": 1, "failed": 2,
            "skipped": 0, "total_recipients": 100,
        }
        text = render_bcast_progress(campaign, 5.0)
        assert "5.0" in text  # rate
        assert "<code>10</code>" in text  # sent
        assert "<code>100</code>" in text  # total

    def test_render_bcast_summary(self) -> None:
        campaign = {
            "id": 7, "label": "My Campaign", "status": "completed",
            "sent": 50, "blocked": 2, "failed": 1, "skipped": 0,
            "total_recipients": 53, "avg_rate": 12.5,
        }
        text = render_bcast_summary(campaign)
        assert "#7" in text
        assert "My Campaign" in text
        assert "مكتملة" in text  # status label
        assert "12.5" in text

    def test_render_bcast_summary_with_error(self) -> None:
        campaign = {"id": 1, "label": "Err", "status": "failed", "error": "boom <x>"}
        text = render_bcast_summary(campaign)
        assert "boom &lt;x&gt;" in text

    def test_bcast_status_labels(self) -> None:
        assert "مسودة" in bcast_status_label("draft")
        assert "مجدولة" in bcast_status_label("scheduled")
        assert "جارية" in bcast_status_label("running")
        assert "مكتملة" in bcast_status_label("completed")
        assert "ملغاة" in bcast_status_label("cancelled")
        assert "فاشلة" in bcast_status_label("failed")


# ---------------------------------------------------------------- _toggle_filter


class TestToggleFilter:
    def test_toggle_target(self) -> None:
        f = AudienceFilter(target="all")
        _toggle_filter(f, "active")
        assert f.target == "active"
        _toggle_filter(f, "blocked")
        assert f.target == "blocked"

    def test_toggle_numeric_cycles(self) -> None:
        f = AudienceFilter()
        assert f.account_count_min is None
        _toggle_filter(f, "accounts_min")
        assert f.account_count_min == 1
        _toggle_filter(f, "accounts_min")
        assert f.account_count_min == 5
        _toggle_filter(f, "accounts_min")
        assert f.account_count_min == 10
        _toggle_filter(f, "accounts_min")
        assert f.account_count_min is None

    def test_toggle_boolean(self) -> None:
        f = AudienceFilter()
        assert not f.exclude_admins
        _toggle_filter(f, "exclude_admins")
        assert f.exclude_admins
        _toggle_filter(f, "exclude_admins")
        assert not f.exclude_admins

    def test_toggle_exclude_previous(self) -> None:
        f = AudienceFilter()
        assert not f.exclude_previously_contacted
        _toggle_filter(f, "exclude_previous")
        assert f.exclude_previously_contacted


# ---------------------------------------------------------------- _parse_schedule_time


class TestParseScheduleTime:
    def test_relative_hours(self) -> None:
        result = _parse_schedule_time("+2h")
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert dt > now
        assert abs((dt - now).total_seconds() - 7200) < 60

    def test_relative_days(self) -> None:
        result = _parse_schedule_time("+1d")
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert (dt - now).total_seconds() > 86000  # >1 day minus 1 min

    def test_relative_minutes(self) -> None:
        result = _parse_schedule_time("+30m")
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert abs((dt - now).total_seconds() - 1800) < 60

    def test_iso_timestamp(self) -> None:
        result = _parse_schedule_time("2030-06-01T12:00:00Z")
        assert result == "2030-06-01T12:00:00Z"

    def test_natural_tomorrow(self) -> None:
        result = _parse_schedule_time("tomorrow 20:00")
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert dt > now

    def test_arabic_tomorrow(self) -> None:
        result = _parse_schedule_time("غداً 20:00")
        assert result is not None
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert dt > now

    def test_past_time_returns_none(self) -> None:
        result = _parse_schedule_time("2020-01-01T00:00:00Z")
        assert result is None

    def test_invalid_returns_none(self) -> None:
        assert _parse_schedule_time("not a time") is None
        assert _parse_schedule_time("") is None
        assert _parse_schedule_time("abc123xyz") is None


# ---------------------------------------------------------------- handlers


class TestCbBroadcast:
    async def test_opens_dashboard(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db)
        cb = make_cb(C.BCAST, bot=bot_factory())
        await cb_broadcast(cb, db)

        assert cb.answer.called
        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        kb = kwargs.get("reply_markup")
        assert "لوحة البث" in text
        assert isinstance(kb, InlineKeyboardMarkup)


# ---------------------------------------------------------------- compose flow


class TestComposeFlow:
    async def test_bcast_new_enters_compose(
        self, fsm_context, safe_edit_mock
    ) -> None:
        cb = make_cb(C.BCAST_NEW)
        await cb_bcast_new(cb, fsm_context)

        assert cb.answer.called
        assert await fsm_context.get_state() == BcastFSM.compose.state
        state_data = await fsm_context.get_data()
        assert state_data.get("campaign_id") is None
        assert safe_edit_mock.called

    async def test_message_enters_creates_draft_and_shows_target(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        bot = bot_factory()
        await add_admin(db)
        # Enter compose state first
        await fsm_context.set_state(BcastFSM.compose.state)

        msg = make_message("hello world", bot=bot)
        await bcast_message_entered(msg, fsm_context, db, config)

        # Verify draft created
        drafts = await repo.list_broadcasts(db, status="draft")
        assert len(drafts) == 1
        draft = drafts[0]
        assert draft["source_chat_id"] == 1001
        assert draft["source_message_id"] == 1
        assert draft["mode"] == "copy"
        assert draft["admin_id"] == 1001

        # Verify state changed
        assert await fsm_context.get_state() == BcastFSM.target.state
        state_data = await fsm_context.get_data()
        assert state_data["campaign_id"] == draft["id"]

        # Verify target selector shown
        assert msg.answer.called
        args, kwargs = msg.answer.call_args
        assert kwargs.get("reply_markup") is not None

    async def test_message_entered_no_from_user(
        self, db: Database, config: Config, fsm_context
    ) -> None:
        msg = make_message("hello", bot=None)
        msg.from_user = None
        await bcast_message_entered(msg, fsm_context, db, config)
        assert not await repo.list_broadcasts(db, status="draft")


class TestTargetToggle:
    async def test_toggle_updates_count(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        await add_user(db, 1)
        await add_user(db, 2)
        await add_user(db, 3)

        # Create a draft and set FSM state
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=cid)

        cb = make_cb(f"{C.BCAST_TARGET_X}active", bot=bot_factory())
        await cb_bcast_target_x(cb, fsm_context, db, config)

        assert cb.answer.called
        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        kb = kwargs.get("reply_markup")
        # Count should be computed (active users with last_seen > 30 days ago)
        assert "المستخدمين" in text
        assert isinstance(kb, InlineKeyboardMarkup)

    async def test_base_click_without_suffix(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=cid)

        cb = make_cb(C.BCAST_TARGET_X, bot=bot_factory())
        await cb_bcast_target_x(cb, fsm_context, db, config)

        assert safe_edit_mock.called

    async def test_no_campaign_id_shows_error(
        self, db: Database, config: Config, fsm_context, safe_edit_mock
    ) -> None:
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=None)

        cb = make_cb(f"{C.BCAST_TARGET_X}active")
        await cb_bcast_target_x(cb, fsm_context, db, config)

        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        assert "انتهت الجلسة" in args[1]


class TestDraftResume:
    async def test_resume_draft_enters_target(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="draft-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        cb = make_cb(f"{C.BCAST_DRAFT_RESUME}{cid}", bot=bot_factory())
        await cb_bcast_draft_resume(cb, fsm_context, db, config)

        assert await fsm_context.get_state() == BcastFSM.target.state
        data = await fsm_context.get_data()
        assert data["campaign_id"] == cid
        assert safe_edit_mock.called

    async def test_resume_nonexistent_shows_error(
        self, db: Database, config: Config, fsm_context, safe_edit_mock
    ) -> None:
        cb = make_cb(f"{C.BCAST_DRAFT_RESUME}99999")
        await cb_bcast_draft_resume(cb, fsm_context, db, config)

        args, kwargs = safe_edit_mock.call_args
        assert "غير موجودة" in args[1]


# ---------------------------------------------------------------- dry run / test


class TestDryRun:
    async def test_dry_run_shows_preview(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        await add_user(db, 1)
        await add_user(db, 2)

        cid = await repo.create_broadcast(
            db, admin_id=1001, label="preview-test",
            source_chat_id=999, source_message_id=1, mode="copy",
            filter_json=json.dumps(AudienceFilter.default().to_dict()),
        )
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=cid)

        cb = make_cb(C.BCAST_DRY_RUN, bot=bot_factory())
        await cb_bcast_dry_run(cb, fsm_context, db, config)

        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        kb = kwargs.get("reply_markup")
        assert "معاينة" in text
        assert "3" in text  # recipient count (admin + 2 users)
        assert isinstance(kb, InlineKeyboardMarkup)


class TestTestSend:
    async def test_test_send_copies_to_admins(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        bot = bot_factory()

        cid = await repo.create_broadcast(
            db, admin_id=1001, label="test-send",
            source_chat_id=999, source_message_id=1, mode="copy",
            filter_json=json.dumps(AudienceFilter.default().to_dict()),
        )
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=cid)

        cb = make_cb(C.BCAST_TEST_SEND, bot=bot)
        await cb_bcast_test_send(cb, fsm_context, db, config)

        assert len(bot.copy_calls) == 1
        assert bot.copy_calls[0]["chat_id"] == 1001
        assert bot.copy_calls[0]["from_chat_id"] == 999
        assert bot.copy_calls[0]["message_id"] == 1


# ---------------------------------------------------------------- send now


class TestSendNow:
    async def test_send_now_calls_broadcaster_start(
        self, db: Database, config: Config, bot_factory, fsm_context, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        bot = bot_factory()
        bc = FakeBroadcaster()

        cid = await repo.create_broadcast(
            db, admin_id=1001, label="send-now",
            source_chat_id=999, source_message_id=1, mode="copy",
            filter_json=json.dumps(AudienceFilter.default().to_dict()),
        )
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=cid)

        cb = make_cb(C.BCAST_SEND_NOW, bot=bot)
        await cb_bcast_send_now(cb, fsm_context, db, bc)

        assert len(bc.started) == 1
        assert bc.started[0][0] == cid
        assert bc.started[0][1] is bot

        # FSM should be cleared
        assert await fsm_context.get_state() is None

        # safe_edit should have been called with live kb
        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        kb = kwargs.get("reply_markup")
        assert isinstance(kb, InlineKeyboardMarkup)

    async def test_send_now_no_campaign_shows_error(
        self, db: Database, fsm_context, safe_edit_mock
    ) -> None:
        await fsm_context.set_state(BcastFSM.target.state)
        await fsm_context.update_data(campaign_id=None)

        cb = make_cb(C.BCAST_SEND_NOW)
        await cb_bcast_send_now(cb, fsm_context, db, FakeBroadcaster())

        args, kwargs = safe_edit_mock.call_args
        assert "انتهت الجلسة" in args[1]


# ---------------------------------------------------------------- schedule


class TestSchedule:
    async def test_enter_schedule_state(
        self, fsm_context, safe_edit_mock
    ) -> None:
        cb = make_cb(C.BCAST_SCHEDULE)
        await cb_bcast_schedule(cb, fsm_context)

        assert await fsm_context.get_state() == BcastFSM.scheduled_for.state
        assert cb.answer.called
        assert safe_edit_mock.called

    async def test_scheduled_for_entered_iso(
        self, db: Database, fsm_context
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="sched",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await fsm_context.set_state(BcastFSM.scheduled_for.state)
        await fsm_context.update_data(campaign_id=cid)

        msg = make_message("2030-06-01T12:00:00Z")
        await bcast_scheduled_for_entered(msg, fsm_context, db)

        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == "scheduled"
        assert campaign["scheduled_for"] == "2030-06-01T12:00:00Z"
        assert await fsm_context.get_state() is None
        assert msg.answer.called

    async def test_scheduled_for_entered_invalid(
        self, db: Database, fsm_context
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="sched",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await fsm_context.set_state(BcastFSM.scheduled_for.state)
        await fsm_context.update_data(campaign_id=cid)

        msg = make_message("not-a-date")
        await bcast_scheduled_for_entered(msg, fsm_context, db)

        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == "draft"  # unchanged
        assert msg.answer.called

    async def test_scheduled_for_entered_relative(
        self, db: Database, fsm_context
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="sched",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await fsm_context.set_state(BcastFSM.scheduled_for.state)
        await fsm_context.update_data(campaign_id=cid)

        msg = make_message("+2h")
        await bcast_scheduled_for_entered(msg, fsm_context, db)

        campaign = await repo.get_broadcast(db, cid)
        assert campaign["status"] == "scheduled"
        assert campaign["scheduled_for"] is not None


# ---------------------------------------------------------------- history / view


class TestHistoryView:
    async def test_history_empty(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db)
        cb = make_cb(C.BCAST_HISTORY, bot=bot_factory())
        await cb_bcast_history(cb, db)

        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        assert "التاريخ" in text

    async def test_history_with_campaigns(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="completed-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await repo.set_broadcast_status(db, cid, "completed", sent=5)

        cb = make_cb(C.BCAST_HISTORY, bot=bot_factory())
        await cb_bcast_history(cb, db)

        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        assert "#1" in text  # campaign id is 1

    async def test_history_pagination(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        for i in range(15):
            cid = await repo.create_broadcast(
                db, admin_id=1001, label=f"camp-{i}",
                source_chat_id=999, source_message_id=1, mode="copy",
            )
            await repo.set_broadcast_status(db, cid, "completed", sent=1)

        cb = make_cb(C.BCAST_HISTORY, bot=bot_factory())
        await cb_bcast_history(cb, db)

        # page 0 should show first 10 campaigns
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        kb = kwargs.get("reply_markup")
        assert "15" not in text or "← السابق" not in [b.text for row in kb.inline_keyboard for b in row]

        # Go to page 1
        cb2 = make_cb(f"{C.BCAST_PAGE}1", bot=bot_factory())
        await cb_bcast_history_page(cb2, db)

        args2, kwargs2 = safe_edit_mock.call_args
        kb2 = kwargs2.get("reply_markup")
        # Should have "previous" button on page 1
        nav_texts = [b.text for row in kb2.inline_keyboard for b in row]
        assert any("السابق" in t for t in nav_texts)

    async def test_build_history_kb(self) -> None:
        campaigns = [{"id": 1, "label": "test", "status": "completed"}]
        kb = _build_history_kb(campaigns, 0, 1)
        assert isinstance(kb, InlineKeyboardMarkup)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.BCAST_VIEW}1" in flat
        assert C.BCAST in flat


# ---------------------------------------------------------------- view / live control


class TestViewAndLive:
    async def test_view_shows_summary(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="view-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await repo.set_broadcast_status(db, cid, "completed", sent=5)

        cb = make_cb(f"{C.BCAST_VIEW}{cid}", bot=bot_factory())
        await cb_bcast_view(cb, db, None)

        assert safe_edit_mock.called
        args, kwargs = safe_edit_mock.call_args
        text = args[1]
        assert "#1" in text  # campaign id
        assert "مكتملة" in text  # status label

    async def test_view_running_shows_live_kb(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="running-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        await repo.set_broadcast_status(db, cid, "running", sent=2, total_recipients=10)

        cb = make_cb(f"{C.BCAST_VIEW}{cid}", bot=bot_factory())
        await cb_bcast_view(cb, db, None)

        args, kwargs = safe_edit_mock.call_args
        kb = kwargs.get("reply_markup")
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.BCAST_PAUSE_LIVE}{cid}" in flat
        assert f"{C.BCAST_CANCEL_LIVE}{cid}" in flat

    async def test_cancel_live_calls_broadcaster_cancel(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="cancel-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        bot = bot_factory()
        bc = FakeBroadcaster()

        cb = make_cb(f"{C.BCAST_CANCEL_LIVE}{cid}", bot=bot)
        await cb_bcast_cancel_live(cb, db, bc)

        assert len(bc.cancelled) == 1
        assert bc.cancelled[0][0] == cid
        assert bc.cancelled[0][1] is bot
        assert safe_edit_mock.called

    async def test_pause_live_calls_broadcaster_pause(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="pause-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        bc = FakeBroadcaster()

        cb = make_cb(f"{C.BCAST_PAUSE_LIVE}{cid}", bot=bot_factory())
        await cb_bcast_pause_live(cb, db, bc)

        assert len(bc.paused) == 1
        assert bc.paused[0] == cid
        assert safe_edit_mock.called

    async def test_resume_live_calls_broadcaster_resume(
        self, db: Database, bot_factory, safe_edit_mock
    ) -> None:
        await add_admin(db, 1001)
        cid = await repo.create_broadcast(
            db, admin_id=1001, label="resume-test",
            source_chat_id=999, source_message_id=1, mode="copy",
        )
        bot = bot_factory()
        bc = FakeBroadcaster()

        cb = make_cb(f"{C.BCAST_RESUME_LIVE}{cid}", bot=bot)
        await cb_bcast_resume_live(cb, db, bc)

        assert len(bc.resumed) == 1
        assert bc.resumed[0][0] == cid
        assert bc.resumed[0][1] is bot
        assert safe_edit_mock.called


# ---------------------------------------------------------------- IsAdmin filter


class TestIsAdminFilter:
    async def test_accepts_admin(self) -> None:
        config = _config(admin_ids="1001,1002")
        filt = IsAdmin()
        event = SimpleNamespace(from_user=SimpleNamespace(id=1001))
        assert await filt(event, config) is True

    async def test_rejects_non_admin(self) -> None:
        config = _config(admin_ids="1001")
        filt = IsAdmin()
        event = SimpleNamespace(from_user=SimpleNamespace(id=999))
        assert await filt(event, config) is False

    async def test_rejects_no_user(self) -> None:
        config = _config(admin_ids="1001")
        filt = IsAdmin()
        event = SimpleNamespace(from_user=None)
        assert await filt(event, config) is False