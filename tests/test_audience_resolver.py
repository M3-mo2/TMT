"""Offline tests for ``app.core.broadcast`` — AudienceResolver segmentation.

Each test seeds a known user roster into the ``db`` fixture and verifies
that ``resolve_audience`` / ``count_audience`` produce the expected IDs
with a **single DB round-trip** (no Python-side filtering).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.core.broadcast import count_audience, resolve_audience
from app.core.broadcast_models import AudienceFilter
from app.db.database import Database
from app.db.repositories import now_iso

# --------------------------------------------------------------------------- #
# Seed helpers
# --------------------------------------------------------------------------- #

_ADMIN_IDS: list[int] = [5]


def _iso_days_ago(n: int) -> str:
    """ISO-UTC timestamp ``n`` days in the past (matches ``now_iso`` format)."""
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed_user(
    db: Database,
    user_id: int,
    *,
    blocked: bool = False,
    updated_days_ago: int = 5,
    created_days_ago: int = 60,
) -> None:
    await db.execute(
        "INSERT INTO users (id, first_name, last_name, username, "
        "created_at, updated_at, is_blocked) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            f"User{user_id}",
            "",
            f"user{user_id}",
            _iso_days_ago(created_days_ago),
            _iso_days_ago(updated_days_ago),
            1 if blocked else 0,
        ),
    )


async def _seed_account(db: Database, owner_id: int, tg_user_id: int) -> None:
    await db.execute(
        "INSERT INTO accounts (owner_id, phone, tg_user_id, display_name, "
        "session_encrypted, status, added_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (owner_id, f"+1555{owner_id}{tg_user_id}", tg_user_id, "TG", "enc", now_iso()),
    )


async def _seed_previously_contacted(db: Database, user_id: int) -> int:
    """Create a past broadcast that marked *user_id* as ``sent``."""
    bcast_id = await db.execute(
        "INSERT INTO broadcasts "
        "(admin_id, label, source_chat_id, source_message_id, "
        "mode, status, created_at) VALUES (?, ?, ?, ?, 'copy', 'completed', ?)",
        (1, "seed-bcast", 100, 200, now_iso()),
    )
    await db.execute(
        "INSERT INTO broadcast_recipients (broadcast_id, user_id, status, sent_at) "
        "VALUES (?, ?, 'sent', ?)",
        (bcast_id, user_id, now_iso()),
    )
    return bcast_id


async def _seed_standard_roster(db: Database) -> None:
    """Users 1–6 with varied attributes (see table in docstring).

    | id | blocked | updated | created | accounts | admin | contacted |
    |----|---------|---------|---------|----------|-------|-----------|
    | 1  | no      | 5d ago  | 60d ago | 1        | no    | no        |
    | 2  | no      | 5d ago  | 60d ago | 3        | no    | no        |
    | 3  | yes     | 5d ago  | 60d ago | 0        | no    | no        |
    | 4  | no      | 60d ago | 60d ago | 0        | no    | no        |
    | 5  | no      | 5d ago  | 10d ago | 1        | yes   | no        |
    | 6  | no      | 5d ago  | 60d ago | 1        | no    | yes       |
    """
    await _seed_user(db, 1, updated_days_ago=5, created_days_ago=60)
    await _seed_user(db, 2, updated_days_ago=5, created_days_ago=60)
    await _seed_user(db, 3, blocked=True, updated_days_ago=5, created_days_ago=60)
    await _seed_user(db, 4, updated_days_ago=60, created_days_ago=60)
    await _seed_user(db, 5, updated_days_ago=5, created_days_ago=10)
    await _seed_user(db, 6, updated_days_ago=5, created_days_ago=60)
    # accounts
    await _seed_account(db, 1, 101)
    await _seed_account(db, 2, 201)
    await _seed_account(db, 2, 202)
    await _seed_account(db, 2, 203)
    await _seed_account(db, 5, 501)
    await _seed_account(db, 6, 601)
    # previously contacted
    await _seed_previously_contacted(db, 6)


# --------------------------------------------------------------------------- #
# Default filter
# --------------------------------------------------------------------------- #


async def test_default_filter_returns_all_non_blocked(db: Database) -> None:
    await _seed_standard_roster(db)
    result = await resolve_audience(db, AudienceFilter.default(), _ADMIN_IDS)
    assert result == [1, 2, 4, 5, 6]


# --------------------------------------------------------------------------- #
# Individual filter dimensions
# --------------------------------------------------------------------------- #


async def test_with_accounts(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(with_accounts=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 5, 6]


async def test_account_count_min(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(account_count_min=2)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [2]


async def test_target_blocked(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(target="blocked")
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [3]


async def test_exclude_admins(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(exclude_admins=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 4, 6]


async def test_exclude_previously_contacted(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(exclude_previously_contacted=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 4, 5]


async def test_last_seen_days_ago(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(last_seen_days_ago=7)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 5, 6]


async def test_registered_days_ago(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(registered_days_ago=30)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [5]


# --------------------------------------------------------------------------- #
# Combined filters
# --------------------------------------------------------------------------- #


async def test_combined_with_accounts_and_exclude_admins(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(with_accounts=True, exclude_admins=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 6]


async def test_combined_exclude_contacted_and_admins(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(exclude_previously_contacted=True, exclude_admins=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [1, 2, 4]


async def test_combined_last_seen_and_account_count(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(last_seen_days_ago=7, account_count_min=2)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    assert result == [2]  # only user 2 is recent AND has >=2 accounts


# --------------------------------------------------------------------------- #
# count_audience parity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "filters",
    [
        AudienceFilter.default(),
        AudienceFilter(with_accounts=True),
        AudienceFilter(account_count_min=2),
        AudienceFilter(account_count_max=1),
        AudienceFilter(target="blocked"),
        AudienceFilter(exclude_admins=True),
        AudienceFilter(exclude_previously_contacted=True),
        AudienceFilter(last_seen_days_ago=7),
        AudienceFilter(registered_days_ago=30),
        AudienceFilter(with_accounts=True, exclude_admins=True),
        AudienceFilter(last_seen_days_ago=7, account_count_min=2),
    ],
)
async def test_count_audience_matches_resolve(
    db: Database, filters: AudienceFilter
) -> None:
    await _seed_standard_roster(db)
    resolved = await resolve_audience(db, filters, _ADMIN_IDS)
    counted = await count_audience(db, filters, _ADMIN_IDS)
    assert counted == len(resolved)


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


async def test_empty_db_returns_empty(db: Database) -> None:
    result = await resolve_audience(db, AudienceFilter.default(), [])
    assert result == []
    assert await count_audience(db, AudienceFilter.default(), []) == 0


async def test_exclude_admins_empty_list(db: Database) -> None:
    """With an empty admin_ids list, exclude_admins is a no-op."""
    await _seed_standard_roster(db)
    filters = AudienceFilter(exclude_admins=True)
    result = await resolve_audience(db, filters, [])
    # Same as default — no admins to exclude
    assert result == await resolve_audience(db, AudienceFilter.default(), [])


async def test_account_count_max(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(account_count_max=1)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    # Non-blocked users with <=1 account: 1 (1 acct), 5 (1 acct), 6 (1 acct)
    # User 2 has 3 accounts, excluded. User 4 has 0 accounts, included.
    assert result == [1, 4, 5, 6]


async def test_without_accounts(db: Database) -> None:
    await _seed_standard_roster(db)
    filters = AudienceFilter(without_accounts=True)
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    # Non-blocked users with no accounts: user 4
    assert result == [4]


async def test_target_active_uses_last_seen_default(db: Database) -> None:
    """target=active without explicit last_seen_days_ago defaults to 30 days."""
    await _seed_standard_roster(db)
    filters = AudienceFilter(target="active")
    result = await resolve_audience(db, filters, _ADMIN_IDS)
    # All non-blocked users with updated_at within last 30 days:
    # users 1, 2, 5, 6 (updated 5 days ago) — user 4 is 60 days ago
    assert result == [1, 2, 5, 6]


async def test_audience_filter_round_trip() -> None:
    """to_dict / from_dict preserves all fields."""
    f = AudienceFilter(
        target="active",
        with_accounts=True,
        account_count_min=2,
        account_count_max=5,
        registered_days_ago=30,
        last_seen_days_ago=7,
        exclude_admins=True,
        exclude_previously_contacted=True,
    )
    restored = AudienceFilter.from_dict(f.to_dict())
    assert restored == f


async def test_audience_filter_default_values() -> None:
    """default() returns all-users / no filters."""
    f = AudienceFilter.default()
    assert f.target == "all"
    assert f.with_accounts is False
    assert f.without_accounts is False
    assert f.account_count_min is None
    assert f.account_count_max is None
    assert f.registered_days_ago is None
    assert f.last_seen_days_ago is None
    assert f.exclude_admins is False
    assert f.exclude_previously_contacted is False
