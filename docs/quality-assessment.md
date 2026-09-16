# TMT — Whole-Codebase Quality Assessment

**Status:** Build-time inspection only. No code was changed to produce this report.
**Branch inspected:** `agent/feature-34694965636`
**Method:** static analysis + the offline test suite + `python -c "import app"`.
**Baseline evidence:**

- `python -m pytest -q` → **483 passed** (suite is fully offline: in-memory/tmp SQLite, real Fernet, fake Telethon/aiogram).
- `python -c "import app"` → **clean import**.
- `ruff check app/ tests/` → **157 errors** (37 import-sort I001, 18 unused-import F401, 25 P017, 11 E001, 10 F059, 1 F821, 6 F841 …). **No linter is configured in the project and no CI gate runs it.**
- `.github/workflows/` contains only `opencode.yml` (the multi-agent pipeline) and `orchestrator.yml`. **No workflow runs `pytest`/`ruff`/`mypy` on push or pull-request** — the only automated gate is the Review/Verify agent inside this pipeline.
- Two feature-specific deep-dive reports already exist and are treated as authoritative references here: `docs/notifications/ReviewReport.md` (41 findings on the notification subsystem) and `docs/mandatory-subscription/report.md` (15 findings, S1–S4). This report is *whole-codebase*: it summarises those two clusters only as pointer items and focuses on the rest of the system plus cross-cutting concerns.

**Severity scale:** Critical · High · Medium · Low. *Critical* = would cause data loss, account takeover, or makes a shipped feature unusable; *High* = violates an explicit RULES.md guard or breaks a shipped feature's correctness; *Medium* = real defect with a workaround/work-around or a maintainability/clarity tax; *Low* = drift, dead code, polish.

---

## 1. Executive summary (top 12, prioritized)

| # | Problem | Severity | Where |
|---|---------|----------|-------|
| 1 | PRD §3 "Non-goals" never updated to match shipped code — admin panel, broadcast campaigns, scheduling, mandatory-subscription gate, notification system, per-user settings were all *excluded* from v1 but are now implemented. The spec and the code are out of sync. | High | `PRD.md:54-62` vs `app/` tree |
| 2 | **Broadcast** `cancel()` emits `broadcast_failed` and the worker finalization *also* emits `broadcast_completed` for the same action → two inbox rows, contradictory event types. | High | `app/core/broadcast.py:315-323` + `:677-697` |
| 3 | **Broadcast** `set_broadcast_status` has **no compare-and-set / write-once guard** (unlike `repo.transition_job`), so cancel vs. worker finalization race and final states aren't write-once. | High | `app/db/repositories.py:649-670`; call sites `broadcast.py:306,543,655` |
| 4 | **Notifications** `mark_notification_read`/`dismiss_notification`/`delete_notification` have **no `owner_id` scoping** → any admin can read-dismiss-delete *another admin's* notifications (IDOR). Tests don't cover it. | High | `repositories.py:898,918,928`; `notifications.py:77,98` |
| 5 | `dateutil` imported (`broadcast.py:128`) but **not declared in `pyproject.toml`** — only present via a transitive aiogram dep; scheduling silently breaks if that pin moves. | Medium | `app/bot/routers/admin/broadcast.py:128` vs `pyproject.toml:16-20` |
| 6 | **Broadcast** `resume()` spawns a worker task but **never stores it in `self._tasks`** → `shutdown()` can't cancel it; in-memory state lies. Plus `test_bcast_pause.py` (promised by `Phases.md`) is missing — zero coverage. | Medium | `app/core/broadcast.py:359-364`; `docs/broadcast/Phases.md:281` |
| 7 | **Broadcast** recovery corrupts `total_recipients`: on resume `total=len(pending)` overwrites the column, losing already-sent counts. | Medium | `app/core/broadcast.py:541-544` |
| 8 | `job_interrupted` and `flood_wait` are **declared in `SYSTEM_EVENTS` but never published** (dead config toggles + dead inbox entries). | Medium | `app/core/events.py:105-121` (declared); no publisher (grep-confirmed) |
| 9 | **Notifications** `delivered` flag is never set after a successful DM; no resend path; per-admin DB error aborts the whole event; `event.data` isn't json-guarded; unknown event types fail-open. | Medium | `app/core/notifications.py:89-129,133-151`; `repositories.py:83-85` |
| 10 | `upsert_user`/`upsert_account` do SELECT-then-write **outside a transaction** → TOCTOU race on concurrent logins for the same account/user. | Medium | `app/db/repositories.py:51-64, 135-156` |
| 11 | No CI test/lint gate; **157 ruff errors** sit on `main` unchecked, including undefined name `Config` in `texts.py:179` (F821) and unused imports in production modules. | Medium | ruff scan; `app/bot/texts.py:179`, `app/bot/reporter.py:13`, `app/bot/routers/jobs.py:20-21`, etc. |
| 12 | RULES §7 "no emoji" contract broken codebase-wide **and the test that should enforce it (`test_texts.py` `_ALLOWED_SYMBOLS`) whitelists the violating emoji** — the gate is inverted. | Medium | `RULES.md:7`, `app/bot/texts.py`, `tests/test_texts.py:26` |

Items 2–9 are substantiated in full in the relevant sections below (§2–§4). The notification-system items (4, 9, and the rest of §3) are **not** re-derived here — see `docs/notifications/ReviewReport.md` (4 High, 9 Medium, 21 Low, 1 Question) for line-precise evidence.

---

## 2. Architecture & requirements drift (High)

### 2.1 The approved PRD and the shipped code disagree on scope
`PRD.md` §3 (*Non-goals*) explicitly excludes from v1:

> - No admin panel now … — No multi-worker execution now … — No broadcast channels as source/destination … — **No scheduled/recurring transfers.** — No PostgreSQL, Redis, Docker in v1 — SQLite only, single process.

Yet the repository now ships, as core v1 features:

- A full **admin panel** — `app/bot/routers/admin/{menu,stats,users,channels,broadcast,notifications,backups,search,settings}.py`, mounted unconditionally on `admin_router` (`router.py:7-21`). `backups.py` is an empty stub (`"💾|إدارة النسخ الاحتياطية"`, no logic).
- A **Broadcast Campaign Engine** with scheduling, A/B testing, personalization templates, drafts, and a sweeper — `app/core/broadcast.py`, `broadcast_models.py`, `rate_limiter.py`, backed by migrations V5–V8.
- A **mandatory-subscription gate** + channels table (V9) — `app/bot/gate.py`, `app/bot/routers/admin/channels.py`.
- A **system-notification engine** with a persistent admin inbox (V10) — `app/core/notifications.py`, `app/bot/routers/admin/notifications.py`.
- **Per-user transfer-parameter overrides** — `app/core/settings.py`, exposed in the user settings router.

None of these expansions were reconciled with `PRD.md §3` (no line was struck or retitled "accepted"). The design docs that *do* describe them (`docs/broadcast/*`, `docs/notifications/*`, `docs/mandatory-subscription/*`) present them as internally-consistent, but the **specification of record (`PRD.md`) still tells a different story**. Consequences:

- **Attack surface grew** in directions the threat model (PRD §7, "Linux box, single process") never accounted for (multi-admin inbox, scheduled jobs, channel CRUD).
- **Two competing isolation models** now coexist: the PRD's "one bot user, their accounts and jobs" vs. the admin-panel's "trusted ADMIN_IDS operate on any user/account/broadcast" — never reconciled, so the IDOR class (§3.1) was introduced in the new code without the per-object ownership checks the user-facing layer has.
- **Operational complexity** the PRD §18 deployment notes don't cover (the `broadcaster` sweeper, the `NotificationService` subscriber, the broadcast worker pool) runs alongside the single-loop model with no documented interaction.

**Fix direction:** update `PRD.md §3` to either (a) accept these as in-scope v1 features with their security implications re-reviewed against §7, or (b) flag the parts that should be deferred. At minimum, document the admin-vs-user isolation boundary and the new background coroutines in §14 (concurrency).

> Layer boundaries (RULES §1) were checked and **hold**: `app/bot/` never imports the `telethon` library directly; `app/tg/` never imports `aiogram` or `app.db`. The violations below are logic/policy, not import cycles.

---

## 3. Notification system & system events

This cluster was reviewed in full detail in `docs/notifications/ReviewReport.md` (41 findings, file:line indexed). The headline items that belong in a *whole-codebase* risk view:

| ID | Problem | Severity | Location |
|----|---------|----------|----------|
| A1/A2 | `mark_notification_read` / `dismiss_notification` scope by `notification_id` only — **no `owner_id`**. Any admin can read-dismiss another admin's notification (IDOR). | High | `repositories.py:898,918`; handler `notifications.py:77,98` |
| A3 | `delete_notification` same IDOR, currently unwired (latent). | Low | `repositories.py:928` |
| B1 | Broadcast cancel double-publishes `broadcast_failed` + `broadcast_completed` (see §4.2). | High | `broadcast.py:315-323, 677-697` |
| B2 | `job_interrupted` declared but **never published** — boot recovery does a raw `UPDATE` (`recover_interrupted_jobs`) and publishes nothing; the inbox toggle is dead. | Medium | `events.py:115`; `job_manager.py:162-165`; `repositories.py:384-396` |
| B3 | `flood_wait` declared but **never published** — TFLOOD_WAIT error kind exists in `tg/errors.py:18` and the engine sleeps, but no `SystemEvent` is emitted; toggle + label dead. | Medium | `events.py:119` (no publisher anywhere) |
| C1 | `notifications.delivered` never set to `1` after a successful DM — dead column; the "resend failed DMs" roadmap can't work. | Medium | `notifications.py:94-105`; `repositories.py:829-856` (no delivered-update fn); test `test_notifications.py:312-314` *asserts* `delivered==0`, blessing the bug |
| C2 | Per-admin DB error in `_on_system_event` propagates and **aborts delivery to all remaining admins** for that event. | Medium | `notifications.py:89-105` |
| C3 | `event.data` passed to `json.dumps` unguarded — a non-serializable value aborts the whole fan-out. | Medium | `notifications.py:101` + `repositories.py:853` |
| D1 | Unknown/non-`SYSTEM_EVENTS` event types are **fail-open**: `_config_enabled` returns `True` for anything unrecognized, and the per-admin toggle UI only renders `SYSTEM_EVENTS` — so the event can't be muted. | Medium | `notifications.py:133-151`; `keyboards.py:259` |
| D2 | `account_unauthorized` bypasses the `notify_on_error` kill-switch (fallthrough `return True`) while `peer_flood`/`flood_wait` respect it — an operator who mutes errors is still paged for one but not the other. | Medium | `notifications.py:142-151` |
| E1 | Per-event, per-admin DB+Dm fan-out (`is_notification_enabled` + `create_notification` + `_try_dm`) runs **inline** in `EventBus.publish`, which JobManager/Broadcaster await — a multi-admin deploy slows job/broadcast startup to N×(DB+DM latency) on the critical path. | Medium | `notifications.py:89-105`; `events.py:75-80` |

The remainder (settings-screen N+1, pagination math, duplicated page-size constant, missing `NOTIFY_SETTINGS` constant, dead `render_notification_card`, doc drift) are Low and fully itemised in the existing report.

---

## 4. Core transfer & job subsystem (solid by design, two sharp edges)

The transfer engine (`app/tg/transfer.py`) and `JobManager` (`app/core/job_manager.py`) are the **strongest** part of the codebase and should be preserved, not rewritten:

- `TransferEngine` classifies every failure via `tg/errors.py:classify_error`, does bounded single-retry on transient kinds, cooperative cancel between invites and inside waits (`_RunState.sleep` slices waits), exactly-once final snapshot in a `finally`, and never `except Exception: pass` (`UNEXPECTED` is re-raised to the runner).
- `JobManager._finalize` uses the guarded CAS `repo.transition_job` (`WHERE status IN (...) AND status NOT IN (final)`) so finalization is **write-once** and double-finalization is impossible (`job_manager.py:523-532`).
- User isolation is enforced in the data layer: every job/account query is `owner_id`-scoped at the SQL level (`get_job`, `get_account`, `list_jobs`, …).

Two items to carry forward as risks / tech debt:

1. **`TransferEngine` is never exercised against a real account at startup** — `JobManager` runs a *second* preflight at `queued→running` (`job_manager.py:382-393`) after `create_job` already ran one in the wizard (`transfers.py`). Double work, and two different code paths (wizard uses `run_preflight` directly; runner uses `self._preflight`). Not a correctness bug, but it doubles preflight latency on every run and is untested as a "two-preflight" invariant.
2. `JobManager._finalize` writes counters *after* the CAS transition (`update_job_progress`, `job_manager.py:541`) but swallows that write's failure (`except Exception: # pragma: no cover`). If it fails, the job row has the final status but stale counters — an audit gap, not data loss. Acceptable but worth an audit log entry.

---

## 5. Broadcast Campaign Engine — state machine & concurrency defects (High/Medium)

This is the newest, most complex subsystem and the one with the most correctness gaps that are **not** covered by the (excellent) notification deep-dive.

### 5.1 `cancel()` double-publishes contradictory system events — High
On an admin cancel of a *running* campaign:
1. `Broadcaster.cancel()` persists `status='cancelled'` and publishes `broadcast_failed` (`broadcast.py:292-324`).
2. The worker `_run_campaign`, whose loop exits on `cancel_evt.is_set()`, then runs its finalization block and publishes `broadcast_completed` with title `×|فشل البث` ("Failed broadcast") (`broadcast.py:651-697`).

Result: **two inbox rows** for one user action with mutually exclusive `event_type` values (`broadcast_failed` + `broadcast_completed`) and a title that says "Failed" on a cancellation. The same defect is cited as item B1 in the notification report. The fix is to centralize finalization: the worker should **not** publish when `cancel_evt.is_set()` (let `cancel()` own the cancellation notification), or a dedicated `broadcast_cancelled` event type should exist and both paths should agree.

### 5.2 No compare-and-set on broadcast status — High
`repo.set_broadcast_status` (`repositories.py:649-670`) is an unconditional `UPDATE broadcasts SET … WHERE id=?`. Compare with the job path: `transition_job` guards with `WHERE status IN (…) AND status NOT IN (final)`, making final states write-once. Broadcasts have **no such guard**, so:
- `cancel()` writes `cancelled` (`broadcast.py:306`), but the worker can re-write `running` at `broadcast.py:543` (`set_broadcast_status(self._db, cid, "running", total_recipients=total)`) **after** cancel — a window where a cancelled campaign is again `running`.
- Final states (`completed`/`cancelled`/`failed`) are not write-once; a stray `set_broadcast_status` call after finalization silently changes a finished campaign.
- The `recover()` path (`broadcast.py:396-453`) and `start()` path both trust `status` to decide, with no atomic transition.

This is the single easiest place for a future one-line bug to corrupt campaign state.

### 5.3 `resume()` spawns an untracked worker — Medium
`Broadcaster.resume()` (`broadcast.py:352-374`) sets the pause event, and if the old task is gone it does `asyncio.create_task(self._run_campaign(...))` **without storing the result in `self._tasks`** (line 361-364). Consequences:
- `shutdown()` (`broadcast.py:456-462`) cancels only `self._tasks` → a resumed worker **survives shutdown** and keeps sending after the process is meant to stop.
- `self._tasks`, `self._cancel_events`, `self._pause_events` no longer agree (task exists, cancel/pause events cleared at end of `_run_campaign`).
- The sweeper (`run_sweeper`, line 487-498) calls `start()`, not `resume()`, so resumed-after-restart campaigns only get tracked via `recover()` — which does *not* store its task either... actually `recover()` at line 423 stores into `self._tasks`, so that path is fine; only the interactive `resume()` path is broken.

### 5.4 Recovery corrupts `total_recipients` — Medium
In `_run_campaign` (`broadcast.py:541-544`):
```python
total = len(user_ids)            # pending recipients only (resumed) or all (fresh)
if total != (campaign.get("total_recipients") or 0):
    await repo.set_broadcast_status(self._db, cid, "running", total_recipients=total)
```
On a **resumed** campaign, `user_ids` comes from `list_pending_recipients` (line 532) — i.e. only the *not-yet-sent* recipients. So `total` = pending count, and `total_recipients` is overwritten to the pending count, **discarding** already-sent/blocked/failed counts. The final card then reports the wrong universe (a campaign that sent 950/1000 and was interrupted will show `total=50`). The counters themselves (`counters` dict, line 547-553) are loaded correctly from `count_recipients`, but the `total` field and `avg_rate` denominator are wrong.

### 5.5 `RETRY_DELAYED` silently fails recipients instead of requeuing — Medium
`classify_error` (`broadcast.py:178-187`) returns `RETRY_DELAYED` when `retry_after > 60`. `_send_one` then hits the static `status_map` at `broadcast.py:790-794`:
```python
status_map = {
    ErrorKind.PERMANENT_BLOCKED: ...,
    ErrorKind.PERMANENT_FAIL: ...,
    ErrorKind.RETRY_DELAYED: RecipientStatus.FAILED.value,   # <-- failed immediately
}
```
The design doc (`BroadcastEngine.md §2.4`) says `RETRY_DELAYED` should **"requeue for delayed retry"**. The code instead marks the recipient `failed`. With `FLOOD_WAIT` applying *globally* to the bot (doc §2.4 note, line 239), failing recipients instead of requeuing them under the cap means large broadcasts partially fail rather than pacing through a long flood wait.

### 5.6 Pause/resume has no tests — Medium
`docs/broadcast/Phases.md:281` promises `tests/test_bcast_pause.py`, and Phase 2.8.2 documents pause/resume as a deliverable. **The file does not exist** in `tests/`. The pause path (`pause`/`resume`/`_pause_events`) — which contains defects 5.3 above — is therefore uncovered. Present test files confirm the rest exists: `test_broadcast_router.py:59`, `test_broadcaster.py:19`, `test_bcast_scheduling.py:7`, `test_bcast_ab.py:5`, `test_bcast_personalization.py:7`, `test_broadcast_final.py:6` — but no pause test.

### 5.7 Personalization A/B split is never actually applied at delivery — Medium
`_ab_test_split` (`broadcast.py:855-866`) is defined and tested, but `_send_one` (`broadcast.py:705-817`) has **no call to it**: the `mode == "personalized"` branch (line 728-740) renders `campaign.get("content_html")` regardless of `ab_test_id`. The variant chosen at `create_ab_test` (one Broadcast row per variant, line 890-900) is correct, but `_send_one` does not select *which* variant a given user gets — it just sends whatever row it was handed. For equal splits this is fine (each variant is a separate campaign with ½ the audience), but the `_ab_test_split` helper and its test are **dead code**. Either wire it or remove it (`broadcast.py:730-732` even has a `# A/B: ...` comment ending in `pass`).

---

## 6. Security & isolation (the user-facing layer is correct; the new admin/notification layers are not)

### 6.1 User isolation is structurally enforced — (confirmed good)
Every user-facing query filters by `owner_id` at the SQL level: `get_account`/`list_accounts`/`get_job`/`list_jobs` all carry `owner_id` in the `WHERE`. `AccountService.get_session` and `JobManager.create_job` resolve the account through `repo.get_account(db, owner_id, account_id)`, so a forged `account_id` from callback data returns `None` → `ServiceError`. `transfers.py:87` passes `query.from_user.id` as the owner. This matches RULES §4 and is covered by `tests/test_repositories.py::test_user_job_stats_are_scoped_to_owner`. **This is the model the rest of the code should follow.**

### 6.2 The notification inbox breaks that model — High (see §3 A1/A2/A3)
`mark_notification_read`/`dismiss_notification`/`delete_notification` take only `notification_id`. `notifications.py:77,98` call them straight off `cb.data`-parsed ids. Any admin can mark-read/dismiss/delete any other admin's row. The partial fix (`mark_all_notifications_read`) *is* owner-scoped (`repositories.py:908`) — so the omission in the single-notification handlers is an oversight, not a design.

### 6.3 `set_account_status` / `AccountService.mark_status` are owner-unscoped — Low/Medium
`repo.set_account_status` (`repositories.py:173-183`) updates by `id` only. `AccountService.mark_status` (`account_service.py:126-134`) is a **public** method taking `account_id` with no `owner_id`. Today only internal system transitions call it (job_manager marking an account unauthorized/limited — correct, those are account-scoped events, not user input). But it is a **latent cross-owner mutation** landmine: any future handler that calls `mark_status(account_id)` with callback-derived input would violate RULES §4. The signature should require `owner_id` and join on it (as `get_account` does).

### 6.4 Session/crypto is correct — (confirmed good)
`app/security/crypto.py` is the only crypto code (RULES §3 satisfied): Fernet (AES-128-CBC + HMAC) via the `cryptography` library; master key from `SESSIONS_MASTER_KEY` or a `0600` file in a `0700` dir (permissions tightened at `load_or_create_key`). Key rotation (`reencrypt`) is implemented and documented in `README.md`. Login secrets live only in `LoginFlowManager._flows` with a 600s TTL sweeper that discards temp clients (`login.py:209-228`). Confirmed: **no** session string / code / password / master key is ever logged or persisted (the only logger calls are id/counts). **Preserve this.**

### 6.5 Mandatory-subscription gate — see `docs/mandatory-subscription/report.md`
The headline risk (S1) is that `check_membership` (`gate.py:17-42`) **fails closed on the first unverifiable channel**, so if the bot loses admin rights in *one* mandatory entry, **every** non-cleared user is permanently stuck, with the only signal being a `logger.warning` that no operator sees. The report also flags admin self-lockout (S2) and a duplicate-channel `IntegrityError` → 500 (S2). `backups.py` is an empty stub. 15 findings (S1×1, S2×2, S3×6, S4×5) plus "no dedicated tests" (S3).

---

## 7. Database / transaction discipline (Medium)

### 7.1 TOCTOU in the upsert hot path
`upsert_user` (`repositories.py:51-64`) and `upsert_account` (`repositories.py:135-156`) each do a `SELECT` then a conditional `INSERT`/`UPDATE` **with no surrounding transaction**. `upsert_user` is called on *every* update by `UserGateMiddleware` (`middlewares.py:60`) at high fan-out; `upsert_account` is the account-creation path. Under concurrency (two logins for the same Telegram account, or a user starting the bot from two devices simultaneously) the SELECTs can interleave and produce a duplicate `UNIQUE(owner_id, tg_user_id)` violation or a lost name update. RULES §6 says "all multi-row writes … go in a transaction" and "never hand-edit the DB" — these aren't multi-row, but they are read-modify-write sequences that should be atomic. They can be collapsed to a single `INSERT … ON CONFLICT … DO UPDATE` (like `upsert_user` already is on the write side) wrapped in a tx.

### 7.2 `now_iso` is duplicated
`repositories.now_iso` (`repositories.py:25`) and `broadcast.now_iso` (`broadcast.py:932`) are identical implementations. The broadcaster should call `repo.now_iso` (or one shared helper) — two timestamp formatters is a latent format-drift bug. Low, but it exists in the module that writes every audit row.

### 7.3 `count_recipients` counts an unreachable status
`RecipientStatus.DELIVERED` (`broadcast_models.py`) is a declared status, and `count_recipients` (`repositories.py:790-823`) COUNTs it, but `_send_one` only ever sets `sent`/`blocked`/`failed`/`skipped`/`pending` — `delivered` is never produced. The `delivered` aggregation branch is dead. (Low — but it's the third place `delivered` appears as a half-built promise; see also §3 C1.)

### 7.4 Migration discipline is good — (confirmed good)
V1–V10 are strictly append-only, each in its own `BEGIN IMMEDIATE`/`COMMIT` via `_split_statements`, no SQLite-isms (no `AUTOINCREMENT`, no `PRAGMA`/`RANDOM()` in queries, ISO-8601 `TEXT` timestamps, `INTEGER PRIMARY KEY`). `foreign_keys` is enabled by `Database.connect`. This is the model the new code should keep following — and the broadcast `set_broadcast_status` no-CAS gap in §5.2 is a violation of the *spirit* of V2's `transition_job` guard, not the migrations themselves.

---

## 8. Dependencies, lint & CI gates (Medium)

### 8.1 `dateutil` is an undeclared dependency
`app/bot/routers/admin/broadcast.py:128`:
```python
from dateutil import parser as du_parser
```
`python-dateutil` is **not** in `pyproject.toml:16-20`. It is only importable because `aiogram`, via `aiohttp`/`multidict`-adjacent transitive pins, currently drags in `python-dateutil==2.8.2` (confirmed in the sandbox). This is a silent landmine: the schedule-time parser (`_parse_schedule_time`) degrades to `return None` on `ImportError`/`Exception`, so **scheduling silently stops working** if a future dependency reshuffle drops `dateutil`. RULES §8: "No new dependency without … a pin update in `pyproject.toml`." Violates it directly. Same rule applies to any other transitive import the code relies on.

### 8.2 No CI runs the test/lint suite
No `on: [push, pull_request]` workflow runs `pytest`/`ruff`/`mypy`. The only automated execution of tests is the pipeline's **Verify** agent, which runs *conditionally* (only after a successful Review) and *only on the pipeline branch*. A PR that never reaches "clean review" is never test-gated, and a green pipeline can be merged without the *current* `main` ever being validated. For a bot that handles encrypted Telegram sessions, a missing test gate is a process risk worth surfacing.

### 8.3 157 lint errors sit on the tip ungated
`ruff check app/ tests/` → 157 errors. The concrete, non-style ones:
- **F821** `app/bot/texts.py:179` — `Config` used in annotation but never imported (`render_settings(current: Config | None = None)`). Latent under `from __future__ import annotations`, but `typing.get_type_hints()` on that function (or any tool that resolves it) raises `NameError`. The settings screen path is therefore untested with a real `Config`.
- **18× F401 unused imports** in production code: `asyncio` (`account_service.py:10`, `main.py:11`), `aiogram.Bot` (`reporter.py:13` — the *type* used for the Bot is actually `Any`), `AccountStatus` (`accounts.py:45`), `PARSE_MODE` (`stats.py:27`, `jobs.py:21`), `M_DELETED_ACCOUNT` (`jobs.py:20`). RULES §8 forbids dead code explicitly.
- **6× F841 unused locals**, e.g. `technical` in `tg/errors.py:258` (the `UNEXPECTED` login-failure branch computes `technical` then discards it).
- Style (I001 import sorting, P017, E001) is the bulk.

### 8.4 The "no emoji" contract is self-undermined by its own test
`RULES.md §7` forbids emoji and restricts decoration to `✓ ! × › ↺ ⟡`. The codebase uses `👤🔰📢👥💾📋⚙️🗑️🎯🧪🎨` throughout (e.g. `texts.py`, `accounts.py`, `backups.py`). Worse, `tests/test_texts.py:26` defines:
```python
_ALLOWED_SYMBOLS = "…👤🔰💳✅✨📚🔐🏷…"
```
i.e. the whitelist **includes** the emoji the rule bans. So the test that is supposed to enforce §7 *blesses* the violations. The emoji drift is codebase-wide (the mandatory-subscription report F-7), but the inverted test gate is the sharper finding: the compliance check is wired backwards.

---

## 9. Small correctness defects (Low)

| # | Defect | Where |
|---|--------|-------|
| 9.1 | `SessionPasswordNeededError` (2FA required) is mapped to `login_message("CODE_EMPTY")` = "لم يتم إدخال الرمز." ("code not entered"), but the user is being prompted for a **password**. Misleading on the 2FA step. | `app/tg/errors.py:256-257` |
| 9.2 | `Broadcaster.cancel(campaign_id, bot)` keeps an unused `bot` parameter (docstring §4 `cancel(self, campaign_id)` vs implementation) — API drift + invites callers to pass the wrong bot. | `app/core/broadcast.py:292`; `BroadcastEngine.md:381-386` |
| 9.3 | `_parse_schedule_time` reassigns `now = datetime.now(timezone.utc)` twice (`broadcast.py:110`, then `:131`) — the second shadows the first; harmless but the second is only needed because the `dateutil` branch re-reads time. Dead-read risk. | `app/bot/routers/admin/broadcast.py:110,131` |
| 9.4 | `UserGateMiddleware` re-renders `upsert_user` on *every* update (`middlewares.py:60`) — correct for gate freshness, but the `user_joined` event is then `publish`ed **inline** on every first-contact message, blocking the update on N admins' DB writes (same hot-path concern as E1 in §3). | `app/bot/middlewares.py:60-76` |

---

## 10. Test coverage gaps (Medium)

- **Mandatory-subscription has no dedicated tests.** Coverage lives only in `tests/test_dispatcher.py` (8 tests, per the existing report F-15). The admin channel CRUD (`channels.py`), `cb_gate_verify` (success/failure/empty), `reset_user_gates`, the icon inconsistency (F-6), the admin-lockout (F-2) and the 500-on-duplicate (F-9) are all untested.
- **`test_bcast_pause.py` missing** (promised by `Phases.md:281`) — the pause/resume path that §5.3–§5.6 rely on is uncovered.
- **A/B `_ab_test_split` is dead code** (`broadcast.py:855`) — tested but never called. Either wire personalization to it or delete it; a test on a never-called helper is false confidence.
- **No coverage tooling configured.** No `.coveragerc`, no `pytest-cov`, no `addopts=--cov`. 483 passing tests tell you *something* passes, not *what*.
- **`texts.py` `render_settings` is untested with a real `Config`** — consistent with the F821 undefined-name defect.

Positive note: the *core* transfer path is the best-covered area (`test_transfer_engine.py:16`, `test_job_manager.py:18`, `test_preflight.py:19`, `test_resolver.py:19`, `test_errors.py` via `test_errors.py`, `test_login.py:17`). These exercise the class-A path end-to-end offline and should be the bar the new admin subsystems are held to.

---

## 11. Prioritized backlog (fix order)

| Priority | Problem | Severity | Effort |
|----------|---------|----------|--------|
| 1 | Scope drift: reconcile `PRD.md §3` non-goals with shipped admin/broadcast/scheduling/notification/subscription/override features; document admin-vs-user isolation boundary. | High | L |
| 2 | Broadcast: eliminate cancel double-publish (centralize finalization) + add CAS/write-once guard to `set_broadcast_status` mirroring `transition_job`. | High | M |
| 3 | Notifications IDOR: scope `mark_notification_read`/`dismiss_notification`/`delete_notification` by `owner_id`; add cross-admin authorization tests. | High | M |
| 4 | `set_broadcast_status` no-CAS race: see #2 (same fix). | High | M |
| 5 | `resume()` untracked task + missing `test_bcast_pause.py`: track the resumed task in `self._tasks`; add pause tests. | Medium | M |
| 6 | Recovery `total_recipients` corruption: use pending + processed counters, don't overwrite the universe. | Medium | M |
| 7 | Publish the declared-but-missing `job_interrupted` (on boot recovery) and `flood_wait` (on FloodWait) events; add `broadcast_cancelled` type. | Medium | M |
| 8 | Harden `_on_system_event`: per-admin try/except, json-guard `data`, reject unknown `event_type` (fail-secure). | Medium | M |
| 9 | Wire (or delete) `_ab_test_split`; set `delivered=1` after a successful DM + add resend path. | Medium | M |
| 10 | Eliminate TOCTOU in `upsert_user`/`upsert_account` via single-statement `INSERT … ON CONFLICT` in a tx. | Medium | S |
| 11 | Pin `dateutil` (or replace its one use — it's only `du_parser.parse`) in `pyproject.toml` / replace with stdlib. | Medium | S |
| 12 | Fix CI: add an `on: [push, pull_request]` job running `ruff` + `mypy` + `pytest`; fix the 18 unused imports + F821; un-invert the emoji whitelist in `test_texts.py`. | Medium | M |
| 13 | `mark_status` owner-scoping; fix 2FA message in `classify_login_error`. | Low | S |

---

## 12. What is already correct (do not regress)

- Session encryption at rest (Fernet, single-key owner, 0600/0700 perms, rotation documented).
- Layer boundaries (RULES §1) — verified clean.
- User-level isolation — enforced in `repositories` by `owner_id` in every SQL `WHERE`.
- Job finalization — guarded CAS (`transition_job`), write-once final states.
- Transfer engine error classification — single source of truth, bounded retry, no `except: pass`.
- Migration discipline — append-only, transactional, portable SQL.
- Offline test model — no test talks to Telegram.
- Boot recovery ordering — `JobManager.recover()` runs before any job starts (`main.py:74`).

---

## 13. File:line reference index

| Area | Key files & lines |
|---|---|
| PRD non-goals vs code | `PRD.md:54-62`; admin panel `app/bot/routers/admin/*`; broadcast engine `app/core/broadcast.py:218-703`; gate `app/bot/gate.py:17`; notifications `app/core/notifications.py:30-151` |
| Broadcast cancel double-publish | `app/core/broadcast.py:292-324` (cancel), `:541-697` (finalize) |
| `set_broadcast_status` (no CAS) | `app/db/repositories.py:649-670`; call sites `app/core/broadcast.py:264,306,398,439,543,597,655` |
| Broadcast `resume` untracked task | `app/core/broadcast.py:352-374` (create at 361-364); `shutdown` at 456-462 |
| Broadcast recovery total overwrite | `app/core/broadcast.py:541-544` |
| `RETRY_DELAYED` → failed | `app/core/broadcast.py:178-187, 790-794` |
| `_ab_test_split` dead | `app/core/broadcast.py:855-866, 730-732` |
| Notification IDOR | `app/db/repositories.py:898,918,928`; `app/bot/routers/admin/notifications.py:77,98` |
| Undeclared `job_interrupted`/`flood_wait` | `app/core/events.py:105-121`; `app/core/job_manager.py:162-165`; `app/tg/errors.py:18` |
| `notify_on_error` bypass | `app/core/notifications.py:133-151` |
| Inline per-admin fan-out | `app/core/notifications.py:89-105`; `app/core/events.py:75-80` |
| TOCTOU upserts | `app/db/repositories.py:51-64` (`upsert_user`), `:135-156` (`upsert_account`) |
| `mark_status` unscoped | `app/core/account_service.py:126-134`; `app/db/repositories.py:173-183` |
| `dateutil` undeclared | `app/bot/routers/admin/broadcast.py:128`; `pyproject.toml:16-20` |
| F821 undefined `Config` | `app/bot/texts.py:179` |
| Unused imports (examples) | `app/bot/reporter.py:13`, `app/core/account_service.py:10`, `app/main.py:11`, `app/bot/routers/accounts.py:45`, `app/bot/routers/admin/stats.py:27`, `app/bot/routers/jobs.py:20-21` |
| 2FA wrong message | `app/tg/errors.py:256-257` |
| Emoji whitelist inverted | `tests/test_texts.py:26`; `RULES.md:7` |
| `now_iso` duplication | `app/db/repositories.py:25`, `app/core/broadcast.py:932` |
| No CI test/lint job | `.github/workflows/` (only `opencode.yml`, `orchestrator.yml`) |
| Missing `test_bcast_pause.py` | promised `docs/broadcast/Phases.md:281` |

---

*This report was produced by the pipeline's Build Agent as the deliverable for the task "a comprehensive report on the most important problems that must be fixed in TMT as a whole." It is inspection-only; no application code was modified. Existing feature-level deep-dives are referenced rather than re-derived — see `docs/notifications/ReviewReport.md` and `docs/mandatory-subscription/report.md`.*
