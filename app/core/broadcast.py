"""AudienceResolver — single-query audience segmentation (BroadcastEngine.md §2.6).

Translates an :class:`AudienceFilter` into one SQL statement that returns
user IDs for a broadcast campaign.  ``core/`` may import ``app.db`` and
``app.core.models`` but not ``app.bot`` or ``app.tg`` (RULES §1).
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.core.broadcast_models import AudienceFilter
from app.db.database import Database

logger = logging.getLogger("app.core.broadcast")

# Sentinel for the "no conditions" case — keeps the assembled SQL readable.
_WHERE_PREFIX = " WHERE "


def _build_filter(
    filters: AudienceFilter, admin_ids: Sequence[int]
) -> tuple[str, list]:
    """Build the WHERE fragment + params list from an ``AudienceFilter``.

    Filters are accumulated as ``(sql_fragment, params)`` tuples and then
    flattened into a single ``AND``-joined clause, keeping parameter order
    deterministic.  No Python-side filtering of fetched rows (RULES §8:
    one DB round-trip per call).
    """
    conditions: list[tuple[str, list]] = []

    # --- target -----------------------------------------------------------
    # 'all' and unknown targets: exclude blocked users (they can't receive
    # messages).  'blocked' is a deliberate opt-in to target only them.
    if filters.target == "blocked":
        conditions.append(("u.is_blocked = 1", []))
    else:
        conditions.append(("u.is_blocked = 0", []))
        if filters.target == "active":
            n = filters.last_seen_days_ago or 30
            conditions.append(("u.updated_at > datetime('now', ?)", [f"-{n} days"]))
        elif filters.target == "inactive":
            n = filters.last_seen_days_ago or 30
            conditions.append(("u.updated_at <= datetime('now', ?)", [f"-{n} days"]))

    # --- standalone last_seen_days_ago (only when target didn't handle it) -
    if (
        filters.last_seen_days_ago is not None
        and filters.target not in ("active", "inactive")
    ):
        conditions.append(
            ("u.updated_at > datetime('now', ?)", [f"-{filters.last_seen_days_ago} days"])
        )

    # --- account ownership ------------------------------------------------
    if filters.with_accounts:
        conditions.append(("EXISTS(SELECT 1 FROM accounts WHERE owner_id=u.id)", []))
    if filters.without_accounts:
        conditions.append(("NOT EXISTS(SELECT 1 FROM accounts WHERE owner_id=u.id)", []))

    # --- account count bounds --------------------------------------------
    if filters.account_count_min is not None:
        conditions.append(
            ("(SELECT COUNT(*) FROM accounts WHERE owner_id=u.id) >= ?", [filters.account_count_min])
        )
    if filters.account_count_max is not None:
        conditions.append(
            ("(SELECT COUNT(*) FROM accounts WHERE owner_id=u.id) <= ?", [filters.account_count_max])
        )

    # --- registration age -------------------------------------------------
    if filters.registered_days_ago is not None:
        conditions.append(
            ("u.created_at > datetime('now', ?)", [f"-{filters.registered_days_ago} days"])
        )

    # --- admin exclusion --------------------------------------------------
    if filters.exclude_admins and admin_ids:
        placeholders = ",".join(["?"] * len(admin_ids))
        conditions.append((f"u.id NOT IN ({placeholders})", list(admin_ids)))

    # --- previously contacted --------------------------------------------
    if filters.exclude_previously_contacted:
        conditions.append(
            (
                "NOT EXISTS(SELECT 1 FROM broadcast_recipients br "
                "JOIN broadcasts b ON br.broadcast_id=b.id "
                "WHERE br.user_id=u.id AND b.mode IS NOT NULL "
                "AND br.status IN ('sent','delivered'))",
                [],
            )
        )

    # --- assemble ---------------------------------------------------------
    if conditions:
        fragments = [frag for frag, _ in conditions]
        params: list = []
        for _, frag_params in conditions:
            params.extend(frag_params)
        where = _WHERE_PREFIX + " AND ".join(fragments)
    else:
        where = ""
        params = []

    return where, params


async def resolve_audience(
    db: Database, filters: AudienceFilter, admin_ids: Sequence[int]
) -> list[int]:
    """Return sorted user IDs matching *filters* in a single DB round-trip."""
    where, params = _build_filter(filters, admin_ids)
    sql = (
        "SELECT DISTINCT u.id FROM users u "
        "LEFT JOIN accounts a ON a.owner_id = u.id"
        f"{where} ORDER BY u.id"
    )
    rows = await db.fetch_all(sql, params)
    ids = [int(r["id"]) for r in rows]
    logger.debug("resolve_audience: %d users matched (target=%s)", len(ids), filters.target)
    return ids


async def count_audience(
    db: Database, filters: AudienceFilter, admin_ids: Sequence[int]
) -> int:
    """Count users matching *filters* — mirrors :func:`resolve_audience`."""
    where, params = _build_filter(filters, admin_ids)
    sql = (
        "SELECT COUNT(DISTINCT u.id) AS c FROM users u "
        "LEFT JOIN accounts a ON a.owner_id = u.id"
        f"{where}"
    )
    row = await db.fetch_one(sql, params)
    return int(row["c"]) if row else 0
