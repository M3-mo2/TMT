"""Tests for the admin notifications router, keyboards, and text renderers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.notifications import (
    _int_after,
    cb_notify_list,
    cb_notify_page,
    cb_notify_read,
    cb_notify_mark_all,
    cb_notify_dismiss,
    cb_notify_settings,
    cb_notify_toggle,
)
from app.bot.routers.admin.keyboards import (
    notifications_list_kb,
    notify_settings_kb,
)
from app.bot.texts import (
    M_NOTIFY_TITLE, M_NOTIFY_EMPTY,
    notification_event_label,
    notification_severity_label,
    render_notifications_list,
    render_notify_settings,
)
from app.config import Config
from app.core.events import SYSTEM_EVENTS
from app.db import repositories as repo
from app.db.database import Database


# ---------------------------------------------------------------- helpers


def _config(**overrides) -> Config:
    kwargs: dict = dict(
        bot_token="123456:test_token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1001,1002",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def make_cb(data: str, user_id: int = 1001) -> MagicMock:
    """Create a fake CallbackQuery compatible with handlers + safe_edit."""
    cb = MagicMock()
    cb.data = data
    cb.id = "test-cb"
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Admin")
    cb.bot = MagicMock()
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.chat = SimpleNamespace(id=user_id, type="private")
    cb.config = _config()
    return cb


@pytest.fixture
def config() -> Config:
    return _config()


@pytest.fixture
def admin_ids(config: Config) -> list[int]:
    return config.admin_id_list


async def _seed_notifications(db: Database, admin_id: int, count: int) -> list[int]:
    ids = []
    for i in range(count):
        ids.append(await repo.create_notification(
            db, owner_id=admin_id, event_type="user_joined",
            title=f"t{i}", body="b",
        ))
    return ids


# ---------------------------------------------------------------- callback constants


class TestNotifyCallbacks:
    def test_notify_constants(self) -> None:
        assert C.NOTIFY == "adm:notify"
        assert C.NOTIFY_PAGE == "adm:notify:p:"
        assert C.NOTIFY_READ == "adm:notify:read:"
        assert C.NOTIFY_DISMISS == "adm:notify:dismiss:"
        assert C.NOTIFY_MARK_ALL == "adm:notify:markall"
        assert C.NOTIFY_TOGGLE == "adm:notify:toggle:"

    def test_int_after_notif(self) -> None:
        assert _int_after("adm:notify:read:5", C.NOTIFY_READ) == 5
        assert _int_after("adm:notify:dismiss:abc", C.NOTIFY_DISMISS) is None
        assert _int_after("adm:notify:p:3", C.NOTIFY_PAGE) == 3


# ---------------------------------------------------------------- keyboards


class TestNotificationsKeyboards:
    def test_notifications_list_kb_with_items(self) -> None:
        notifs = [
            {"id": 1, "title": "a", "event_type": "user_joined", "severity": "info"},
            {"id": 2, "title": "b", "event_type": "job_failed", "severity": "error"},
        ]
        kb = notifications_list_kb(notifs, page=0, admin_id=1001)
        rows = kb.inline_keyboard
        # Two notification rows with read + dismiss buttons
        assert rows[0][0].callback_data == f"{C.NOTIFY_READ}1"
        assert rows[0][1].callback_data == f"{C.NOTIFY_DISMISS}1"
        assert rows[1][0].callback_data == f"{C.NOTIFY_READ}2"
        assert rows[1][1].callback_data == f"{C.NOTIFY_DISMISS}2"

    def test_notifications_list_kb_pagination_prev(self) -> None:
        notifs = [{"id": i, "title": f"t{i}", "event_type": "x", "severity": "info"}
                  for i in range(3)]
        kb = notifications_list_kb(notifs, page=1, admin_id=1)
        nav_row = [b for b in kb.inline_keyboard if "السابق" in b[0].text]
        assert nav_row
        assert nav_row[0][0].callback_data == f"{C.NOTIFY_PAGE}0"

    def test_notify_settings_kb_has_all_event_types(self) -> None:
        kb = notify_settings_kb(admin_id=1)
        labels = []
        for row in kb.inline_keyboard:
            for btn in row:
                labels.append(btn.text)
        # Every SYSTEM_EVENT should have a toggle button
        for et in sorted(SYSTEM_EVENTS):
            assert notification_event_label(et) in labels


# ---------------------------------------------------------------- text renderers


class TestNotifyTextRenderers:
    def test_render_notifications_list_empty(self) -> None:
        rendered = render_notifications_list([], 0)
        assert M_NOTIFY_TITLE in rendered

    def test_render_notifications_list_shows_unread_count(self) -> None:
        notifs = [
            {"id": 1, "title": "a", "event_type": "user_joined", "severity": "info",
             "body": "b", "read_at": None, "created_at": "2026-01-01T00:00:00Z"},
        ]
        rendered = render_notifications_list(notifs, 1)
        assert "<code>1</code>" in rendered

    def test_render_notify_settings_shows_admin_count(self) -> None:
        rendered = render_notify_settings([1001, 1002], ["user_joined"])
        assert "<code>2</code>" in rendered
        assert "user_joined" in rendered

    def test_notification_event_label(self) -> None:
        assert notification_event_label("user_joined") == "مستخدم جديد"
        assert notification_event_label("job_completed") == "اكتملت عملية نقل"
        assert notification_event_label("broadcast_failed") == "فشل أو ألغي البث"

    def test_notification_severity_label(self) -> None:
        assert notification_severity_label("info") == "معلومات"
        assert notification_severity_label("error") == "خطأ"
        assert notification_severity_label("warning") == "تحذير"


# ---------------------------------------------------------------- handler tests


@pytest.fixture
def mock_edit(monkeypatch):
    """Patch safe_edit so handlers can run without a real aiogram Message."""
    mock = AsyncMock()
    monkeypatch.setattr(
        "app.bot.routers.admin.notifications.safe_edit", mock
    )
    return mock


async def test_cb_notify_list(db: Database, config: Config, mock_edit: AsyncMock) -> None:
    await _seed_notifications(db, 1001, 3)
    cb = make_cb(C.NOTIFY, user_id=1001)
    await cb_notify_list(cb, db=db)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert M_NOTIFY_TITLE in text


async def test_cb_notify_list_excludes_dismissed(db: Database, mock_edit: AsyncMock) -> None:
    """Dismissed notifications must not appear in the inbox (RULES §4 / §7)."""
    nid = await _seed_notifications(db, 1001, 1)
    await repo.dismiss_notification(db, nid[0], 1001)
    cb = make_cb(C.NOTIFY, user_id=1001)
    await cb_notify_list(cb, db=db)
    cb.answer.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert M_NOTIFY_EMPTY in text  # inbox is empty because the only notif was dismissed


async def test_cb_notify_read_scoped_to_owner(db: Database, mock_edit: AsyncMock) -> None:
    """An admin cannot mark-read a notification that belongs to another admin."""
    nid = await repo.create_notification(db, owner_id=1001, event_type="x", title="t", body="b")
    cb = make_cb(f"{C.NOTIFY_READ}{nid}", user_id=9999)  # foreign admin
    await cb_notify_read(cb, db=db)
    row = await db.fetch_one(
        "SELECT read_at FROM notifications WHERE id=?", (nid,)
    )
    assert row["read_at"] is None  # untouched — ownership enforced


async def test_cb_notify_dismiss_scoped_to_owner(db: Database, mock_edit: AsyncMock) -> None:
    """An admin cannot dismiss a notification that belongs to another admin."""
    nid = await repo.create_notification(db, owner_id=1001, event_type="x", title="t", body="b")
    cb = make_cb(f"{C.NOTIFY_DISMISS}{nid}", user_id=9999)  # foreign admin
    await cb_notify_dismiss(cb, db=db)
    row = await db.fetch_one(
        "SELECT dismissed FROM notifications WHERE id=?", (nid,)
    )
    assert row["dismissed"] == 0  # untouched — ownership enforced


async def test_cb_notify_list_with_pagination(db: Database, mock_edit: AsyncMock) -> None:
    for i in range(15):
        await repo.create_notification(
            db, owner_id=1001, event_type="user_joined", title=f"t{i}", body="b",
        )
    cb = make_cb(f"{C.NOTIFY_PAGE}1", user_id=1001)
    await cb_notify_page(cb, db=db)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()


async def test_cb_notify_read_marks_and_relists(db: Database, mock_edit: AsyncMock) -> None:
    notif_id = await _seed_notifications(db, 1001, 1)
    cb = make_cb(f"{C.NOTIFY_READ}{notif_id[0]}", user_id=1001)
    await cb_notify_read(cb, db=db)
    cb.answer.assert_awaited_once()
    row = await db.fetch_one(
        "SELECT read_at FROM notifications WHERE id=?", (notif_id[0],)
    )
    assert row["read_at"] is not None


async def test_cb_notify_read_malformed_id(db: Database, mock_edit: AsyncMock) -> None:
    cb = make_cb(f"{C.NOTIFY_READ}abc", user_id=1001)
    await cb_notify_read(cb, db=db)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()


async def test_cb_notify_mark_all(db: Database, mock_edit: AsyncMock) -> None:
    await _seed_notifications(db, 1001, 3)
    cb = make_cb(C.NOTIFY_MARK_ALL, user_id=1001)
    await cb_notify_mark_all(cb, db=db)
    cb.answer.assert_awaited_once()
    count = await repo.count_unread_notifications(db, 1001)
    assert count == 0


async def test_cb_notify_dismiss(db: Database, mock_edit: AsyncMock) -> None:
    notif_id = await _seed_notifications(db, 1001, 1)
    cb = make_cb(f"{C.NOTIFY_DISMISS}{notif_id[0]}", user_id=1001)
    await cb_notify_dismiss(cb, db=db)
    cb.answer.assert_awaited_once()
    row = await db.fetch_one(
        "SELECT dismissed FROM notifications WHERE id=?", (notif_id[0],)
    )
    assert row["dismissed"] == 1


async def test_cb_notify_dismiss_malformed(db: Database, mock_edit: AsyncMock) -> None:
    cb = make_cb(f"{C.NOTIFY_DISMISS}xyz", user_id=1001)
    await cb_notify_dismiss(cb, db=db)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()


async def test_cb_notify_settings_shows_toggles(db: Database, config: Config, mock_edit: AsyncMock) -> None:
    cb = make_cb(f"{C.NOTIFY}:settings", user_id=1001)
    await cb_notify_settings(cb, db=db, config=config)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()
    text = mock_edit.call_args.args[1]
    assert "user_joined" in text


async def test_cb_notify_toggle_flips_setting(db: Database, config: Config, mock_edit: AsyncMock) -> None:
    cb = make_cb(f"{C.NOTIFY_TOGGLE}user_joined", user_id=1001)
    await cb_notify_toggle(cb, db=db, config=config)
    cb.answer.assert_awaited_once()
    enabled = await repo.is_notification_enabled(db, 1001, "user_joined")
    assert enabled is False


async def test_cb_notify_toggle_unknown_type(db: Database, config: Config, mock_edit: AsyncMock) -> None:
    cb = make_cb(f"{C.NOTIFY_TOGGLE}bogus_type", user_id=1001)
    await cb_notify_toggle(cb, db=db, config=config)
    cb.answer.assert_awaited_once()
    mock_edit.assert_awaited_once()