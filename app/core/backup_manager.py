"""Backup management service (admin-side DB snapshots).

Mirrors :class:`~app.core.broadcast.Broadcaster` lifecycle: a constructor that
takes only ``db``, ``config``, ``bus`` and ``data_dir`` — ``core/`` may import
``db/`` and ``core/`` but not ``bot/`` or ``tg/`` (RULES §1); a background
sweeper that promotes due scheduled snapshots; ``recover()`` on boot; and
``shutdown()`` cleanup.

Backups are **serial** (single ``asyncio.Lock``): they are DB-file I/O bound
and must not run in parallel. The snapshot/restore use the SQLite backup API on
a fresh ``sqlite3.Connection`` opened on the live DB file (never the aiosqlite
live connection), driven through ``asyncio.to_thread`` so the event loop stays
responsive (RULES §6: no SQLite-isms in SQL).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Config
from app.core.events import EventBus, SystemEvent
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger("app.core.backup_manager")

# Backup statuses that are final/terminal for the resume guard.
_FINAL_BACKUP_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Backup statuses that may be cancelled.
_CANCELABLE_BACKUP_STATUSES = frozenset({"pending", "scheduled"})
# Backup statuses that may be resumed via "run now".
_RESUMABLE_BACKUP_STATUSES = frozenset({"pending", "scheduled", "failed", "cancelled"})

_RECOVER_ERROR = "تم إيقاف النسخة الاحتياطية بسبب إعادة تشغيل الخادم."


class BackupManager:
    """Admin-side backup engine: snapshot, schedule, cancel, restore, retain."""

    def __init__(
        self, db: Database, config: Config, bus: EventBus, data_dir: Path | str
    ) -> None:
        self._db = db
        self._config = config
        self._bus = bus
        self._backup_dir = Path(data_dir) / "backups"
        # The live DB file the aiosqlite connection serves (snapshotted/restored).
        self._live_db_path = Path(db.path)
        self._lock = asyncio.Lock()
        self._sweep_task: asyncio.Task[None] | None = None
        # Spawned one-shot workers, keyed by backup id, for cancellation/await.
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ public

    async def create_backup(
        self,
        label: str,
        kind: str = "manual",
        scheduled_for: str | None = None,
        admin_id: int | None = None,
        run_now: bool = False,
    ) -> int:
        """Persist a backup row and arm its run.

        ``run_now`` or a missing ``scheduled_for`` spawns an immediate worker
        (``run_backup``); a non-null ``scheduled_for`` leaves the row
        ``scheduled`` for the sweeper to promote when it is due."""
        bid = await repo.create_backup(
            self._db,
            label=label,
            kind=kind,
            scheduled_for=scheduled_for,
            admin_id=admin_id,
        )
        if run_now or scheduled_for is None:
            task = asyncio.create_task(self.run_backup(bid), name=f"backup-{bid}")
            self._tasks[bid] = task
            task.add_done_callback(self._tasks.pop)
        else:
            await self._publish_system_event(
                bid, admin_id, "backup_scheduled", "info",
                "⟡|تم جدولة نسخة احتياطية",
                f"⟡|النسخة <code>#{bid}</code> جدولة لـ <code>{scheduled_for}</code>.",
            )
        return bid

    async def run_backup(self, backup_id: int) -> None:
        """Snapshot the live DB into ``data/backups/<label>_<ts>.db``.

        Serialised by ``self._lock``; idempotent against a final/running state
        so a resumed backup can be re-run safely."""
        async with self._lock:
            backup = await repo.get_backup(self._db, backup_id)
            if backup is None:
                logger.warning("run_backup: backup %d not found", backup_id)
                return
            if backup["status"] in _FINAL_BACKUP_STATUSES or backup["status"] == "running":
                # Already done/running — don't re-snapshot.
                return
            owner = backup.get("created_by")
            await repo.set_backup_status(
                self._db, backup_id, "running", started_at=repo.now_iso()
            )
            await self._audit("backup_started", owner, {"backup_id": backup_id})
            await self._publish_system_event(
                backup_id, owner, "backup_started", "info",
                "⟡|بدأ النسخ الاحتياطي",
                f"⟡|النسخة <code>#{backup_id}</code> جاري التشغيل.",
            )
            try:
                file_path, size_bytes = await asyncio.to_thread(
                    self._snapshot, backup["label"]
                )
            except Exception as exc:  # noqa: BLE001 - surface to row + event, not raw trace
                logger.exception("backup %d snapshot failed", backup_id)
                await repo.set_backup_status(
                    self._db, backup_id, "failed",
                    error=str(exc), finished_at=repo.now_iso(),
                )
                await self._audit(
                    "backup_failed", owner,
                    {"backup_id": backup_id, "error": str(exc)},
                )
                await self._publish_system_event(
                    backup_id, owner, "backup_failed", "error",
                    "×|فشل النسخ الاحتياطي",
                    f"⟡|النسخة <code>#{backup_id}</code> فشلت، راجع السجلات.",
                )
                return
            await repo.set_backup_status(
                self._db, backup_id, "completed",
                file_path=file_path, size_bytes=size_bytes,
                finished_at=repo.now_iso(),
            )
            await self._audit(
                "backup_completed", owner,
                {"backup_id": backup_id, "file_path": file_path,
                 "size_bytes": size_bytes},
            )
            await self._publish_system_event(
                backup_id, owner, "backup_completed", "info",
                "✅|اكتمل النسخ الاحتياطي",
                f"⟡|النسخة <code>#{backup_id}</code> اكتملت "
                f"(<code>{size_bytes}</code> بايت).",
            )
            await self._apply_retention()

    async def cancel(self, backup_id: int) -> bool:
        """Cancel a ``pending``/``scheduled`` backup. Returns False for unknown
        or non-cancelable (running/completed/failed/cancelled) statuses."""
        backup = await repo.get_backup(self._db, backup_id)
        if backup is None or backup["status"] not in _CANCELABLE_BACKUP_STATUSES:
            return False
        await repo.set_backup_status(
            self._db, backup_id, "cancelled", finished_at=repo.now_iso()
        )
        await self._audit(
            "backup_cancelled", backup.get("created_by"), {"backup_id": backup_id}
        )
        await self._publish_system_event(
            backup_id, backup.get("created_by"), "backup_cancelled", "info",
            "↺|ألغيت نسخة احتياطية",
            f"⟡|النسخة <code>#{backup_id}</code> ألغيت بواسطة المشرف.",
        )
        return True

    async def resume(self, backup_id: int) -> bool:
        """Re-run a ``pending``/``scheduled``/``failed``/``cancelled`` backup."""
        backup = await repo.get_backup(self._db, backup_id)
        if backup is None or backup["status"] not in _RESUMABLE_BACKUP_STATUSES:
            return False
        task = asyncio.create_task(self.run_backup(backup_id), name=f"backup-{backup_id}")
        self._tasks[backup_id] = task
        task.add_done_callback(self._tasks.pop)
        return True

    async def wait_done(self, backup_id: int, timeout: float = 60.0) -> None:
        """Await a spawned worker (by id) so the caller can render a finished
        card.  No-op when the backup was never spawned (e.g. sweeper-driven)."""
        task = self._tasks.get(backup_id)
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("wait_done: backup %d still running after %ss", backup_id, timeout)

    async def restore(self, backup_id: int, admin_id: int) -> str:
        """Overwrite the live DB file in place with ``backup_id``'s dump.

        Returns one of: ``restored``, ``not_found``, ``not_completed``,
        ``no_file``, ``failed``. Scope is ``bot.db`` only (Telethon sessions + the master
        key live in separate files under ``data/`` and are untouched)."""
        backup = await repo.get_backup(self._db, backup_id)
        if backup is None:
            return "not_found"
        if backup["status"] != "completed":
            return "not_completed"
        file_path = backup.get("file_path")
        if not file_path or not Path(file_path).exists():
            return "no_file"
        # Flush the WAL into the main file so the live aiosqlite conn has no
        # stale frames once the destination backup overwrites the db file.
        await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            await asyncio.to_thread(self._do_restore, Path(file_path))
        except Exception as exc:  # noqa: BLE001
            logger.exception("restore of backup %d failed", backup_id)
            await repo.set_backup_status(
                self._db, backup_id, "failed",
                error=str(exc), finished_at=repo.now_iso(),
            )
            return "failed"
        await self._audit(
            "backup_restored", admin_id,
            {"backup_id": backup_id, "file_path": file_path},
        )
        await self._publish_system_event(
            backup_id, admin_id, "backup_restored", "info",
            "⟡|استعادة نسخة احتياطية",
            f"⟡|النسخة <code>#{backup_id}</code> استعيدت على القاعدة الحية.",
        )
        return "restored"

    # ------------------------------------------------------------------ sweeper

    def start_sweeper(self) -> None:
        """Start the background sweeper that runs due scheduled backups.

        Unlike the broadcast sweeper, backups need no Bot (file I/O only), so
        no ``bot`` parameter is required."""
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_task = asyncio.create_task(self.run_sweeper())

    async def run_sweeper(self) -> None:
        """Every ``backup_sweep_interval`` seconds, run backups whose
        ``scheduled_for`` has elapsed."""
        while True:
            try:
                due = await repo.list_due_scheduled_backups(self._db)
                for backup in due:
                    bid = backup["id"]
                    try:
                        await self.run_backup(bid)
                    except Exception:  # noqa: BLE001
                        logger.exception("sweeper: failed to run backup %d", bid)
                await asyncio.sleep(self._config.backup_sweep_interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("sweeper: unexpected error")

    async def stop_sweeper(self) -> None:
        """Cancel and await the sweeper task (idempotent)."""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

    # ------------------------------------------------------------------ boot

    async def recover(self) -> int:
        """Boot recovery: any backup left ``running`` by a previous process
        becomes ``failed`` with a restart reason (mirrors JobManager.recover).
        Due scheduled backups are left for the sweeper to pick up."""
        cur = await self._db.conn.execute(
            "UPDATE backups SET status='failed', error=?, finished_at=? "
            "WHERE status='running'",
            (_RECOVER_ERROR, repo.now_iso()),
        )
        n = cur.rowcount or 0
        if n:
            logger.warning("Boot recovery: marked %d running backup(s) as failed", n)
        return n

    # ------------------------------------------------------------------ lifecycle

    async def shutdown(self) -> None:
        """Cancel the sweeper and await any in-flight backup workers."""
        await self.stop_sweeper()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------ internals

    def _snapshot(self, label: str) -> tuple[str, int]:
        """Run in a worker thread: copy the live DB file to a fresh .db under
        the backup dir via the SQLite backup API."""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = self._safe_label(label)
        dest = self._backup_dir / f"{safe}_{ts}.db"
        # A fresh sqlite3 connection (not the aiosqlite live conn) is the
        # backup *source*; WAL readers don't block writers, so this is a
        # consistent online snapshot.
        src = sqlite3.connect(str(self._live_db_path))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        size = dest.stat().st_size if dest.exists() else 0
        return str(dest), int(size)

    def _do_restore(self, src_path: Path) -> None:
        """Run in a worker thread: overwrite the live DB file in place from the
        backup file (``src.backup(dst)`` where dst = live db)."""
        src = sqlite3.connect(str(src_path))
        dst = sqlite3.connect(str(self._live_db_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    @staticmethod
    def _safe_label(label: str) -> str:
        """Filesystem-safe slug for the filename (keeps Unicode alphanumerics
        and ``-``/``_``; everything else becomes ``_``)."""
        cleaned = "".join(
            ch if (ch.isalnum() or ch in "-_") else "_" for ch in (label or "")
        ).strip("_")
        return cleaned[:64] or "backup"

    async def _apply_retention(self) -> None:
        """Delete the oldest backup files + rows beyond ``backup_keep_last``,
        keeping the newest completed snapshots (ordered by ``created_at``)."""
        keep = self._config.backup_keep_last
        rows = await self._db.fetch_all(
            "SELECT id, file_path FROM backups "
            "WHERE status='completed' ORDER BY created_at ASC, id ASC"
        )
        if len(rows) <= keep:
            return
        for row in rows[: len(rows) - keep]:
            file_path = row["file_path"]
            if file_path:
                try:
                    os.remove(file_path)
                except OSError:
                    logger.warning("retention: could not remove %s", file_path)
            await self._db.execute("DELETE FROM backups WHERE id=?", (row["id"],))
            await self._audit(
                "backup_retained_delete", None,
                {"backup_id": int(row["id"]), "file_path": file_path},
            )

    async def _audit(
        self, event: str, owner_id: int | None, detail: dict[str, Any] | None
    ) -> None:
        try:
            await repo.audit(self._db, event, owner_id=owner_id, detail=detail)
        except Exception:  # noqa: BLE001 - audit is best-effort
            logger.warning("audit write failed for %s", event, exc_info=True)

    async def _publish_system_event(
        self,
        backup_id: int,
        admin_id: int | None,
        event_type: str,
        severity: str,
        title: str,
        body: str,
    ) -> None:
        try:
            await self._bus.publish(
                SystemEvent(
                    event_type=event_type,
                    severity=severity,
                    title=title,
                    body=body,
                    data={"backup_id": backup_id, "admin_id": admin_id},
                )
            )
        except Exception:  # noqa: BLE001 - publish never raises
            logger.warning(
                "backup: system event publish failed (%s)", event_type, exc_info=True
            )


def esc_iso(value: str) -> str:
    """Minimal HTML escape for ISO timestamp interpolation into event bodies."""
    import html

    return html.escape(str(value), quote=True)
