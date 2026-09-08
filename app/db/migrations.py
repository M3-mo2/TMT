"""Append-only schema migrations. Never edit an applied migration (RULES §6)."""

from __future__ import annotations

import sqlite3

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

_V2 = """
-- v2: make the jobs -> accounts link nullable with ON DELETE SET NULL so
-- deleting an account keeps its job history (PRD §20: the jobs table is the
-- future admin panel's statistics backbone). SQLite cannot alter an FK in
-- place, so the table is rebuilt; requires PRAGMA foreign_keys=ON (set by
-- Database.connect) for the SET NULL action to fire.
CREATE TABLE jobs_v2 (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NULL REFERENCES accounts(id) ON DELETE SET NULL,
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

INSERT INTO jobs_v2 (
    id, owner_id, account_id, source_ref, dest_ref, source_title, dest_title,
    status, status_detail, total, invited, skipped, failed, skip_reasons,
    error, created_at, started_at, finished_at, cancel_requested
)
SELECT
    id, owner_id, account_id, source_ref, dest_ref, source_title, dest_title,
    status, status_detail, total, invited, skipped, failed, skip_reasons,
    error, created_at, started_at, finished_at, cancel_requested
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_v2 RENAME TO jobs;

CREATE INDEX idx_jobs_owner_status ON jobs(owner_id, status);
CREATE INDEX idx_jobs_account_status ON jobs(account_id, status);
"""

_V3 = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    invite_link TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
"""

# v4: capture the Telegram user's display name + username so the admin
# panel can list users by name (not just by numeric id) and identify them.
# Nullable for existing rows (names were never stored before → UI falls back
# to the numeric id). Standard ALTER TABLE (portable to PostgreSQL, RULES §6).
_V4 = """
ALTER TABLE users ADD COLUMN first_name TEXT;
ALTER TABLE users ADD COLUMN last_name TEXT;
ALTER TABLE users ADD COLUMN username TEXT;
"""

# v5: Broadcast Campaign Engine — campaigns, per-recipient delivery rows,
# and exclusion lists (BroadcastEngine.md §2). All FKs reference the
# pre-existing `users` table (V1). Plain `PRIMARY KEY` (no AUTOINCREMENT,
# RULES §6). ON CONFLICT DO NOTHING is standard SQL upsert (portable).
_V5 = """
CREATE TABLE broadcasts (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES users(id),
    label TEXT NOT NULL DEFAULT '',
    source_chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'copy',
    content_html TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_for TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total_recipients INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    avg_rate REAL,
    error TEXT
);

CREATE TABLE broadcast_recipients (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TEXT,
    sent_at TEXT,
    UNIQUE(broadcast_id, user_id)
);

CREATE TABLE broadcast_exclusions (
    broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    UNIQUE(broadcast_id, user_id)
);

CREATE INDEX idx_bcast_status ON broadcasts(status);
CREATE INDEX idx_brec_status ON broadcast_recipients(broadcast_id, status);
CREATE INDEX idx_brec_user ON broadcast_recipients(user_id);
"""

# v6: persist the resolved audience filter (as JSON) on the campaign so that
# ``Broadcaster.recover()`` can resume an interrupted campaign without the
# caller re-supplying the audience.  Standard ALTER TABLE — portable to
# PostgreSQL (RULES §6).
_V6 = """
ALTER TABLE broadcasts ADD COLUMN filter_json TEXT;
"""

# v7: Phase 5 — draft_data and recurrence_rule for scheduling/drafts.
# draft_data stores the serialized form state (Phase 4 FSM snapshot).
# recurrence_rule stores a recurrence definition (e.g. "daily").
_V7 = """
ALTER TABLE broadcasts ADD COLUMN draft_data TEXT;
ALTER TABLE broadcasts ADD COLUMN recurrence_rule TEXT;
"""

# v8: Phase 6 — A/B testing tables and the ab_test_id FK on broadcasts.
_V8 = """
CREATE TABLE ab_tests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE broadcast_templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    content_html TEXT NOT NULL,
    parse_mode TEXT NOT NULL DEFAULT 'HTML',
    is_personalized INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

ALTER TABLE broadcasts ADD COLUMN ab_test_id INTEGER REFERENCES ab_tests(id);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
    (2, _V2),
    (3, _V3),
    (4, _V4),
    (5, _V5),
    (6, _V6),
    (7, _V7),
    (8, _V8),
]


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements.

    `executescript()` commits any pending transaction before running, which
    would break the explicit BEGIN IMMEDIATE below — so we execute statements
    one by one instead. `sqlite3.complete_statement` keeps triggers
    (BEGIN...END bodies with inner semicolons) intact.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if buf.strip() and sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        statements.append(buf.strip())
    return statements


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
            for statement in _split_statements(sql):
                await conn.execute(statement)
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
