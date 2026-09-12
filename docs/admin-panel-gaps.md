# Admin Panel — Gaps & Missing Additions Report

**Status:** Inspection-only (no code changed).
**Scope:** Every router under `app/bot/routers/admin/` (`menu`, `stats`, `users`,
`channels`, `broadcast`, `notifications`, `backups`, `search`, `settings`) and
the data/services they directly depend on.
**Branch:** `agent/feature-34700820433`
**Cross-referenced against:** the DB schema + domain models
(`app/db/migrations.py`, `app/db/repositories.py`, `app/core/models.py`,
`app/core/broadcast_models.py`), the core subsystems (`app/core/job_manager.py`,
`app/core/broadcast.py`, `app/core/notifications.py`, `app/core/settings.py`,
`app/tg/transfer.py`, `app/tg/errors.py`, `app/tg/resolver.py`,
`app/tg/preflight.py`, `app/security/crypto.py`, `app/core/account_service.py`),
`PRD.md`, `RULES.md`, and the design docs under `docs/`.

> This is the admin-panel-narrowed lens. It does **not** duplicate the whole-codebase
> assessment; it focuses on what each admin route *should* expose but does not,
> what data/views it is missing, what controls/actions are absent, what subsystem
> integration is incomplete, and what admin-panel-specific defects exist
> (notably RULES §4 owner-scoping deficits).

---

## 0. Executive summary

The admin panel is **structurally incomplete and partially unreachable**:

- **Three routes are scaffolding-only** with no real logic and no path from the UI
  menu to reach them — `backups.py`, `search.py`, and the admin `settings.py`
  (which only renders a dead "🌐 اللغة" button).
- **Two of those scaffolds are unreachable from the admin menu at all**:
  `admin_menu_kb()` (`app/bot/routers/admin/keyboards.py:14-33`) exposes buttons for
  Users, Mandatory Subscription, Stats, Broadcast, Notifications, and Settings — but
  **not** for Search (`C.SEARCH`) or Backups (`C.BAK`). The routers are mounted in
  `router.py:19,20` but have no menu entry, so an admin can never navigate to them.
- **Four routes are fully/ mostly implemented** (`users.py`, `channels.py`,
  `broadcast.py`, `notifications.py`) but each carries **missing data/views**,
  **missing controls**, or **security defects** detailed below.
- **The `stats` route is a stub of 6 counters** that exposes none of the
  analytics/system-health/audit data the PRD §20 and `BroadcastEngine.md` say the
  admin panel needs.
- **A confirmed IDOR cluster** (the item flagged across `repositories.py`) exists in
  the notifications slice: `mark_notification_read` (`repositories.py:898`),
  `dismiss_notification` (`repositories.py:918`), and `delete_notification`
  (`repositories.py:928`) all take only `notification_id` with **no `owner_id`
  scope** — any admin can read/dismiss/delete *another admin's* notifications.

### Severity legend
- **S1** Critical / blocking / security — must fix before use.
- **S2** High — functionally broken or a real exploit.
- **S3** Medium — missing feature / data gap the panel promises but lacks.
- **S4** Low — polish, consistency, latent, or test-coverage gap.

---

## 1. Menu (`app/bot/routers/admin/menu.py`)

**Current state.** The entry point for the whole panel. `/admin`
(`menu.py:27-38`) gate-checks the caller against `config.admin_id_list` and renders
`render_admin_menu` (`texts.py:101-111`) with four live counters, plus
`admin_menu_kb()` (`keyboards.py:14-33`). The `C.MENU` callback
(`menu.py:41-48`) re-renders the same. `C.CLOSE` (`menu.py:51-53`) deletes a card.

### Missing data/views (menu)
- The welcome banner shows only `user_count`, `jobs_count`, `completed_count`
  (`menu.py:21-23`). PRD §20 wants admin overview data: **blocked users**, accounts
  **by status** (active/unauthorized/limited) with **limited cooldowns**, **running
  vs queued** job queue depth, **failed-job / PeerFlood** counts, and a
  **last-audit-event** timestamp. None of these counters exist as repo functions
  (see §9 — there is no `count_blocked_users`, no `count_jobs_by_status`, no
  `count_accounts_by_status`). The menu therefore can't even *show* a "system
  health" pulse.
- `render_admin_menu` (`texts.py:101`) is a fixed string; it does not surface a
  **per-admin unread-notification badge** (the data exists —
  `count_unread_notifications`, `repositories.py:888` — but the menu never calls it,
  so admins get no at-a-glance alert that the Notification inbox (#6) has unread
  rows). Contrast the BroadcastEngine.md §1 vision banner which shows
  `غير مقروءة: N`.
- No **audit trail teaser**: PRD §20 lists "audit trail" as a panel need, but there
  is no `C.AUDIT` button on the menu and no `list_audit`-style repo reader at all
  (§9).

### Missing controls/actions (menu)
- No way to jump back to the menu from deeper screens is **inconsistent**: most
  sub-routers provide a "› رجوع" → `C.MENU`, but `settings_kb`
  (`keyboards.py:211-217`) and `broadcast_center_kb` (`keyboards.py:138-145`)
  return to `C.MENU` while several live-control handlers (e.g.
  `broadcast.py:549-553`, `571-575`, `593-597`) return to `broadcast_center_kb()`
  instead of `C.MENU` — a minor but real navigation inconsistency. The deeper issue
  (§9) is that there is **no global audit view** reachable from the menu.

### Validation / error handling
- `cmd_admin` (`menu.py:27-38`) returns the private string `"ليس لديك صلاحية."`
  directly on a `message.answer` instead of going through a shared text constant —
  minor, but it is the **one** user-visible Arabic string inlined in a handler
  rather than living in `bot/texts.py` (RULES §7).

---

## 2. Stats (`app/bot/routers/admin/stats.py`)

**Current state.** `cb_stats` (`stats.py:17-33`) renders six counters:
users, mandatory channels, mandatory groups, total jobs, completed jobs
(+ a "🔄 تحديث" refresh and "› رجوع" button via `stats_kb`, `keyboards.py:120-126`).

### Missing data/views (stats) — S1/S3
This is the place the PRD §20 vision says should show "system health (queue
depth, client pool state, FloodWait log), throughput, failure reasons histogram".
**Almost none of it exists:**

| PRD §20 / design-doc want | Current state | Gap |
|---|---|---|
| users: count, **blocked**, activity | `count_users` + `is_user_blocked` exist, but no `count_blocked_users` → blocked subset not shown | S3 |
| **accounts by status** (active/unauthorized/limited) + limited cooldowns | `list_accounts` is owner-scoped (`repositories.py:166`); no global `list_accounts_all` / `count_accounts_by_status`. No `limited_until` aggregation. | **S1** (admin can't see account health) |
| **jobs: throughput, failure-reason histogram, avg duration** | only `count_jobs`/`count_completed_jobs` (`repositories.py:504-514`). No `count_jobs_by_status`, no `sum_skip_reasons`, no duration math, no `count_active_jobs` globally. Jobs schema HAS `skip_reasons` JSON, `started_at`, `finished_at`, `error` — but no admin query reads them. | **S1** |
| **system health: queue depth** | `count_active_jobs_for_user` exists (`repositories.py:375`) but is user-scoped; no global running/queued count. | S2 |
| **client pool state** (connected clients per account, per-account locks) | `ClientPool` (`tg/client_pool.py:25-101`) holds `_clients`/`_locks` in memory only; **never exposed** to any repo or admin endpoint. | S2 |
| **FloodWait log** | `TransferEngine` classifies `FLOOD_WAIT` (`transfer.py:405-429`) and `JobManager` emits a `peer_flood` event, but a *raw* FloodWait (non-PeerFlood) is never recorded anywhere readable — no `flood_wait` SystemEvent is ever published (see notifications §6). No admin-visible flood log. | **S1** |
| **per-user limits for moderation** | `UserSettings` (`core/settings.py:16-54`) stores per-user *transfer* overrides, but the global `Config` limits (`max_running_jobs_per_user`, `max_concurrent_jobs`, `max_members_per_job` — `config.py:32-39`) are **not shown** on this screen, and there is **no** UI to override a user's limits. | S3 |
| **broadcast analytics** (PRD §20 + `BroadcastEngine.md` §2.7) | `broadcasts`/`broadcast_recipients` exist (`migrations.py:140-183`); `count_recipients` (`repositories.py:790`) aggregates one campaign — but `stats` never queries broadcasts at all. No broadcast throughput/failure breakdown for the admin. | S3 |

### Missing controls/actions (stats)
- Only a **refresh** button. No drill-down (e.g. "tap jobs count → job list"),
  no per-status filter, no date range, no way to inspect a failing job/account
  from the stats screen.
- `stats_kb` offers no navigation to the other (missing) admin sections
  (no audit, no account-health, no broadcast-analytics button).

### Validation / error handling
- Clean but minimal; errors from `repo.*` are not wrapped, but the calls are all
  trivial counts so the surface is small.

---

## 3. Users (`app/bot/routers/admin/users.py`)

**Current state.** The most fleshed-out admin route (~281 lines). Paginated user
list (`cb_users_list_root`/`cb_users_page`, `users.py:109-121` via
`list_all_users`, `repositories.py:517`), an **owner-scoped** detail card
(`_user_card_payload`, `users.py:65-90` — correctly joins accounts through the
user's `owner_id`, RULES §4), block toggle with audit
(`cb_user_toggle_block`, `users.py:139-160`), a "send DM to user" flow
(`cb_user_notify`/`admin_notify_text`, `users.py:163-233`), and account deletion
with a confirm step + the active-job guard (`count_active_jobs_for_account`,
`users.py:266`).

### Good (already correct)
- Account deletion checks `count_active_jobs_for_account(db, acc_id) > 0`
  before deleting (`users.py:266`) — matches PRD §J1 failure scenario.
- Detail card uses owner-scoped `list_accounts(db, uid)` (`users.py:75`).
- All int parsing uses `_int_after` returning `None` on malformed input (`users.py:54-62`),
  so a forged callback id can't 500.

### Missing data/views (users) — S2/S3
- **Accounts shown but not manageable.** The card renders each account's
  `account_status_label` (`texts.py:295-296`) and `limited_until`, but there is
  **no control to act on account health**: no "re-validate", no "mark
  unauthorized → re-login", no "discard pooled client". `set_account_status`
  exists (`repositories.py:173`) and `AccountService.mark_status`
  (`account_service.py:126-134`) wraps it, but **no admin handler calls either**.
  An operator looking at a `محدود`/`بحاجة إعادة تسجيل الدخول` account can only
  **delete** it — not unlock or kick a stale session.
- **No per-user job list.** The card shows counts (jobs/completed/failed/active)
  via `count_jobs_for_user`/`count_completed_jobs_for_user`/
  `count_failed_jobs_for_user`/`count_active_jobs_for_user` (`users.py:78-81`),
  but **not the actual jobs**. PRD §J1 / `JobManager.list_jobs`
  (`job_manager.py:298-299`) is owner-scoped; the admin has no
  `list_jobs_for_admin(user_id)` view. The user-facing jobs router
  (`app/bot/routers/jobs.py`) already has the exact rendering
  (`render_jobs_list`/`render_job_card`/`job_detail`) the admin could mirror.
- **No audit trail for the user.** `audit_log` rows exist (every
  block/notify/delete writes via `repo.audit`, `users.py:152,222,270`) but the
  admin card shows **none** of them — there is no `list_audit(owner_id=uid)`
  reader (§9) and no `C.AUDIT` callback. An admin investigating a user sees zero
  history.
- **Account list is capped at `display_name`/`tg_username`/`status` only.** No
  `tg_user_id`, `added_at`, or `last_validated_at` column is rendered
  (`user_detail_kb`/`render_user_card` — `texts.py:121-151`). `AccountRecord`
  carries `added_at`/`last_validated_at` (`repositories.py:79-89`) but they're
  discarded in the card.

### Missing controls/actions (users) — S3
- No **"view jobs for this user"** button → job list.
- No **"view audit log for this user"** button.
- No **account-status action** (re-validate / reset to active / mark unauthorized).
- No **"resend / test DM"** beyond the free-form notify flow.
- No **"force-cancel all running jobs for this user"** moderation action (the data
  to detect them exists — `count_active_jobs_for_user` — but there's no bulk
  cancel, and `JobManager.cancel_job` is owner-scoped so the admin can't call it
  by owner_id anyway).

### Missing subsystem integrations (users)
- `AccountService` is available as `dp["accounts"]` (`bot/__init__.py:61`) but
  `users.py` does **not** inject it — it reaches into `repo` directly and inlines
  the session-decrypt concern is avoided, but it also duplicates the
  `count_active_jobs_for_account` gate that `AccountService.remove`
  (`account_service.py:112`) already centralizes. The admin delete path
  (`cb_account_delete_ok`, `users.py:254-280`) re-implements that guard instead of
  delegating to `accounts.remove(uid, acc_id)`.

### Validation / error handling
- `cb_user_toggle_block` (`users.py:139-160`) does not check the
  **active-job guard before blocking**: it only flips `is_blocked`. Blocking a
  user with a *running* transfer job is allowed — the job keeps running under a
  blocked user (the gate only re-checks on new interactions). PRD J1 does not
  explicitly forbid this, but it's an inconsistency: deleting an account blocks on
  active jobs (`users.py:266`) but **blocking a user does not**.
- `admin_notify_text` (`users.py:208-221`) catches `TelegramAPIError` and reports
  "قد يكون قد حظر البوت" — good, but it swallows the specific error class that
  feeds the notification system's `NotificationService` (no `SystemEvent` is
  published for an admin-initiated outreach). Minor.

---

## 4. Channels / Mandatory Subscription (`app/bot/routers/admin/channels.py`)

**Current state.** Full two-tab CRUD for mandatory entries (`channels.py:86-220`):
tabs, add (with the invite-link fallback FSM), toggle, delete-with-confirm,
each re-calling `reset_user_gates`. This route is **functionally complete** and
was already audited in detail in `docs/mandatory-subscription/report.md`.

### Missing data/views (channels) — S3/S4
- **No audit trail for channel changes** (the prior report's F-11,
  `mandatory-subscription/report.md:235-251`). `add_channel`/`toggle_channel`/
  `delete_channel` (`channels.py:138-220`) never call `repo.audit`, unlike
  `users.py`. Compare `users.py:152,222,269-276` which audit every mutation.
- **No `created_at`/`updated_at`/`created_by` on `channels`** (schema
  `migrations.py:117-123, 229-232` — the table has only `id, channel_id, title,
  invite_link, is_active, type`). There is nowhere to render *when* an entry was
  added or by whom — a stats/analytics gap, not a rendering gap.
- **`reset_user_gates` is silent** (`repositories.py:494-496`): it bulk-clears
  `gate_cleared` for all users with no audit row, so the operator can't see *that*
  a reset happened (prior report F-5/F-11).

### Missing controls/actions (channels) — S4
- `add_channel` (`repositories.py:439-445`) is a plain `INSERT` with
  `UNIQUE(channel_id)` (`migrations.py:119`) but **no `ON CONFLICT`** — adding a
  duplicate raises an unhandled `IntegrityError` (prior report F-9,
  `mandatory-subscription/report.md:200-214`). The fix is idempotent upsert + a
  "updated" message, not new UI, but the **control** (add duplicate) currently
  500s silently.
- `toggle_channel`/`delete_channel` return a `bool` affected-row count
  (`repositories.py:448-459`) that the handlers **ignore** — a stale id after a
  delete re-renders the (now-empty) tab silently rather than surfacing
  "not found" (prior report F-10).

### Validation / error handling
- `entry_ref_entered` (`channels.py:128-132`) catches `Exception` around
  `bot.get_chat` and shows `M_ENTRY_NOT_FOUND` — acceptable but broad (catches
  programming errors too). No per-exception classification like
  `tg/errors.py:classify_login_error`.

**Verdict:** Channels is the one admin route that is *complete*; its gaps are the
auditable items in `mandatory-subscription/report.md` (audit trail, idempotency,
timestamps) which are explicitly out of scope for this file's re-audit.

---

## 5. Broadcast (`app/bot/routers/admin/broadcast.py`)

**Current state.** The most substantial route (~597 lines) — full broadcast-center
dashboard, compose FSM, audience-filter builder, dry-run/preview, test-send,
schedule, send-now, history, view, and live pause/resume/cancel. Backed by
`Broadcaster` (`core/broadcast.py`) and `AudienceResolver`/`count_audience`
(`core/broadcast.py:134-161`). This implements the `BroadcastEngine.md` §2-§8
spec.

### Missing data/views (broadcast) — S3
- **Broadcast history is missing key statuses.** `_render_history_page`
  (`broadcast.py:467-501`) queries
  `WHERE status IN ('completed','cancelled','failed')` only — **running,
  scheduled, interrupted, and draft** campaigns never appear in history. A
  campaign stuck in `interrupted` (boot-recovery failure) or `scheduled` (sweeper
  never fired) is invisible to the admin in the history view; they must be
  caught via the dashboard's `list_broadcasts(status=...)` (limited to 50 each,
  `broadcast.py:191-194`).
- **`render_bcast_progress` (`texts.py:648`) is dead code** — exported in
  `__all__` (`texts.py:50`) and tested (`tests/test_broadcast_router.py:310`),
  but the engine's `_send_admin_card` (`broadcast.py:819-840`) uses the stub
  `_progress_text` (`broadcast.py:202-213`) instead, per the comment at
  `broadcast.py:206` ("Phase 4 replaces this"). So the **live progress card shown
  to the admin during a run is the minimal placeholder**, not the richer
  `render_bcast_progress` card (which would show per-recipient counters, rate,
  and percentage). The richer renderer exists but is never wired.
- **No per-recipient failure inspection.** `count_recipients`
  (`repositories.py:790`) aggregates status counts for a campaign, but the
  `broadcast_recipients.last_error` column (`migrations.py:169`) is **never read
  anywhere** — there is no "top error reasons" or "failed recipients" view, even
  though `BroadcastEngine.md` §2.7/§6.5 explicitly promises "top 3 error reasons
  (from `last_error`)" and "per-recipient error" analytics. The admin can see
  counters but not *why* recipients failed.
- **`render_bcast_summary` (`texts.py:668`) omits the source message context.** It
  shows counters/status/error — but not the originating `source_chat_id`/
  `source_message_id` (`migrations.py:145-146`) or `mode`/`content_html`, so the
  admin can't tell from the summary whether a campaign was `copy` or
  `personalized`, or what content was sent. The data is in the row
  (`get_broadcast`, `repositories.py:644`).

### Missing controls/actions (broadcast) — S3
- **No A/B-test creation UI.** `Broadcaster.create_ab_test` (`broadcast.py:868-906`)
  and the `ab_tests`/`broadcast_templates` tables exist (`migrations.py:202-219`)
  and `ab_test_id` on `broadcasts`, but **no router/handler/keyboard calls
  `create_ab_test`** — it is entirely unreachable from the panel. `BroadcastEngine.md`
  §2.3/§2.10 promises A/B testing as a first-class feature, but the panel can never
  create one.
- **No template management UI.** `broadcast_templates` (`migrations.py:210-217`)
  exists but has no admin reader/writer — no way to create/reuse templates from
  the panel (Phase 6 `test_bcast_personalization` exercises the engine, not the UI).
- **No "re-run from draft" beyond `cb_bcast_draft_resume`.** Resuming a draft
  goes back to the *target* builder, but there is no action to **duplicate** an
  existing sent campaign, or to **re-send** a failed campaign to only its failed
  recipients (the `broadcast_recipients` table is keyed by `(broadcast_id,
  user_id)` with per-status rows — re-sending to failed ones is possible by
  re-running `resolve_audience` over a failed-only filter, but no such filter or
  UI exists).
- **No global campaign management across all statuses.** The dashboard pages
  drafts/scheduled/running/completed in 50-row caps; there is no single
  "campaigns" table with status filters and pagination (the history view is
  terminal-status-only, see above).

### Missing subsystem integrations (broadcast)
- `Broadcaster` is injected into workflow data (`bot/__init__.py:67`, `main.py:100`)
  and the live-control handlers (`broadcast.py:381-409`, `504-528`, `534-597`)
  correctly receive `broadcaster: Broadcaster | None`. The integration is
  **functionally present** — this is the one route where subsystem wiring is
  complete. The gaps are above (dead `render_bcast_progress`, unreachable
  A/B-test, un-read `last_error`).
- **Double-publish defect on cancel (confirmed by `docs/notifications/ReviewReport.md`
  B1).** `Broadcaster.cancel()` (`broadcast.py:292-324`) publishes
  `SystemEvent(event_type="broadcast_failed", …)` (line 315-323), then the worker's
  `_run_campaign` finalization (`broadcast.py:651-697`) sets
  `final_status = "cancelled"` and *also* publishes a
  `broadcast_completed` event (line 677-697) with a "Failed" title (`"×|فشل البث"`).
  One cancel → **two inbox rows** with contradictory event types
  (`broadcast_failed` + `broadcast_completed`) and a title saying "Failed" on a
  cancellation. `SYSTEM_EVENTS` (`events.py:117-118`) has no `broadcast_cancelled`
  event type to resolve this cleanly.
- **`cancel()` swallows the `Broadcaster` return on no-op.** `cb_bcast_cancel_live`
  (`broadcast.py:534-553`) calls `broadcaster.cancel(...)` but ignores whether it
  returned `False` (campaign not running) — it still shows `render_bcast_summary`.
  Minor UX, but means cancel on a non-running campaign gives no feedback that it
  did nothing.

### Validation / error handling
- `_parse_schedule_time` (`broadcast.py:101-154`) returns `None` for bad input and
  the handler shows a prompt (`broadcast.py:432-437`) — good, no 500. It accepts
  ISO, `+2h`/`+1d`/`+30m`, and Arabic keywords (`غداً`/`اليوم`/`الآن`).
- `_int_after` guards malformed campaign ids (`broadcast.py:53-61`).
- `_send_admin_card` (`broadcast.py:819-840`) swallows card-edit failures
  (`except Exception: logger.warning`) — correct for a progress card (don't kill
  the worker).

---

## 6. Notifications (`app/bot/routers/admin/notifications.py`)

**Current state.** Inbox screen with list/pagination (`cb_notify_list`/
`cb_notify_page`), mark-read (`cb_notify_read`), mark-all-read
(`cb_notify_mark_all`), dismiss (`cb_notify_dismiss`), settings
(`cb_notify_settings`/`cb_notify_toggle`), backed by `NotificationService`
(`core/notifications.py`) and `SYSTEM_EVENTS` (`events.py:105-121`). This was the
subject of a dedicated review in `docs/notifications/ReviewReport.md`.

### Security defects (IDOR) — S1/S2  *(the repositories.py:898,918,928 cluster)*

This is the admin-panel-specific item #4 referenced by the brief. Three repo
functions take **only `notification_id`** with no `owner_id` scope, letting any
admin act on another admin's row:

| Function | Repo line | Handler call site | Owner-scoped? |
|---|---|---|---|
| `mark_notification_read` | `repositories.py:898-905` | `notifications.py:77` | **No** — `UPDATE … WHERE id=?` |
| `dismiss_notification` | `repositories.py:918-925` | `notifications.py:98` | **No** — `UPDATE … WHERE id=?` |
| `delete_notification` | `repositories.py:928-933` | (not wired to any handler) | **No** — `DELETE … WHERE id=?` |

By contrast, `cb_notify_mark_all` (`notifications.py:86`) and `_render_page`
(`notifications.py:39`) **do** derive `admin_id = cb.from_user.id` — so the IDOR is
an inconsistency/oversight, not a design choice. RULES §4 is explicit:
"ownership is never taken from message text" and "joined against `owner_id` in the
repository call". A malicious admin can flip every other admin's read state and
dismiss their notifications. The `delete_notification` gap is **latent** (no handler
calls it yet) but would inherit the IDOR immediately if wired.

### Missing data/views (notifications) — S2/S3  *(ReviewReport G1/G2)*
- **Notification body + `data` are unreachable from the UI.**
  `render_notifications_list` (`texts.py:760-786`) renders only `title` +
  `event_type` label per row. The `body` — which for `user_joined` carries the
  user's name/username/id, and for job/broadcast events carries invite/fail/skip
  counters and error details — is **never displayed**. There is no `NOTIFY_OPEN`
  callback and no per-row "view" button in `notifications_list_kb`
  (`keyboards.py:220-249` — only ✓ read and × dismiss per row). The dedicated
  `render_notification_card` (`texts.py:738-757`) — which *would* show body,
  event type, timestamp, severity — is **defined and tested
  (`tests/test_texts.py:349`) but never called by any router** (ReviewReport G2).
  Net: the persistent inbox delivers only one-line titles, undermining the
  system's stated purpose ("no record of what they were told",
  `NotificationSystem.md` §1).
- **`delivered` column is a dead flag (ReviewReport C1).** `notifications.delivered`
  (`migrations.py:258`) defaults `0` and is documented as "set to 1 on DM success"
  (`notifications.py:40`), but `_try_dm` (`notifications.py:109-129`) never updates
  it and **no `mark_notification_delivered` repo function exists**
  (`repositories.py` — only `create_notification`/`mark_notification_read`/`dismiss`/
  `delete`/`set_notification_setting`/`is_notification_enabled` exist). The
  "resend failed DMs" feature (Phase 2, `NotificationSystem.md` §5) cannot work and
  cannot even tell attempted vs. unattempted.

### Missing controls/actions (notifications) — S3
- No "Resend" button for `delivered=0` rows (ReviewReport C4).
- No per-notification "View detail" action (the body/data viewer, G1).
- No "Delete" (GDPR cleanup) action exposed in the UI, even though
  `delete_notification` exists (`repositories.py:928`) — ReviewReport G6.

### Validation / error handling
- `cb_notify_read`/`cb_notify_dismiss` ignore the bool return of the repo function
  (ReviewReport F5) — a no-op (already-read/already-dismissed/missing) re-renders
  page 0 with no feedback, and **bounces the user to page 0** even if they were on
  page 2 (`notifications.py:78,99`).
- `cb_notify_page` accepts negative page numbers (`adm:notify:p:-1` → `page=-1`
  → `offset=-10`) with no clamping (ReviewReport F3, `notifications.py:61-66`).
  SQLite tolerates it today but it's an undocumented dependency.
- **TOCTOU** between `count_unread_notifications` and `list_notifications` in
  `_render_page` (`notifications.py:40-44`) — two separate queries, so a live
  event can make the unread badge disagree with the list (ReviewReport F2).
- **`NOTIFS_PER_PAGE`** is duplicated: constant at `notifications.py:21` but the
  keyboard hardcodes `10` at `keyboards.py:240` (`if len(notifications) >= 10:`)
  (ReviewReport F1).
- **Fail-open on unknown event types (ReviewReport D1, `notifications.py:149-151`):**
  `_config_enabled` returns `True` for any event type not explicitly listed, and
  `is_notification_enabled` defaults `True` for missing settings rows. Since the
  settings UI only renders toggles for `SYSTEM_EVENTS`
  (`keyboards.py:259`, `texts.py:801`), a `SystemEvent` with an unregistered
  `event_type` is shown to all admins with no way to mute it.
- **`account_unauthorized` bypasses `notify_on_error` (ReviewReport D2,
  `notifications.py:142-151`):** `flood_wait`/`peer_flood` map to
  `notify_on_error`, but `account_unauthorized` (and `account_added`/
  `account_removed`) fall through to the unconditional `return True` (line 151).
  An operator who sets `NOTIFY_ON_ERROR=false` to reduce noise is still paged on
  unauthorized accounts but not on PeerFlood — inconsistent.
- **Per-admin DB error aborts all admins (ReviewReport C2,
  `notifications.py:89-105`):** the loop over `admin_ids` has no per-admin
  try/except; a single DB failure for admin 1 propagates out of
  `EventBus.publish` (`events.py:75-80`) and stops rows/DMs for admins 2..N. Since
  `job_started`/`broadcast_started` publish from hot paths
  (`job_manager.py:342`, `broadcast.py:281`), this also delays job/broadcast
  startup (ReviewReport E1).
- **Missing `NOTIFY_SETTINGS` callback constant (ReviewReport G5,
  `callbacks.py:60-66`):** the settings route is built inline as
  `f"{C.NOTIFY}:settings"` in both the router (`notifications.py:102`) and the
  keyboard (`keyboards.py:246`) — fragile, no single source of truth.

### Test-coverage gap
- **No cross-admin authorization tests** (ReviewReport I1,
  `tests/test_notify_router.py`): all handler tests seed notifications for
  `owner_id=1001` and act as `user_id=1001` — no test asserts that admin A *cannot*
  mutate admin B's notification. The IDOR is therefore untested.

---

## 7. Broadcasts (live controls) — see also §5

The live-control buttons (`broadcast.py:534-597`, `BCAST_CANCEL_LIVE`/
`PAUSE_LIVE`/`RESUME_LIVE`) call into `Broadcaster` correctly, but:
- No **force-stop** semantics if `Broadcaster` is `None` (the handlers accept
  `broadcaster: Broadcaster | None` and silently no-op, `broadcast.py:397-398`,
  `545`, `567`, `589`). The DB status is never corrected if the service is down,
  so a "running" campaign whose in-memory task died is left stuck in `running`
  until boot-recovery.
- These three handlers are structurally identical (parse id, call one method,
  re-render summary) — a duplication the brief's "missing controls" framing
  tolerates, but worth noting as a maintenance hazard.

---

## 8. Backups (`app/bot/routers/admin/backups.py`)

**Current state.** Scaffolding-only (16 lines):
```python
@router.callback_query(F.data == C.BAK or F.data.startswith(C.BAK))
async def cb_backups(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "💾|إدارة النسئف الاحتياطية")
```
(`backups.py:13-16`). No `db` dependency, no controls, no logic.

### Missing data/views / controls / integrations (backups) — S1
- The `backups.py` route is **the empty stub** explicitly called out by the brief.
  It should, per PRD §7.6 and §J3 (DB/backup security) and the
  `BroadcastEngine.md`-style data needs, expose:
  - A list of **existing backups** (the `data/` dir holds `bot.db` +
    `keys/master.key`, `config.py:71-76`, `data_dir` `config.py:28`). There is **no
    table and no repo function** recording backup files, timestamps, sizes, or
    success/failure — `audit_log` has no `backup_created`/`backup_failed` events.
  - A **"create backup"** action: snapshot `bot.db` (WAL) → a timestamped file,
    mark the master key read-only for the copy, and write an `audit_log` row. PRD
    §7.7 stresses the key/DB coupling; a backup UI that doesn't pair DB + key is
    useless.
  - A **"download/restore"** flow with owner-scoping (only admins) and
    confirmation, since restore overwrites the live DB.
- **Validation/error handling:** none — the route doesn't even import `repo`.
- **Reachability:** there is no `BAK` button in `admin_menu_kb()`
  (`keyboards.py:14-33`), so even the stub text is unreachable from the panel.
  `C.BAK`, `C.BAK_NEW`, `C.BAK_OPEN` exist (`callbacks.py:69-71`) but
  `BAK_NEW`/`BAK_OPEN` have no handler and are never referenced.

---

## 9. Search (`app/bot/routers/admin/search.py`)

**Current state.** Scaffolding-only (16 lines):
```python
@router.callback_query(F.data.startswith(C.SEARCH))
async def cb_search(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "🔎 أرسل معرّف المستخدم أو معرّف العملية للبحث.")
```
(`search.py:13-16`). No `db` dependency, no FSM, no result rendering.

### Missing data/views / controls / integrations (search) — S2
- The prompt asks for a "user id or job id" but **there is no handler for the
  submitted value** — nothing listens for the text after this prompt, and the
  `SEARCH`/`SEARCH_PAGE`/`SEARCH_RESULTS` constants (`callbacks.py:74-76`) are
  never wired to any callback handler.
- **No cross-user search at all.** `get_user` (`repositories.py:526`) and
  `list_jobs` (`repositories.py:271`) are the only lookup primitives, and both are
  effectively user-scoped or single-row. The admin needs a search that, given a
  TG id, returns: the user row + accounts + recent jobs + relevant `audit_log`
  rows. There is **no `list_audit` reader** and no joined user/account/job lookup
  for an arbitrary id.
- **No validation/error handling:** the route doesn't even import `repo`.
- **Reachability:** no `SEARCH` button in `admin_menu_kb()` (`keyboards.py:14-33`);
  the `SEARCH` router is mounted (`router.py:17`) but has no menu entry — truly
  unreachable.

### Missing subsystem integrations (search)
- Should integrate `repo.get_user`, `repo.list_accounts` (owner-scoped, so
  `search.py` must pass the searched `uid` as `owner_id` — a good place to *model*
  the owner-scoping the rest of the panel gets wrong, see §6).
- Should show `audit_log` for the user/job — but `audit_log` has **no reader**
  function (§9 cross-cutting). The table exists (`migrations.py:54-62`,
  indexed `idx_audit_ts`, `migrations.py:67`) but only the `audit` *writer*
  (`repositories.py:402-426`) exists.

---

## 10. Settings (`app/bot/routers/admin/settings.py`)

**Current state.** Scaffolding-only (17 lines): renders `"⚙️ الإعدادات"` with
`settings_kb()` (`keyboards.py:211-217`).

### Missing data/views / controls / integrations (settings) — S3/S4
- **`settings_kb` exposes a dead language button.** It has only
  `🌐 اللغة → C.SET_LANG` plus "› رجوع" (`keyboards.py:213-214`). The callback
  constants `SET_LANG`/`SET_LANG_AR`/`SET_LANG_EN` exist
  (`callbacks.py:56-58`), but **there is no handler registered for any of them**
  anywhere in the app (grep for `F.data == C.SET_LANG` / `startswith(C.SET_LANG)`
  → zero matches). Clicking the button produces an **unanswered callback** —
  Telegram shows "loading…" forever. This is the admin panel's one visible
  dead button (S4 maintainability, but a real UX defect).
- **No global config view.** `Config` (`config.py:15-87`) has the full set of
  transfer/broadcast/notification knobs, but `settings.py` shows **none** of
  them. PRD §20 wants "per-user limits for moderation" visibility; the admin
  settings screen shows zero limits.
- **No per-user `UserSettings` moderation.** `UserSettings`
  (`core/settings.py:16-54`, `OVERRIDABLE_KEYS` = `max_members_per_job`,
  `invite_delay_seconds`, `flood_wait_max_seconds`, `job_timeout_seconds`) is
  injected as `dp["settings"]` (`bot/__init__.py:66`) and has `all_values()`/
  `set()`/`reset()` — but **no admin route reads or overrides it**. The
  per-user overrides set by the user-facing `routers/settings.py` are invisible
  to the operator. There's no `UserSettings.get(owner_id)` admin view and no
  admin `reset(owner_id)` action.
- **Missing controls.** Should offer: global config summary, per-user limit
  override + reset, notification kill-switches (`notify_on_*` — `config.py:54-57`),
  and broadcast knobs (`bcast_*`). The user-facing settings router
  (`routers/settings.py:40-97`) already has the exact `SettingsFSM` +
  `settings_keyboard` pattern the admin could mirror for a *per-user* override UI;
  it simply isn't reached from the admin side.
- **Reachability:** the `SETTINGS` button *is* on the menu (`keyboards.py:30`),
  so this route is reachable — but it's a dead end.

### Validation / error handling
- None — the route doesn't import `repo` or `UserSettings`.

---

## 11. Cross-cutting gaps (infra the panel lacks)

These are missing **data sources / repo reads** that multiple admin routes need:

1. **No `audit_log` reader.** `audit_log` exists (`migrations.py:54-62`) and is
   written by `users.py`/`account_service.py`/`job_manager.py`/`broadcast.py`
   via `repo.audit` (`repositories.py:402-426`), but there is **no `list_audit`**,
   no `audit_for_user`, no `audit_for_job`. The panel can *write* history but
   cannot *show* it — blocks the audit-trail need in PRD §20 and the mandatory
   channels audit-trail need (§4).
2. **No global account listing.** `list_accounts` is owner-scoped
   (`repositories.py:166`); there is no `list_accounts_all(db, *, status=…)`
   for the admin to see all accounts by health (`migrations.py:17-30` has
   `status`/`limited_until` columns). Stats §2 and Users §3 both starve on this.
3. **No global job listing / aggregation.** `list_jobs` is owner-scoped
   (`repositories.py:271`); no `list_jobs_all`, no `count_jobs_by_status`,
   no `sum_skip_reasons` (the JSON `skip_reasons` column `migrations.py:46` is
   never aggregated for admin analytics), no duration math
   (`started_at`/`finished_at` exist `migrations.py:49-50` but are unused by
   admin queries). Stats §2 starves on this.
4. **No client-pool / system-health reader.** `ClientPool`
   (`tg/client_pool.py:25-101`) keeps `_clients`/`_locks` in memory only; nothing
   exposes "connected clients" / "per-account lock state" to the panel
   (`migrations.py`/`repositories.py` have no pool-state table). Stats §2
   "client pool state" is impossible today.
5. **No `broadcast_recipients` per-recipient reader.** `count_recipients`
   (`repositories.py:790`) aggregates; there's no `list_failed_recipients(bid)`
   to read `last_error`/`last_attempt_at` (`migrations.py:169-170`) — the
   "top 3 error reasons" (`BroadcastEngine.md` §2.7/§6.5) can't be built.

### Owner-scoping / IDOR pattern (RULES §4)
The panel is **inconsistent** about owner-scoping:
- ✅ `users.py` detail card joins accounts through `owner_id` (`users.py:75`).
- ✅ `users.py`/`common.py` re-derive admin identity from `cb.from_user.id` for
  mark-all and page renders.
- ❌ `notifications.py` reads/dismiss/delete operate on `notification_id` alone
  with no `owner_id` (§6, the flagged `repositories.py:898,918,928` cluster).
- ✅ `channels.py` entries are global (no owner) — correct per PRD A1/J4 (the
  `channels` table has no `owner_id`, `migrations.py:117`), but the prior report
  flags the lack of audit on these global mutations.
- The broadcast handlers scope by `campaign_id` only (`get_broadcast`,
  `repositories.py:644`) — broadcasts are **admin-owned** (`broadcasts.admin_id`
  FK `migrations.py:143`), but the router does **not** verify the acting admin
  matches `campaign["admin_id"]`. Any admin in `ADMIN_IDS` can cancel/view another
  admin's campaign. Not strictly an IDOR across *users* (admins are a trusted
  set per PRD), but a deviation from the "verify ownership" rule — and the
  `NotificationSystem.md` §7.1 explicitly warns callback data is user input even
  for admin routes.

---

## 12. Prioritized "missing additions" list

Ordered by impact/surface area exposed to an admin operator.

| # | Priority | Route(s) | Missing addition | Ref |
|---|---|---|---|---|
| 1 | **S1** | backups | Implement real backup view + create/restore actions; write `audit_log` rows; pair DB+master-key. Route is an empty stub. | `backups.py:13-16`; `callbacks.py:69-71`; `config.py:71-76`; `migrations.py:54-62` |
| 2 | **S1** | notifications | Scope `mark_notification_read`/`dismiss_notification`/`delete_notification` by `owner_id` (IDOR fix). | `repositories.py:898,918,928`; `notifications.py:77,98` |
| 3 | **S1** | stats | Global account-health view (`list_accounts_all`, status aggregation, limited-until) + jobs analytics (by-status, failure histogram, durations, throughput). | `stats.py:17-33`; `repositories.py:159-189,220-271`; `migrations.py:17-30,40-52` |
| 4 | **S1** | notifications | Wire `render_notification_card` to a `NOTIFY_OPEN` action so body/`data` are reachable; the inbox is title-only today. | `texts.py:648,738`; `notifications.py:38-49`; `keyboards.py:220-249` |
| 5 | **S1** | broadcast | Fix cancel double-publish: suppress the worker's `broadcast_completed`/`broadcast_failed` publish when `cancel_evt` is set, or add a `broadcast_cancelled` event type. | `broadcast.py:292-324,651-697`; `events.py:105-121` |
| 6 | **S1** | stats | Expose flood-wait log + client-pool state to the admin (engine never records readable flood events; pool is memory-only). | `transfer.py:405-429`; `client_pool.py:25-101`; `events.py:119` |
| 7 | **S2** | search | Build a real cross-user search: id → user + accounts + jobs + audit rows. Requires `list_audit` reader. | `search.py:13-16`; `callbacks.py:74-76`; `repositories.py:526,166,271` |
| 8 | **S2** | users | Add per-user job list + audit-log view + account-status actions (re-validate / mark unauthorized) on the user card. | `users.py:65-90`; `job_manager.py:298`; `repositories.py:88` (missing `list_audit`) |
| 9 | **S2** | users | Delegate account deletion to `AccountService.remove` (already owns the active-job guard) instead of re-implementing it (`users.py:266`). | `users.py:254-280`; `account_service.py:108-124` |
| 10 | **S3** | stats | Show blocked-user count + per-user-limit moderation view (read `UserSettings` overrides, allow reset). | `stats.py:17-33`; `settings.py:core/settings.py:35-54`; `config.py:32-39` |
| 11 | **S3** | broadcast | Wire `render_bcast_progress` as the live card (engine currently uses the stub `_progress_text`). | `broadcast.py:202-213,819-840`; `texts.py:648` |
| 12 | **S3** | broadcast | Add per-recipient failure inspection: read `broadcast_recipients.last_error`/`last_attempt_at` for the promised "top error reasons". | `broadcast.py:534-597`; `repositories.py:790` (no per-recipient reader); `migrations.py:169-170` |
| 13 | **S3** | broadcast | Expose A/B-test creation (`Broadcaster.create_ab_test`) + template management (`broadcast_templates`) in the UI — both exist in code but are unreachable. | `broadcast.py:868-906`; `callbacks.py:69-71`(no BCAST_AB_TEST constant); `migrations.py:202-219` |
| 14 | **S3** | broadcast | Broaden history to running/scheduled/interrupted (not just completed/cancelled/failed) and add status filters. | `broadcast.py:467-479` |
| 15 | **S3** | settings | Make the `🌐 اللغة` button work or remove it; add a real settings screen (global config summary + notification kill-switches `notify_on_*`). | `settings.py:14-17` (no SET_LANG handler); `callbacks.py:56-58`; `config.py:53-57` |
| 16 | **S3** | notifications | Update `delivered=1` after a successful DM + add a "Resend" UI (currently `delivered` is a dead column). | `notifications.py:109-129`; `repositories.py:898` (no `mark_notification_delivered`) |
| 17 | **S3** | channels | Make `add_channel` idempotent (`ON CONFLICT`) + add `audit_log` on add/toggle/delete + `created_at`/`updated_at` columns. | `channels.py:138-171`; `repositories.py:439-445`; `migrations.py:117-123` |
| 18 | **S4** | notifications | Batch the 14 per-admin settings queries into one (`get_notification_settings`); clamp negative pages; fix the duplicated `NOTIFS_PER_PAGE`; preserve page on read/dismiss. | `notifications.py:108-110,130-133`; `keyboards.py:240`; `repositories.py:950-963` |
| 19 | **S4** | notifications | Add `NOTIFY_SETTINGS` constant; stop fail-open on unknown event types; guard `json.dumps(data)` in `create_notification`. | `callbacks.py:60-66`; `notifications.py:149-151`; `repositories.py:853` |
| 20 | **S4** | menu / all | Add Search + Backups entry points to `admin_menu_kb` so the mounted-but-unreachable routers become reachable. | `keyboards.py:14-33`; `router.py:17,19` |
| 21 | **S4** | menu | Move the inlined `"ليس لديك صلاحية."` to `texts.py` (RULES §7). | `menu.py:32` |
| 22 | **S4** | stats | Add `idx_notifications_owner_created` and other analytics indexes; add composite indexes for the new stats queries. | `migrations.py:270-272` (existing indexes; none cover the new queries) |

---

## 13. File:line reference index

| Area | Files & representative lines |
|---|---|
| Admin router mount | `app/bot/routers/admin/router.py:1-21` (includes menu/stats/broadcast/users/search/channels/backups/settings/notifications) |
| Admin menu keyboard (no Search/Backups button) | `app/bot/routers/admin/keyboards.py:14-33` |
| Admin menu handlers | `app/bot/routers/admin/menu.py:19-54` |
| Admin menu text | `app/bot/texts.py:101-111` (`render_admin_menu`) |
| Stats route (stub) | `app/bot/routers/admin/stats.py:17-33`; `keyboards.py:120-126` |
| Users route | `app/bot/routers/admin/users.py:1-281`; `keyboards.py:36-80` |
| Channels route | `app/bot/routers/admin/channels.py:86-220`; `keyboards.py:83-117` |
| Broadcast route | `app/bot/routers/admin/broadcast.py:1-597`; `keyboards.py:129-173` |
| Broadcast progress (stub vs dead renderer) | `broadcast.py:202-213` (`_progress_text`), `broadcast.py:819-840` (`_send_admin_card`) vs `texts.py:648` (`render_bcast_progress`, defined but unused) |
| Broadcast double-publish on cancel | `broadcast.py:292-324` (`cancel`) + `broadcast.py:651-697` (worker finalize) |
| Notifications route | `app/bot/routers/admin/notifications.py:19-139`; `keyboards.py:220-249` |
| Notification IDOR (owner_id missing) | `repositories.py:898` (`mark_notification_read`), `repositories.py:918` (`dismiss_notification`), `repositories.py:928` (`delete_notification`); call sites `notifications.py:77,98` |
| Notification body unreachable | `texts.py:760-786` (list omits body) + `texts.py:738` (`render_notification_card`, defined/tested, never called) |
| Notification delivered dead column | `migrations.py:258`; `notifications.py:109-129` (`_try_dm` never updates); no `mark_notification_delivered` in `repositories.py` |
| Backups stub (empty) | `app/bot/routers/admin/backups.py:13-16`; constants `callbacks.py:69-71` |
| Search stub (empty) + unreachable search | `app/bot/routers/admin/search.py:13-16`; constants `callbacks.py:74-76` |
| Settings stub + dead language button | `app/bot/routers/admin/settings.py:14-17`; `settings_kb` `keyboards.py:211-217`; `SET_LANG` constants `callbacks.py:56-58` (no handler — grep `F.data == C.SET_LANG` → 0 matches) |
| UserSettings (per-user overrides, no admin UI) | `app/core/settings.py:16-54`; injected `bot/__init__.py:66` |
| AccountRepository (owner-scoped; no global view) | `repositories.py:122-214` (`upsert_account`, `get_account`, `list_accounts`) |
| JobRepository (owner-scoped; no analytics) | `repositories.py:240-396` (`insert_job`, `get_job`, `list_jobs`, `transition_job`, `count_active_jobs_for_*`) |
| audit_log writer (no reader) | `repositories.py:402-426` (`audit`); table `migrations.py:54-62` |
| TransferEngine | `app/tg/transfer.py:192-442` (run/fetch/invite/error-classification) |
| JobManager | `app/core/job_manager.py:122-645` (lifecycle, recover `recover_interrupted_jobs`) |
| Broadcaster | `app/core/broadcast.py:218-934` (start/cancel/pause/resume/recover/sweeper) |
| NotificationService | `app/core/notifications.py:30-151` |
| ClientPool (memory-only state) | `app/tg/client_pool.py:25-101` |
| Config (all knobs) | `app/config.py:15-87`; `.env.example:1-51` |
| DB schema (all versions) | `app/db/migrations.py:9-273` (V1 users/accounts/jobs/audit … V10 notifications) |
| Dispatcher wiring | `app/bot/__init__.py:44-79` (workflow data keys) |
| Composition root | `app/main.py:36-133` (instantiates Broadcaster/NotificationService, calls `recover`/`start_sweeper`) |
| Prior reviews (cross-referenced) | `docs/notifications/ReviewReport.md` (notification defects); `docs/mandatory-subscription/report.md` (channels); `docs/broadcast/BroadcastEngine.md` + `docs/broadcast/Phases.md` (broadcast spec); `PRD.md` §19-20 (future admin panel / data it needs) |

---

## 14. Notes on scope & verification

- This report is **inspection-only**: no application code, migrations, or config were
  modified. Line numbers reflect the state of branch
  `agent/feature-34700820433` at inspection time.
- The `backups.py`, `search.py`, and admin `settings.py` stubs register routers
  in `router.py:17-20` but have **no menu entry** (`admin_menu_kb`,
  `keyboards.py:14-33`), so they are unreachable scaffolding — confirmed by grep:
  `C.SEARCH` and `C.BAK` appear in no keyboard function; `C.SET_LANG` has no
  handler.
- The IDOR cluster (`repositories.py:898,918,928`) is the admin-panel-specific
  instance of the whole-codebase finding referenced by the brief; it is a
  **confirmed** deviation from RULES §4, reproduced by the absence of any
  `owner_id` parameter or `AND owner_id=?` clause in those three UPDATE/DELETE
  statements.
- The `ReviewReport.md` (notifications) and `mandatory-subscription/report.md`
  were treated as prior art: items already documented there are referenced, not
  re-derived, for the `notifications` and `channels` routes.
