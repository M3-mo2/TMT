"""Append-only schema migrations. Never edit an applied migration (RULES §6)."""

from __future__ import annotations

import aiosqlite

_V1 = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    phone TEXT NOT NULL,
    tg_user_id INTEGER NOT NULL,
    tg_username TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    session_encrypted TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    limited_until TEXT,
    added_at TEXT NOT NULL,
    last_validated_at TEXT,
    UNIQUE(owner_id, tg_user_id)
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    source_ref TEXT NOT NULL,
    dest_ref TEXT NOT NULL,
    source_title TEXT NOT NULL DEFAULT '',
    dest_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    status_detail TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    invited INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skip_reasons TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    owner_id INTEGER,
    account_id INTEGER,
    job_id INTEGER,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_accounts_owner ON accounts(owner_id);
CREATE INDEX idx_jobs_owner_status ON jobs(owner_id, status);
CREATE INDEX idx_jobs_account_status ON jobs(account_id, status);
CREATE INDEX idx_audit_ts ON audit_log(ts);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
]


async def apply_migrations(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    cur = await conn.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in await cur.fetchall()}

    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        else:
            await conn.execute("COMMIT")
