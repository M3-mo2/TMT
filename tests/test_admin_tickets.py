"""Tests for the admin ticket router: callback constants, keyboards, handler logic."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    admin_ticket_detail_kb,
    admin_tickets_list_kb,
    ticket_status_change_kb,
    tickets_status_filter_kb,
)
from app.config import Config
from app.db import repositories as repo
from app.db.database import Database


def _config() -> Config:
    return Config(
        bot_token="123456:test-token",
        api_id=1,
        api_hash="0123456789abcdef0123456789abcdef",
        admin_ids="1001",
    )


async def _add_user(db: Database, user_id: int) -> None:
    await repo.upsert_user(db, user_id, first_name="Admin")


def _make_cb(data: str, user_id: int = 1001) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.id = "test-cb"
    cb.from_user = SimpleNamespace(id=user_id, is_bot=False, first_name="Admin")
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.chat = SimpleNamespace(id=user_id, type="private")
    return cb


@pytest.fixture
def fsm_context() -> FSMContext:
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=StorageKey(chat_id=1001, user_id=1001, bot_id=1))


# ---------------------------------------------------------------- callback constants


class TestAdminTicketConstants:
    def test_constants_have_prefix(self) -> None:
        assert C.TICKETS == "adm:tickets"
        assert C.TICKETS_ALL.startswith("adm:tickets")
        assert C.TICKETS_OPEN.startswith("adm:tickets")
        assert C.TICKETS_OPEN_TICKET.startswith("adm:tickets")
        assert C.TICKETS_STATUS.startswith("adm:tickets")
        assert C.TICKETS_PRIORITY.startswith("adm:tickets")
        assert C.TICKETS_REPLY.startswith("adm:tickets")
        assert C.TICKETS_CLOSE.startswith("adm:tickets")

    def test_open_ticket_prefix_carries_id(self) -> None:
        assert C.TICKETS_OPEN_TICKET + "42" == "adm:tickets:open:42"

    def test_status_prefix_carries_id(self) -> None:
        assert C.TICKETS_STATUS + "42" == "adm:tickets:status:42"

    def test_priority_prefix_carries_id(self) -> None:
        assert C.TICKETS_PRIORITY + "42" == "adm:tickets:priority:42"


# ---------------------------------------------------------------- keyboards


class TestAdminTicketKeyboards:
    def test_status_filter_kb_has_all_options(self) -> None:
        kb = tickets_status_filter_kb(C.TICKETS_ALL)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert C.TICKETS_ALL in flat
        assert C.TICKETS_OPEN in flat
        assert C.TICKETS_IN_PROGRESS in flat
        assert C.TICKETS_CLOSED in flat
        assert C.MENU in flat  # back to menu

    def test_status_filter_kb_marks_current(self) -> None:
        kb = tickets_status_filter_kb(C.TICKETS_OPEN)
        rows = kb.inline_keyboard
        # Second row ("مفتوحة") should have the ✓ marker, first row ("كل") should not
        assert not any("✓" in b.text for b in rows[0])
        assert any("✓" in b.text for b in rows[1])

    def test_admin_tickets_list_kb_has_view_buttons(self) -> None:
        tickets = [{"id": 1}, {"id": 2}]
        kb = admin_tickets_list_kb(tickets)
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.TICKETS_OPEN_TICKET}1" in flat
        assert f"{C.TICKETS_OPEN_TICKET}2" in flat

    def test_admin_ticket_detail_kb_has_actions(self) -> None:
        from app.bot.texts import BUT_CLOSE, BUT_TICKET_DETAIL
        kb = admin_ticket_detail_kb(42, "open")
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.TICKETS_STATUS}42" in flat  # status change
        assert f"{C.TICKETS_PRIORITY}42:low" in flat  # low priority
        assert f"{C.TICKETS_PRIORITY}42:normal" in flat  # normal priority
        assert f"{C.TICKETS_PRIORITY}42:high" in flat  # high priority
        assert f"{C.TICKETS_REPLY}42" in flat  # reply to user
        assert f"{C.TICKETS_CLOSE}42" in flat  # close
        assert f"{C.TICKETS_OPEN_TICKET}42" in flat  # back to detail
        # button labels
        texts_flat = [b.text for row in kb.inline_keyboard for b in row]
        assert BUT_CLOSE in texts_flat
        assert BUT_TICKET_DETAIL in texts_flat

    def test_ticket_status_change_kb_lists_statuses(self) -> None:
        kb = ticket_status_change_kb(99, "open")
        flat = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"{C.TICKETS_STATUS}99:open" in flat
        assert f"{C.TICKETS_STATUS}99:in_progress" in flat
        assert f"{C.TICKETS_STATUS}99:resolved" in flat
        assert f"{C.TICKETS_STATUS}99:closed" in flat


# ---------------------------------------------------------------- handler: list + filter


class TestAdminTicketHandlers:
    async def test_list_all_tickets(self, db: Database) -> None:
        await _add_user(db, 1001)
        await _add_user(db, 1002)
        cb = _make_cb(C.TICKETS)
        # Create tickets for different owners
        await repo.create_ticket(db, owner_id=1001, subject="T1")
        await repo.create_ticket(db, owner_id=1002, subject="T2")
        from app.bot.routers.admin.tickets import cb_tickets_list
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_tickets_list(cb, db)
            assert cb.answer.called
            assert m.called
            args, kwargs = m.call_args
            text = args[1]
            assert "#1" in text
            assert "#2" in text

    async def test_filter_by_status(self, db: Database) -> None:
        await _add_user(db, 1001)
        t1 = await repo.create_ticket(db, owner_id=1001, subject="T1")  # open
        t2 = await repo.create_ticket(db, owner_id=1001, subject="T2")  # open
        await repo.update_ticket_status(db, t1, "closed")
        cb = _make_cb(C.TICKETS_CLOSED)
        from app.bot.routers.admin.tickets import cb_tickets_filter
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_tickets_filter(cb, db)
            text = m.call_args[0][1]
            assert "#1" in text  # closed ticket
            assert "#2" not in text  # open ticket not in closed list

    async def test_empty_filter_shows_no_tickets(self, db: Database) -> None:
        await _add_user(db, 1001)
        cb = _make_cb(C.TICKETS_ALL)
        from app.bot.routers.admin.tickets import cb_tickets_filter
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_tickets_filter(cb, db)
            text = m.call_args[0][1]
            assert "لا توجد تذاكر" in text

    async def test_detail_shows_thread(self, db: Database) -> None:
        await _add_user(db, 1001)
        ticket_id = await repo.create_ticket(db, owner_id=1001, subject="Test ticket")
        await repo.create_ticket_message(db, ticket_id=ticket_id, sender_id=1001, sender_role="user", body="Hello")
        cb = _make_cb(f"{C.TICKETS_OPEN_TICKET}{ticket_id}")
        from app.bot.routers.admin.tickets import cb_ticket_detail
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_ticket_detail(cb, db)
            text = m.call_args[0][1]
            assert "#1" in text
            assert "Test ticket" in text
            assert "Hello" in text

    async def test_detail_ticket_not_found(self, db: Database) -> None:
        await _add_user(db, 1001)
        cb = _make_cb(f"{C.TICKETS_OPEN_TICKET}99999")
        from app.bot.routers.admin.tickets import cb_ticket_detail
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_ticket_detail(cb, db)
            text = m.call_args[0][1]
            assert "لم يتم العثور" in text

    async def test_change_status(self, db: Database) -> None:
        await _add_user(db, 1001)
        t = await repo.create_ticket(db, owner_id=1001, subject="T")
        cb = _make_cb(f"{C.TICKETS_STATUS}{t}:resolved")
        from app.bot.routers.admin.tickets import cb_ticket_status
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_ticket_status(cb, db)
            assert cb.answer.called
            cb.answer.assert_awaited()
        row = await db.fetch_one("SELECT status FROM tickets WHERE id=?", (t,))
        assert row["status"] == "resolved"

    async def test_change_priority(self, db: Database) -> None:
        await _add_user(db, 1001)
        t = await repo.create_ticket(db, owner_id=1001, subject="T")
        cb = _make_cb(f"{C.TICKETS_PRIORITY}{t}:high")
        from app.bot.routers.admin.tickets import cb_ticket_priority
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock):
            await cb_ticket_priority(cb, db)
        row = await db.fetch_one("SELECT priority FROM tickets WHERE id=?", (t,))
        assert row["priority"] == "high"

    async def test_close_ticket(self, db: Database) -> None:
        await _add_user(db, 1001)
        t = await repo.create_ticket(db, owner_id=1001, subject="T")
        cb = _make_cb(f"{C.TICKETS_CLOSE}{t}")
        from app.bot.routers.admin.tickets import cb_ticket_close
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock):
            await cb_ticket_close(cb, db)
        row = await db.fetch_one("SELECT status FROM tickets WHERE id=?", (t,))
        assert row["status"] == "closed"

    async def test_status_picker_shown_when_no_value(self, db: Database) -> None:
        await _add_user(db, 1001)
        t = await repo.create_ticket(db, owner_id=1001, subject="T")
        cb = _make_cb(f"{C.TICKETS_STATUS}{t}")
        from app.bot.routers.admin.tickets import cb_ticket_status
        with patch("app.bot.routers.admin.tickets.edit_or_answer", new_callable=AsyncMock) as m:
            await cb_ticket_status(cb, db)
            # Should show the status picker (ticket_status_change_kb)
            kb = m.call_args[0][2] if len(m.call_args[0]) > 2 else m.call_args[1].get("reply_markup")
            assert kb is not None
            flat = [b.callback_data for row in kb.inline_keyboard for b in row]
            assert f"{C.TICKETS_STATUS}{t}:open" in flat
            assert f"{C.TICKETS_STATUS}{t}:closed" in flat
