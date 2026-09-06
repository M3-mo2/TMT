# RULES.md — Mandatory rules for any agent or developer working on TMT

Read PRD.md first. These rules exist to prevent architectural drift, security
regressions, and behavior breaks. They are concrete, not aspirational.

## 1. Layer boundaries (hard)

- `bot/` (aiogram) must not import `telethon` directly. It talks to `core` services only.
- `tg/` (Telethon) must not import `aiogram` or `db/`. The transfer engine and
  preflight receive everything they need as parameters. `tg.errors` may be imported
  by anyone (it is the shared vocabulary of failure).
- `db/` must not import `bot/`, `tg/`, or any service module (`core` services,
  `db` may not import them). Exception: `app/core/models.py` is the shared domain
  vocabulary — every layer may import it; it must stay dependency-free.
- Handler functions are thin: parse input → call a service → render text. Any
  `if/elif` business decision longer than 3 lines belongs in `core/` or `tg/`, not in
  a handler.

## 2. Telegram capabilities: verify or mark UNKNOWN — never assume

- Before writing "Telegram allows X", it must be backed by an API check in the code
  path or a documented runtime observation (PRD §8).
- Anything not reliably verifiable is reported as `UNKNOWN` in preflight and never
  counted as success.
- New error classes from Telethon must be added to `tg/errors.py` classification —
  never `except Exception: pass`, never bare `except:`.

## 3. Secrets — absolute

- Never log, never store in DB unencrypted, never `print`: session strings, phone
  login codes, 2FA passwords, `phone_code_hash`, API_ID/API_HASH values, master key.
- Never commit: `.env`, `data/`, `*.session`, any file containing real credentials.
  `.gitignore` already covers them — keep it that way; check `git status` before
  every commit.
- Sessions are encrypted with `security/crypto.py` (Fernet). No new crypto code
  anywhere else; no changes to key handling without updating PRD §7.
- Login-flow secrets live only in memory inside `LoginFlowManager` with TTL. They
  must never be written to the DB, to disk, or into FSM storage data.

## 4. User isolation (hard)

- Every DB query that reads or writes user-owned rows must be scoped by `owner_id`.
- Every callback/button handler must verify the referenced account/job belongs to
  the callback user before acting. Callback data is user-controlled input, not
  authorization.
- `account_id` / `job_id` in a callback must be joined against `owner_id` in the
  repository call, not filtered afterward in Python.

## 5. Job & state discipline

- Job status transitions go through guarded SQL updates (`WHERE status IN (...)`).
  Final states (`completed`, `failed`, `cancelled`, `interrupted`) are write-once.
- Never run two jobs on the same account concurrently. `JobManager` is the only
  component that starts jobs.
- Cancellation is cooperative: the engine checks the cancel flag between invites and
  inside waits. Never `task.cancel()` alone as the cancel path.
- Never auto-retry `PeerFloodError`. Never blind-retry anything; retries are bounded
  and only for kinds classified retryable in `tg/errors.py`.
- On boot, `JobManager.recover()` runs before any new job can start.

## 6. Database

- All writes that touch more than one row go in a transaction.
- No SQLite-isms in SQL (no `AUTOINCREMENT`, `PRAGMA` in queries, `RANDOM()`...):
  PostgreSQL migration (PRD §19) must remain a one-module change.
- Schema changes are new migrations in `db/migrations.py`, append-only. Never edit
  an applied migration; never hand-edit the DB file.

## 7. Arabic UI contract

- All user-visible strings live in `bot/texts.py` — no f-string Arabic text scattered
  in handlers.
- HTML parse mode; `<b>`/`<code>`/`<i>`; symbols `✓ ! × ›` as inline decorations
  only. **Forbidden**: emoji, ASCII art, box-drawing, text tables, space-aligned
  layouts.
- No raw exception text, no English jargon unless unavoidable; technical detail
  goes to logs.
- User input that is a secret (code, password) must be deleted from chat when the
  flow moves on.

## 8. Code quality gates (definition of done for any change)

- Type hints everywhere; `python -m app` still imports cleanly.
- `pytest` passes offline. New behavior ships with tests (state transitions, error
  paths, isolation).
- No dead code, no commented-out code, no debug prints, no duplicated business
  logic. If two modules need the same rule, lift it to `core/` or `tg/errors.py`.
- No new dependency without: (a) a real need, (b) a PRD note, (c) pin update in
  `pyproject.toml`.

## 9. Process

- Breaking existing behavior requires an explicit reason written into the change
  (commit message or PRD update). "It's cleaner" is not a reason.
- Security-relevant changes (crypto, sessions, isolation, logging) require re-reading
  PRD §7 and updating it if the design changes.
- Before declaring done: run tests, run the security checklist (PRD §7 + §27 of the
  master prompt), `git status` / `git diff` review, then commit.
