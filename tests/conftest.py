"""Shared offline fixtures: temp database and session crypto.

No network access anywhere — everything runs against a tmp_path SQLite file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
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
