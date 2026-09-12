"""Backup system: periodic DB snapshots sent to the operator.

Sits in ``core/`` (RULES §1: may import ``db``/``config``/``core`` but not
``app.bot`` or ``app.tg``).  The ``bot`` is injected per-call — the service is
constructed before the Bot exists on boot and stores it via ``start()``, exactly
mirroring :class:`~app.core.broadcast.Broadcaster`.

Design summary:

* **Consistent snapshots** without disturbing the live connection — a
  dedicated read-only :mod:`sqlite3` connection runs the SQLite *backup API*
  inside a worker thread (``asyncio.to_thread``) so the event loop is never
  blocked (NFR1).  Each archive bundles both ``bot.db`` and the Fernet master
  key (``master.key``) so it is a *complete* restore package; the per-archive
  security note is explicit (see ``M_BACKUP_SECURITY_NOTE``).
* **Scheduling** — a sweeper wakes every ``backup_sweep_interval`` and, when the
  feature is enabled and the interval has elapsed since the last completed
  backup, runs ``create_backup`` (snapshot → zip → record → DM the operator).
* **Robust restore** — the live DB is checkpointed, a *safety* copy of the
  current files is made, the new files are installed, the DB is reopened +
  migrated, and on any failure the safety copy is rolled back so the bot never
  lands with an empty/broken database (RULES §5: final states are durable,
  operations are transactional where the layer allows).
* **Retention** — completed archives are pruned to ``backup_retention_count``
  newest when storage is on the same host.

The DB rows are the source of truth (PRD §12 seam #1); nothing backup-related
lives only in RAM except the sweeper task handle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.models import BackupRecord, BackupSettings, BackupStatus
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger("app.core.backup")

__all__ = [
    "BackupService",
    "BackupResult",
    "RestoreResult",
    "SnapshotError",
    "fmt_ts",
]


@dataclass
class BackupResult:
    """Outcome of a backup attempt surfaced to the bot layer."""

    ok: bool
    backup_id: int | None = None
    file_size: int = 0
    error: str | None = None


@dataclass
class RestoreResult:
    """Outcome of a restore attempt."""

    ok: bool
    error: str | None = None
    # Human-readable detail (Arabic) for the admin; the raw cause stays in logs.
    detail: str = ""
    active_jobs: int = 0


class SnapshotError(Exception):
    """Raised when the DB/key could not be snapshotted."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_ts(ts: str) -> str:
    """Render an ISO-8601 UTC timestamp for the admin, or "—".

    Exposed for the bot layer's text renderers; kept dependency-free.
    """
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "—"


class BackupService:
    """Periodic DB backup producer + restore/export operator.

    Lifecycle mirrors :class:`~app.core.broadcast.Broadcaster`:
    construct with ``db``/``config``; the bot is attached via ``start_sweeper``
    since it does not exist at composition-root time.  Call
    ``stop_sweeper()`` + ``shutdown()`` on exit.
    """

    # Names of the files inside the archive.
    DB_MEMBER = "bot.db"
    KEY_MEMBER = "master.key"

    def __init__(self, db: Database, config: Any) -> None:
        self._db = db
        self._config = config
        self._bot: Any = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()  # serialises file-system DB ops (backup/restore)
        self._dir: Path = config.data_dir / "backups"

    # ------------------------------------------------------------------ config

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def db_path(self) -> Path:
        return self._config.db_path

    @property
    def key_path(self) -> Path:
        return self._config.key_file

    async def get_settings(self) -> BackupSettings:
        """Resolve backup settings: DB value or Config default.

        ``chat_id`` defaults to the first configured admin (``ADMIN_IDS``) so the
        operator receives backups with zero setup.  ``interval_hours`` and
        ``enabled`` default to Config when unset/empty.
        """
        raw = await repo.get_backup_settings(self._db)
        enabled_raw = raw.get(repo.BACKUP_KEY_ENABLED, "false").strip().lower()
        enabled = enabled_raw in ("1", "true", "yes", "on")
        try:
            interval = int(raw.get(repo.BACKUP_KEY_INTERVAL, "") or "")
        except ValueError:
            interval = 0
        if interval < 1:
            interval = int(self._config.backup_interval_hours)
        try:
            chat_id = int(raw.get(repo.BACKUP_KEY_CHAT_ID, "") or "")
        except ValueError:
            chat_id = None
        if not chat_id:
            admins = self._config.admin_id_list
            chat_id = admins[0] if admins else None
        return BackupSettings(enabled=enabled, interval_hours=interval, chat_id=chat_id)

    async def set_setting(self, key: str, value: str) -> None:
        await repo.set_setting(self._db, key, value)

    async def list_backups(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return backup rows as dicts (bot-layer convention, like broadcasts)."""
        records = await repo.list_backups(self._db, limit=limit, offset=offset)
        return [
            {
                "id": r.id,
                "filename": r.filename,
                "file_size": r.file_size,
                "status": r.status.value,
                "created_at": r.created_at,
                "sent_to": r.sent_to,
                "error": r.error,
            }
            for r in records
        ]

    # ------------------------------------------------------------------ lifecycle

    def start_sweeper(self, bot: Any) -> None:
        """Attach the bot and start the periodic sweep task."""
        self._bot = bot
        if self._sweep_task is not None and not self._sweep_task.done():
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._dir, stat.S_IRWXU)  # 0700, best effort
        except OSError:
            pass
        self._sweep_task = asyncio.create_task(self._run_sweeper())
        logger.info("backup sweeper started (interval=%ds)", self._config.backup_sweep_interval)

    async def stop_sweeper(self) -> None:
        if self._sweep_task is not None and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
        self._sweep_task = None

    async def shutdown(self) -> None:
        """Cancel the sweeper and release the bot reference."""
        await self.stop_sweeper()
        self._bot = None

    async def _run_sweeper(self) -> None:
        """Wake periodically; run a backup when the interval has elapsed."""
        while True:
            try:
                await asyncio.sleep(self._config.backup_sweep_interval)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("backup sweeper: unexpected error")
                # keep sweeping rather than dying silently

    async def _tick(self) -> None:
        settings = await self.get_settings()
        if not settings.enabled:
            return
        # Skip if a restore is in progress (DB file is mid-swap).
        if self._lock.locked():
            return
        last = await repo.last_backup_created_at(self._db)
        if last is not None and not self._is_due(last, settings.interval_hours):
            return
        result = await self.create_backup(send=True)
        if result.ok:
            logger.info("periodic backup created: id=%s size=%d", result.backup_id, result.file_size)
            await repo.audit(
                self._db, "backup_created",
                detail={"backup_id": result.backup_id, "file_size": result.file_size},
            )
        else:
            logger.warning("periodic backup failed: %s", result.error)
            await repo.audit(
                self._db, "backup_failed",
                detail={"error": result.error, "file_size": result.file_size},
            )

    def _is_due(self, last_ts: str, interval_hours: int) -> bool:
        try:
            last = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() >= interval_hours * 3600

    # ------------------------------------------------------------------ create

    async def run_now(self) -> BackupResult:
        """Manual, on-demand backup from the admin panel (always sends)."""
        return await self.create_backup(send=True)

    async def create_backup(self, *, send: bool = True) -> BackupResult:
        """Snapshot the live DB + key into a zip archive, record it, optionally DM it.

        The snapshot is taken with a dedicated read-only connection so the live
        ``Database`` connection is never interrupted (no close/reopen dance for
        the common path).
        """
        async with self._lock:
            bid = await repo.insert_backup(
                self._db, filename="", file_size=0, status=BackupStatus.RUNNING
            )
            try:
                archive_path = await self._make_archive()
            except (SnapshotError, OSError) as exc:
                logger.exception("backup snapshot failed")
                await repo.update_backup_status(
                    self._db, bid, BackupStatus.FAILED, error=str(exc)
                )
                return BackupResult(ok=False, backup_id=bid, error=str(exc))

            size = archive_path.stat().st_size

            sent_to: int | None = None
            if send and self._bot is not None:
                settings = await self.get_settings()
                if settings.chat_id is not None:
                    sent = await self._send_archive(settings.chat_id, archive_path)
                    if sent:
                        sent_to = settings.chat_id
                    else:
                        # Archive is still valid; a DM failure doesn't fail the backup.
                        logger.warning("backup DM failed for backup_id=%s chat=%s", bid, settings.chat_id)

            # Single RUNNING -> final transition. update_backup_status only writes
            # pending/running rows (final states are write-once, per RULES §5), so
            # the filename + size + sent_to must be folded into one write rather
            # than split into an OK-then-SENT pair (the SENT step would be a no-op
            # once the row is already 'ok').
            final_status = BackupStatus.SENT if sent_to is not None else BackupStatus.OK
            await repo.update_backup_status(
                self._db, bid, final_status,
                file_size=size, filename=archive_path.name, sent_to=sent_to,
            )

            await self._prune_retention()
            return BackupResult(ok=True, backup_id=bid, file_size=size)

    async def _make_archive(self) -> Path:
        """Create ``data/backups/backup-<ts>.zip`` containing bot.db + master.key.

        Done in a worker thread (the SQLite backup API is synchronous); the
        snapshot uses a *separate* read-only connection so the live connection
        is untouched.
        """
        ts = _now_iso().replace(":", "").replace("-", "")
        # Microsecond precision avoids filename collisions when several backups
        # are produced within the same second (on-demand double-clicks, etc.).
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        archive_name = f"backup-{ts}.zip"
        archive_path = self._dir / archive_name

        db_path = self.db_path
        key_path = self.key_path
        if not db_path.exists():
            raise SnapshotError(f"database file not found: {db_path}")
        # On-demand backups (run_now) may run before start_sweeper() created the
        # directory, so ensure it exists here rather than only in the sweeper.
        self._dir.mkdir(parents=True, exist_ok=True)

        def _work() -> None:
            # Dedicated read-only connection — sees committed WAL data, never
            # blocks the live writer the way reusing the app connection would.
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
            try:
                if archive_path.exists():
                    archive_path.unlink()
                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    # Page-copy snapshot into a throwaway file → consistent DB state.
                    snap_file = archive_path.with_suffix(".db.tmp")
                    snap = sqlite3.connect(str(snap_file))
                    try:
                        src.backup(snap, sleep=0.25, pages=100)
                    finally:
                        snap.close()
                    zf.write(snap_file, arcname=self.DB_MEMBER)
                    snap_file.unlink(missing_ok=True)
                    if key_path.exists():
                        zf.write(key_path, arcname=self.KEY_MEMBER)
            finally:
                src.close()

        await asyncio.to_thread(_work)
        if not archive_path.exists():
            raise SnapshotError("archive was not created")
        return archive_path

    async def _send_archive(self, chat_id: int, archive_path: Path) -> bool:
        """Best-effort DM of the archive; never raises to the caller."""
        try:
            from aiogram.types import FSInputFile

            await self._bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(str(archive_path)),
                caption="⟡|نسخة احتياطية من قاعدة البيانات.",
            )
            return True
        except Exception:
            logger.warning("failed to send backup archive to %d", chat_id, exc_info=True)
            return False

    # ------------------------------------------------------------------ restore

    async def restore_from_file(self, archive_path: Path) -> RestoreResult:
        """Restore the live DB + key from an uploaded/on-disk archive.

        Refuses while jobs are running (RULES §5: never run two jobs on one
        account; restoring the DB mid-flight would corrupt in-flight state).
        Wraps the swap in a worker thread with a safety copy + rollback so a
        failure never leaves the bot without a working database.
        """
        async with self._lock:
            active = await repo.count_active_jobs(self._db)
            if active:
                logger.warning("restore refused: %d active job(s) running", active)
                return RestoreResult(
                    ok=False,
                    error="active_jobs",
                    detail="توجد عمليات نشطة الآن، ألغِ العمليات أولاً ثم جرّب الاستعادة.",
                    active_jobs=active,
                )

            # Validate the archive (in a thread; reads are cheap, sync IO).
            extracted = await asyncio.to_thread(self._extract_and_validate, archive_path)
            if extracted is None:
                return RestoreResult(
                    ok=False,
                    error="invalid_archive",
                    detail="الملف غير صالح أو لا يحتوي على قاعدة بيانات صالحة.",
                )

            return await self._swap(extracted)

    def _extract_and_validate(self, archive_path: Path) -> tuple[Path, Path | None] | None:
        """Validate that *archive_path* is a zipping containing a real SQLite DB
        (+ key).  Returns ``(db_tmp, key_tmp_or_None)`` on success, else None.

        Validation is structural only (SQLite header + presence of the
        ``schema_migrations`` table) — we never trust uploaded data; we let the
        reopened connection's migration runner assert schema compatibility.
        """
        try:
            if not zipfile.is_zipfile(str(archive_path)):
                return None
            with zipfile.ZipFile(archive_path, "r") as zf:
                names = zf.namelist()
                if self.DB_MEMBER not in names:
                    return None
                tmpdir = archive_path.parent / "_stage"
                tmpdir.mkdir(parents=True, exist_ok=True)
                db_tmp = tmpdir / self.DB_MEMBER
                zf.extract(self.DB_MEMBER, tmpdir)
                # SQLite header magic: the literal is 16 bytes ("SQLite format 3"
                # = 15 chars + NUL), so compare a full 16-byte slice.
                with open(db_tmp, "rb") as fh:
                    header = fh.read(16)
                if header[:16] != b"SQLite format 3\x00":
                    return None
                # Quick schema sanity: must own a migrations table.
                probe = sqlite3.connect(f"file:{db_tmp}?mode=ro", uri=True, timeout=10)
                try:
                    cur = probe.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='schema_migrations'"
                    )
                    if cur.fetchone() is None:
                        return None
                finally:
                    probe.close()
                key_tmp: Path | None = None
                if self.KEY_MEMBER in names:
                    zf.extract(self.KEY_MEMBER, tmpdir)
                    key_tmp = tmpdir / self.KEY_MEMBER
                return db_tmp, key_tmp
        except Exception:
            logger.exception("backup archive validation failed")
            return None

    def _safety_backup(self) -> tuple[Path, Path]:
        """Copy the live DB + key into a safety archive; returns the copies."""
        stamp = _now_iso().replace(":", "").replace("-", "")
        db_safety = self._dir / f"safety-{stamp}.db"
        key_safety = self._dir / f"safety-{stamp}.key"
        if self.db_path.exists():
            shutil.copy2(self.db_path, db_safety)
        if self.key_path.exists():
            shutil.copy2(self.key_path, key_safety)
        return db_safety, key_safety

    def _install(self, db_src: Path, key_src: Path | None) -> None:
        """Overwrite the live DB (+ key if present) with the validated sources."""
        if db_src.exists():
            shutil.copy2(db_src, self.db_path)
        if key_src is not None and key_src.exists():
            shutil.copy2(key_src, self.key_path)

    def _rollback(self, db_safety: Path, key_safety: Path) -> None:
        """Restore the live files from the safety copies."""
        if db_safety.exists():
            shutil.copy2(db_safety, self.db_path)
        if key_safety.exists():
            shutil.copy2(key_safety, self.key_path)

    async def _swap(self, extracted: tuple[Path, Path | None]) -> RestoreResult:
        db_src, key_src = extracted
        # Capture audit-relevant facts before the finally block rmtrees staging.
        db_size = db_src.stat().st_size
        db_safety, key_safety = await asyncio.to_thread(self._safety_backup)
        ok = True
        error: str | None = None
        try:
            await self._db.close()
            await asyncio.to_thread(self._install, db_src, key_src)
            await self._db.connect()  # rerun migrations (no-op if schema current)
        except Exception:
            logger.exception("restore swap failed — rolling back")
            try:
                await asyncio.to_thread(self._rollback, db_safety, key_safety)
                await self._db.connect()
            except Exception:
                logger.exception("restore rollback also failed — manual intervention required")
                ok = False
                error = "rollback_failed"
            if error is None:
                ok = False
                error = "swap_failed"
        finally:
            # Clean staging artifacts regardless of outcome.
            try:
                shutil.rmtree(db_src.parent, ignore_errors=True)
            except Exception:
                pass
        if not ok:
            await repo.audit(
                self._db, "backup_restore_failed",
                detail={"error": error},
            )
            return RestoreResult(
                ok=False,
                error=error,
                detail="فشل استعادة النسخ الاحتياطي — تم الرجوع إلى النسخة الأصلية.",
            )
        await repo.audit(
            self._db, "backup_restored",
            detail={"file_size": db_size},
        )
        logger.info("restore complete from %s", db_src.name)
        return RestoreResult(ok=True, detail="تمت استعادة النسخة الاحتياطية بنجاح.")

    # ------------------------------------------------------------------ export/delete

    async def get_backup(self, backup_id: int) -> BackupRecord | None:
        """Fetch a single backup record via the service boundary (no repo leakage)."""
        return await repo.get_backup(self._db, backup_id)

    async def archive_path_for(self, backup_id: int) -> Path | None:
        rec = await repo.get_backup(self._db, backup_id)
        if rec is None or not rec.filename:
            return None
        path = self._dir / rec.filename
        return path if path.exists() else None

    async def export_to(self, backup_id: int, chat_id: int) -> bool:
        """DM an existing backup archive to *chat_id* (admin 'export')."""
        path = await self.archive_path_for(backup_id)
        if path is None:
            return False
        return await self._send_archive(chat_id, path)

    async def delete_backup(self, backup_id: int) -> bool:
        async with self._lock:
            rec = await repo.get_backup(self._db, backup_id)
            if rec is None:
                return False
            path = self._dir / rec.filename
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("could not delete backup file %s: %s", path, exc)
            return await repo.delete_backup(self._db, backup_id)

    # ------------------------------------------------------------------ retention

    async def _prune_retention(self) -> None:
        keep = int(self._config.backup_retention_count)
        if keep <= 0:
            return
        # Remove on-disk files that are beyond the kept set.
        kept = await repo.list_backups(self._db, limit=keep)
        kept_names = {r.filename for r in kept}
        for entry in self._dir.glob("backup-*.zip"):
            if entry.name not in kept_names:
                try:
                    entry.unlink()
                except OSError:
                    pass
        deleted = await repo.delete_backups_older_than(self._db, keep)
        if deleted:
            logger.info("backup retention: pruned %d old row(s)", deleted)

    # ------------------------------------------------------------------ display

    async def last_backup_created_at(self) -> str | None:
        return await repo.last_backup_created_at(self._db)

    async def count_backups(self) -> int:
        return await repo.count_backups(self._db)
