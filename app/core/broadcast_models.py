"""Domain vocabulary for the Broadcast Campaign Engine (BroadcastEngine.md §2–§4).

Shared by every layer that deals with broadcasts.  Depends only on the
standard library — no ``bot/``, ``tg/``, or ``db/`` imports (RULES §1).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum


class ErrorKind(str, Enum):
    """Outcome of a single recipient send, mapped from a ``TelegramAPIError``."""

    RETRY_FLOOD = "retry_flood"
    RETRY_DELAYED = "retry_delayed"
    RETRY_TRANSIENT = "retry_transient"
    PERMANENT_BLOCKED = "permanent_blocked"
    PERMANENT_FAIL = "permanent_fail"


class BroadcastStatus(str, Enum):
    """Campaign lifecycle states (BroadcastEngine.md §2.1).

    String values match the DB ``status`` column exactly.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RecipientStatus(str, Enum):
    """Per-recipient delivery status (BroadcastEngine.md §2.2).

    ``delivered`` is the post-send terminal state for a successful copy.
    """

    PENDING = "pending"
    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"
    DELIVERED = "delivered"


class BroadcastMode(str, Enum):
    """How a campaign delivers its message (BroadcastEngine.md §2.8)."""

    COPY = "copy"
    PERSONALIZED = "personalized"


@dataclass(slots=True)
class AudienceFilter:
    """Segmentation criteria for resolving a campaign audience.

    ``default()`` returns the all-users / no-filter configuration.  The
    filter is serializable via ``to_dict`` / ``from_dict`` for FSM state
    persistence (Phases.md §Phase 4).
    """

    target: str = "all"  # 'all' | 'active' | 'inactive' | 'blocked'
    with_accounts: bool = False
    without_accounts: bool = False
    account_count_min: int | None = None
    account_count_max: int | None = None
    registered_days_ago: int | None = None
    last_seen_days_ago: int | None = None
    exclude_admins: bool = False
    exclude_previously_contacted: bool = False

    @classmethod
    def default(cls) -> AudienceFilter:
        """All users, no extra filters."""
        return cls()

    def to_dict(self) -> dict[str, object]:
        """Flat dict suitable for FSM / JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AudienceFilter:
        """Rebuild from a dict, ignoring unknown keys."""
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})
