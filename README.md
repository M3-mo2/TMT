# TMT — Telegram Member Transfer Bot

Manage your personal Telegram accounts through a bot, then use one of them to
transfer members between two Telegram groups — with a deep preflight inspection
before anything runs, live progress, cooperative cancellation, and honest
reporting of everything Telegram does not let us verify.

Built with aiogram 3 (Bot API) + Telethon 1.44 (User API) + SQLite.
Full design: [PRD.md](PRD.md). Engineering rules: [RULES.md](RULES.md).

## Architecture in one paragraph

One asyncio process, layered: `app/bot` (Arabic UI, aiogram — presentation only) →
`app/core` (account service, job manager, event bus — business rules) →
`app/tg` (Telethon: client pool, login flow, entity resolver, preflight,
transfer engine) → `app/db` (aiosqlite repositories, migrations).
Jobs live in SQLite as the source of truth; the transfer engine never imports
the bot layer, so a future worker/queue split is an extraction, not a rewrite.

## Setup

Requires Python 3.11+.

```bash
pip install -e .[dev]
cp .env.example .env
```

Edit `.env`:

| Variable | Where to get it |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → /newbot |
| `API_ID`, `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |

`API_ID`/`API_HASH` identify the *application* your personal accounts connect
through; they are required for the login flow. Everything else in `.env` has
sane defaults (job limits, delays, FloodWait cap — see `.env.example`).

## Run

```bash
python -m app
```

On boot the bot applies DB migrations, recovers any job interrupted by a
previous shutdown (marked `interrupted`, safe to re-run), and starts polling.
Open a **private chat** with the bot — group messages are ignored by design.

Typical flow: الحسابات → إضافة حساب (phone → code → optional 2FA) → النقل →
choose account, send source and destination group references
(`@name`, `t.me/...`, invite links, or numeric IDs) → review the preflight
report → confirm. Progress runs on a live card you can cancel at any time.

## Security notes

- **Telegram sessions are bearer credentials.** They are stored as
  [Fernet](https://cryptography.io)-encrypted strings inside the SQLite
  database — never as plain files, never in logs, never in Git.
- The master key comes from `SESSIONS_MASTER_KEY` (env) or is auto-generated
  on first run into `data/keys/master.key` (file mode `0600`, directory `0700`).
- **Losing the key means every stored session must log in again.** Back up the
  key separately from the DB; a stolen DB without the key is inert.
- **Key rotation:** generate a new Fernet key, then re-encrypt:
  ```python
  import asyncio
  from cryptography.fernet import Fernet
  from app.db.database import Database
  from app.db import repositories as repo
  from app.security.crypto import SessionCrypto
  from pathlib import Path

  async def rotate(db_path: str, old_key: bytes, new_key: bytes) -> None:
      db = Database(Path(db_path)); await db.connect()
      old, new = SessionCrypto(old_key), Fernet(new_key)
      for acc in await db.fetch_all("SELECT id, session_encrypted FROM accounts"):
          await db.execute(
              "UPDATE accounts SET session_encrypted=? WHERE id=?",
              (old.reencrypt(acc["session_encrypted"], new_key), acc["id"]),
          )
      await db.close()

  asyncio.run(rotate("data/bot.db", old_key, Fernet.generate_key()))
  ```
- **Threat-model honesty:** encryption at rest defends against DB-file-only
  compromise (lost backups, copied data dirs). An attacker with root on the
  host reads both the DB and the key — that scenario is out of scope for any
  local-key design.
- Login codes and 2FA passwords live only in process memory with a hard TTL,
  are deleted from the chat on arrival, and are never persisted anywhere.
- User isolation: every query and callback is owner-scoped; the repos refuse
  cross-owner access structurally, not by convention.

## Operations

- **Boot recovery:** jobs found `running`/`queued` after a crash are marked
  `interrupted` with progress preserved; re-running is safe (already-invited
  members are skipped).
- **PeerFlood cooldown:** if Telegram hard-limits an account from invites, the
  account is marked `limited` and new jobs on it are refused until the cooldown
  passes.
- **Config knobs:** concurrency per process/user, members per job, invite
  delay + jitter, FloodWait auto-wait cap, job timeout — all in `.env.example`.
- **Logs:** stderr, human-readable, with a secret-redaction filter as a second
  line of defense (the first is: secrets are never passed to loggers).
- **Systemd** sketch:
  ```ini
  [Unit]
  Description=TMT bot
  After=network-online.target

  [Service]
  WorkingDirectory=/opt/tmt
  EnvironmentFile=/opt/tmt/.env
  ExecStart=/opt/tmt/.venv/bin/python -m app
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

## Testing

```bash
python -m pytest -q
```

The suite is fully offline: in-memory/tmp SQLite, real Fernet crypto, and fake
Telethon clients with scriptable behaviors. No test talks to Telegram.

## Known limitations

- Bot usage is private-chat only (groups are ignored).
- Broadcast channels are not supported as source or destination — groups and
  supergroups only.
- Source groups that hide their member list cannot be read; preflight reports
  this as unverifiable and blocks the transfer rather than pretending.
- Numeric-ID references only resolve if the account already knows the entity.
- MemoryStorage for in-dialog state: a restart cancels any half-finished
  dialog (login flows expire anyway).
- Telegram's invite limits are undocumented and vary per account; the engine
  observes them at runtime (FloodWait/PeerFlood handling) rather than assuming.
