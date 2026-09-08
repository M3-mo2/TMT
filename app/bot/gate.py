"""Mandatory-subscription gate logic — shared between middleware and handlers.

Centralising the membership check here avoids duplicating the Telegram
API call (and its error handling) in both the ``UserGateMiddleware`` and the
``gate:verify`` handler in ``common.py``.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["check_membership"]


async def check_membership(bot: Any, user_id: int, mandatory: list[dict[str, Any]]) -> bool:
    """Return ``True`` when *user_id* is a member of every entry in *mandatory*.

    A member check that cannot be performed (e.g. the bot was removed as admin
    from a channel) is **skipped** — logged as a warning — rather than blocking
    the user outright.  This "honesty" policy means a misconfigured mandatory
    set degrades to open-access instead of locking everyone out (RULES §3:
    never lose data silently — the warning is logged for the operator).
    """
    for entry in mandatory:
        try:
            member = await bot.get_chat_member(
                chat_id=entry["channel_id"], user_id=user_id,
            )
        except Exception:
            logger.warning(
                "Cannot verify membership for channel_id=%s user_id=%d — skipping",
                entry["channel_id"], user_id, exc_info=True,
            )
            continue
        if member.status not in ("creator", "administrator", "member", "restricted"):
            return False
    return True
