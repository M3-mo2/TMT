"""Domain vocabulary shared by every layer (PRD §10, §13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AccountStatus(str, Enum):
    ACTIVE = "active"
    UNAUTHORIZED = "unauthorized"
    LIMITED = "limited"


class JobStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


FINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }
)
ACTIVE_JOB_STATUSES = frozenset(
    {JobStatus.CREATED, JobStatus.VALIDATING, JobStatus.QUEUED, JobStatus.RUNNING}
)


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Check:
    """A single preflight inspection result (master prompt §10)."""

    key: str
    status: CheckStatus
    message: str  # Arabic, user-facing
    detail: str = ""  # technical, for logs only


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """A Telegram entity the user-account layer resolved and inspected."""

    id: int
    kind: str  # "chat" | "supergroup" | "channel" | "user"
    title: str
    username: str | None = None
    members_count: int | None = None
    is_broadcast: bool = False
    is_megagroup: bool = False
    is_member: bool | None = None  # None = not verifiable
    via_invite_link: bool = False  # resolved through t.me/+hash (preview only)
    raw_ref: str = ""

    @property
    def is_groupish(self) -> bool:
        return self.kind in ("chat", "supergroup") or (
            self.kind == "channel" and self.is_megagroup
        )


@dataclass(slots=True)
class Job:
    """In-memory view of a transfer job row."""

    id: int
    owner_id: int
    account_id: int
    source_ref: str
    dest_ref: str
    source_title: str = ""
    dest_title: str = ""
    status: JobStatus = JobStatus.CREATED
    status_detail: str | None = None
    total: int = 0
    invited: int = 0
    skipped: int = 0
    failed: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class Account:
    """In-memory view of an account row (session string not carried here)."""

    id: int
    owner_id: int
    phone: str
    tg_user_id: int
    tg_username: str | None
    display_name: str
    status: AccountStatus
    limited_until: str | None
    added_at: str
    last_validated_at: str | None


# Standard skip-reason keys (counted per job, rendered in Arabic by bot layer).
SKIP_BOT = "bot"
SKIP_ALREADY_MEMBER = "already_member"
SKIP_PRIVACY = "privacy"
SKIP_NOT_MUTUAL = "not_mutual"
SKIP_TOO_MANY = "channels_too_much"
SKIP_KICKED = "kicked"
SKIP_DELETED = "deleted_account"
SKIP_OTHER = "other"
