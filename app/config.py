"""Application configuration.

All environment access happens here (pydantic-settings). Every other module
receives a `Config` instance — see RULES.md §8.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    bot_token: str = Field(min_length=10)
    api_id: int
    api_hash: str = Field(min_length=10)
    admin_ids: str = Field(default="")

    data_dir: Path = Path("data")
    sessions_master_key: str | None = None
    log_level: str = "INFO"

    max_concurrent_jobs: int = Field(default=3, ge=1)
    max_running_jobs_per_user: int = Field(default=1, ge=1)
    max_members_per_job: int = Field(default=2000, ge=1)
    invite_delay_seconds: float = Field(default=2.0, ge=0)
    invite_delay_jitter_seconds: float = Field(default=1.0, ge=0)
    flood_wait_max_seconds: int = Field(default=900, ge=1)
    peer_flood_cooldown_seconds: int = Field(default=3600, ge=0)
    job_timeout_seconds: int = Field(default=4 * 3600, ge=60)
    login_ttl_seconds: int = Field(default=600, ge=60)
    progress_edit_min_interval: float = Field(default=4.0, ge=1)

    # Broadcast Campaign Engine (BroadcastEngine.md §4)
    max_bcast_concurrency: int = Field(default=10, ge=1)
    bcast_max_rate_per_second: int = Field(default=25, ge=1)
    bcast_flood_retry_threshold: int = Field(default=60, ge=1)
    bcast_retry_attempts: int = Field(default=3, ge=1)
    bcast_retry_backoff_base: float = Field(default=2.0, ge=0.0)
    bcast_edit_interval: float = Field(default=3.0, ge=0.1)
    bcast_batch_size: int = Field(default=50, ge=1)
    bcast_sweep_interval: int = Field(default=30, ge=5)

    # Notification System — system-event alerts to bot operators (docs/notifications/)
    notify_on_user_join: bool = True       # DM every admin when a new user starts the bot
    notify_on_job_events: bool = True      # DM admins on job lifecycle (started/completed/failed/cancelled)
    notify_on_error: bool = True           # DM admins on FloodWait / PeerFlood / unexpected errors
    notify_on_broadcast_events: bool = True  # DM admins on broadcast lifecycle

    @model_validator(mode="after")
    def _normalize(self) -> Config:
        object.__setattr__(self, "data_dir", self.data_dir.expanduser().resolve())
        object.__setattr__(self, "log_level", self.log_level.upper())
        return self

    @property
    def admin_id_list(self) -> list[int]:
        """Returns parsed admin IDs from the comma-separated admin_ids string."""
        return [int(pid.strip()) for pid in self.admin_ids.split(",") if pid.strip()]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def key_file(self) -> Path:
        return self.data_dir / "keys" / "master.key"


def load_config() -> Config:
    """Load and validate configuration; raises a readable error on failure."""
    try:
        return Config()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError + pydantic_settings errors
        raise SystemExit(
            "Invalid configuration. Copy .env.example to .env, fill BOT_TOKEN, "
            f"API_ID and API_HASH. Details: {exc}"
        ) from exc
