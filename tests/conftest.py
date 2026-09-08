"""Shared offline fixtures: temp database and session crypto.

No network access anywhere — everything runs against a tmp_path SQLite file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import CopyMessage, SendMessage
from cryptography.fernet import Fernet

from app.db.database import Database
from app.security.crypto import SessionCrypto


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Connected Database with migrations applied, backed by a temp file."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def crypto() -> SessionCrypto:
    return SessionCrypto(Fernet.generate_key())


@pytest.fixture
def db_row_factory_check(db: Database) -> Database:
    """aiosqlite must return named-Row objects so repositories can use row["col"]."""
    assert db.conn.row_factory is aiosqlite.Row
    return db


class FakeBroadcastBot:
    """Scriptable bot double — created fresh per test via ``bot_factory``."""

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
