# Notification System — Comprehensive Review Report

> Scope: `app/core/notifications.py`, `app/core/events.py`,
> `app/bot/routers/admin/notifications.py`, `app/db/repositories.py`
> (notification slice), `app/bot/routers/admin/callbacks.py`,
> `app/bot/routers/admin/keyboards.py`, `app/bot/texts.py`,
> `app/db/migrations.py` (V10), event-publishing sites
> (`app/core/account_service.py`, `app/core/broadcast.py`,
> `app/core/job_manager.py`, `app/bot/middlewares.py`),
> `docs/notifications/NotificationSystem.md`, and tests
> (`tests/test_notifications.py`, `tests/test_notify_router.py`,
> `tests/test_events.py`, `tests/test_texts.py`, `tests/test_dispatcher.py`).
>
> Status: **Review only — no code changes.** Every item below was traced to a
> concrete source location. Items marked **QUESTION** are risks the reviewer
> could not confirm from static analysis alone.

---

## 1. Executive Summary

The notification system is a well-structured EventBus → NotificationService →
persistent inbox pipeline, but the implementation diverges from the design doc
in several **high-severity** ways that undermine its core promise of "a
persistent record so missed DMs aren't lost forever":

1. **Two authorization gaps (IDOR):** the single-notification read/dismiss
   handlers operate by `notification_id` alone with **no `owner_id` scoping**,
   so any admin can mark-read or dismiss *another admin's* notifications
   (`repo.mark_notification_read`, `repo.dismiss_notification`). This directly
   violates the codebase's own RULES §4 ("isolation is enforced here").

2. **Contradictory duplicate events on broadcast cancel:** `Broadcaster.cancel()`
   publishes a `broadcast_failed` event *and* the worker finalization publishes
   a `broadcast_completed` event with a "failed" title — two inbox rows for one
   cancellation, with conflicting event types.

3. **Notification content (body/data) is unreachable from the UI:** the inbox
   list renders only the generic title + event-type label; the detailed Arabic
   body (which holds user IDs, error messages, counters) is never displayed, and
   `render_notification_card` (which would show it) is defined and tested but
   **never wired** to any handler.

Beyond these, the review identified **9 medium-severity** items (dead
`delivered` flag with no resend path; `event.data` not json-guarded; per-admin
DB error aborting all admins; `job_interrupted` and `flood_wait` event types
that are declared but never published; unknown/non-system event types that are
fail-open and un-mutable; `account_unauthorized` bypassing the
`notify_on_error` kill-switch; inline per-admin queries blocking the publisher
hot path), plus **21 low-severity** and **1 question** spanning pagination math,
config kill-switch gaps, N+1 queries, a TOCTOU race, missing DB indexes, dead
code, UI/UX polish, and documentation drift.

**Counts:** 4 High · 9 Medium · 21 Low · 1 Question · 0 Critical.

---

## 2. Findings

Each finding is presented as: **Summary → Location → Severity → Suggested fix.**

### A. Security & Authorization (IDOR)

---

**A1. `mark_notification_read` has no owner_id guard (IDOR)**
- **Summary:** `cb_notify_read` calls `repo.mark_notification_read(db, notif_id)`
  using only the notification row id. There is no check that the notification
  belongs to `cb.from_user.id`. Any admin in `ADMIN_IDS` can mark *any other
  admin's* notification as read.
- **Location:** `app/bot/routers/admin/notifications.py:77` (handler) →
  `app/db/repositories.py:898` (repo function signature takes only
  `notification_id`). Contrast with `cb_notify_mark_all` (line 86) and
  `_render_page` (line 39), which *do* scope by `admin_id` — so the omission is
  an oversight, not a deliberate design.
- **Severity:** **High** (authorization bypass; violates documented isolation).
- **Suggested fix:** Add `owner_id: int` parameter to
  `mark_notification_read` and scope the UPDATE with `AND owner_id=?`. Pass
  `admin_id` from the handler.

---

**A2. `dismiss_notification` has no owner_id guard (IDOR)**
- **Summary:** Same class as A1 but for dismiss. `cb_notify_dismiss` calls
  `repo.dismiss_notification(db, notif_id)` with no owner check. Any admin can
  dismiss another admin's notification.
- **Location:** `app/bot/routers/admin/notifications.py:98` →
  `app/db/repositories.py:918` (signature takes only `notification_id`).
- **Severity:** **High**.
- **Suggested fix:** Add `owner_id` scoping to `dismiss_notification` (UPDATE
  `… WHERE id=? AND owner_id=? AND dismissed=0`) and pass `admin_id` from the
  handler.

---

**A3. `delete_notification` lacks owner_id scoping (latent IDOR)**
- **Summary:** `delete_notification` takes only `notification_id`. It is not
  currently wired to any handler (the docs mention it for GDPR cleanup in §3.3
  but no router calls it), so this is a **latent** vulnerability — any future
  wiring would inherit the IDOR unless the repo is fixed first.
- **Location:** `app/db/repositories.py:928-933`.
- **Severity:** **Low** (not currently reachable).
- **Suggested fix:** Add `owner_id` parameter and scope the DELETE. Fix at the
  repository layer now so any future handler is safe by construction.

---

**A4. QUESTION — Admin ID spoofing via forged callback data**
- **Summary:** The admin panel relies on `IsAdmin` filter
  (`app/bot/routers/admin/filters.py`) which reads `config.admin_id_list`.
  Callback data is treated as untrusted input (RULES §4), and handlers do
  re-derive `admin_id` from `cb.from_user.id` for list/mark-all. But A1/A2
  show this isn't done consistently. The reviewer could not confirm whether
  `cb.from_user` is ever `None` for an admin callback in practice; the
  `if cb.from_user else 0` fallback would scope to `owner_id=0` (a non-existent
  admin), which silently no-ops rather than erroring.
- **Location:** `app/bot/routers/admin/notifications.py:39, 85, 106, 123`
  (the `cb.from_user.id if cb.from_user else 0` pattern).
- **Severity:** **QUESTION** (low-risk; defensive code path, but the `=0`
  fallback masks a real misconfiguration rather than alerting).
- **Suggested fix direction:** Treat `cb.from_user is None` as an explicit
  error/early-return with logging rather than defaulting to `0`.

---

### B. Event Publishing Correctness & Double-Publish

---

**B1. Double/contradictory system events on broadcast cancel**
- **Summary:** When an admin cancels a *running* campaign, two different
  system events are published for the same logical action:
  1. `Broadcaster.cancel()` publishes `broadcast_failed` (event_type, severity
     `info`, title "↺|ألغي بث" = "Cancelled broadcast").
  2. The worker `_run_campaign` finalization then sees `cancel_evt.is_set()`,
     sets `final_status = "cancelled"`, and publishes
     `broadcast_completed` (event_type, severity `info`, title "×|فشل البث" =
     "Failed broadcast").

  This produces **two inbox rows** with contradictory event types (`broadcast_failed`
  + `broadcast_completed`) and a title that says "Failed" on a cancellation.
- **Location:** `app/core/broadcast.py:315-323` (cancel publishes
  `broadcast_failed`) and `app/core/broadcast.py:651-697` (worker always
  publishes `broadcast_completed` regardless of cancellation).
- **Severity:** **High** (duplicate + semantically contradictory notifications).
- **Suggested fix:** Either (a) stop the worker from publishing on cancel —
  guard the finalization block with `if not cancel_evt.is_set()` before the
  system-event publish (let `cancel()` own the cancellation notification), or
  (b) introduce a dedicated `broadcast_cancelled` event type in `SYSTEM_EVENTS`
  and publish only that from `cancel()`, suppressing the worker's publish when
  `final_status == "cancelled"`.

---

**B2. `job_interrupted` is declared but never published**
- **Summary:** `job_interrupted` is in `SYSTEM_EVENTS` (events.py:115) and
  `_job_final_event` can return it (job_manager.py:102), but the only code path
  that sets a job to `interrupted` is boot recovery — `repo.recover_interrupted_jobs`
  (repositories.py:384-396) — which performs a raw `UPDATE` and publishes
  nothing. `JobManager.recover()` (job_manager.py:162-165) only calls the repo
  function. So `job_interrupted` SystemEvents are **never emitted**; the inbox
  will never show a boot-recovery interruption, and the per-admin toggle for
  `job_interrupted` in the settings UI is dead.
- **Location:** `app/core/job_manager.py:162-165` (recover) +
  `app/db/repositories.py:384-396` (recover_interrupted_jobs — no publish).
- **Severity:** **Medium** (incomplete feature; dead config UI entry).
- **Suggested fix:** After `recover_interrupted_jobs` marks rows, publish a
  `job_interrupted` SystemEvent for each recovered job (or publish inside the
  job's `_finalize` path if the engine is the one that interrupted it).

---

**B3. `flood_wait` is declared but never published**
- **Summary:** `flood_wait` is in `SYSTEM_EVENTS` (events.py:119), has a config
  toggle (`notify_on_error`), and a label (`texts.py:717`). But **no code path
  in the entire `app/` tree publishes** `SystemEvent(event_type="flood_wait")`.
  The design doc §3.2 says the source is `JobManager (pending)`. The
  `FLOOD_WAIT` error kind exists in `app/tg/errors.py:18` and the transfer
  engine handles `FloodWait` (transfer.py:413), but it only sleeps — it never
  emits a system event. So the `flood_wait` toggle and label are dead.
- **Location:** No publisher exists (confirmed via repo-wide grep of
  `bus.publish`/`SystemEvent` call sites: account_service, broadcast,
  job_manager, middlewares — none emit `flood_wait`).
- **Severity:** **Medium**.
- **Suggested fix:** Publish a `flood_wait` SystemEvent from the transfer
  engine / job runner when a `FloodWait` is encountered (currently only
  `peer_flood` is emitted, via `_mark_account_fatal`).

---

**B4. `job_cancelled` severity mismatch: code says `info`, docs say `warning`**
- **Summary:** `_job_final_event` returns `("job_cancelled", "info", …)`
  (job_manager.py:100), but the design doc's event taxonomy table (§3.2,
  `NotificationSystem.md:120`) lists `job_cancelled` as severity `warning`.
  The actual inbox glyph and DM silent/audible behavior derive from the
  *code* value (`info` → silent DM, `›` glyph), so the doc is stale. This is a
  doc/code drift; if anyone "fixes" the doc to `warning`, behavior is unchanged
  (warning is also silent), but the **inbox glyph** changes (`!`/`›`) and the
  `render_notification_card` glyph would differ.
- **Location:** `app/core/job_manager.py:100` (code) vs
  `docs/notifications/NotificationSystem.md:120` (doc table).
- **Severity:** **Low** (doc drift; behavior currently consistent only because
  both `info` and `warning` map to silent DMs).
- **Suggested fix:** Align the doc and code. Prefer keeping `info` (cancellation
  is not an error) and update the doc table; or explicitly justify `warning` if
  cancellations should visually flag as warnings.

---

**B5. QUESTION — No deduplication of notification rows for duplicate events**
- **Summary:** `create_notification` always INSERTs a new row. If an event is
  published twice (e.g., the B1 double-publish, or a retry), two inbox rows are
  created. There is no dedup key. The reviewer could not confirm whether all
  publishers are exactly-once (some use try/except around `publish`, which
  could mask retries).
- **Severity:** **QUESTION** (low-medium; depends on publisher idempotency).
- **Suggested fix direction:** Consider a dedup key `(owner_id, event_type,
  created_at-bucked)` or dedup by `data` content for burst-prone event types
  (job_started, broadcast_started).

---

### C. Delivery Tracking & Resilience

---

**C1. `delivered` flag is never set to `1` (dead column)**
- **Summary:** The `notifications.delivered` column defaults to `0` and is
  documented as "set to 1 when the DM is sent successfully" (migrations.py:239,
  notifications.py:40). But `_try_dm` (notifications.py:109-129) sends the DM
  and **never updates `delivered`**. No `mark_notification_delivered` repo
  function exists. The test `test_notifier_marks_rows_delivered`
  (test_notifications.py:303-315) explicitly asserts `all(r["delivered"] == 0)`,
  codifying the gap. The "resend failed DMs" feature (Phase 2, docs §5)
  cannot work.
- **Location:** `app/core/notifications.py:94-105` (create + DM, no delivered
  update); `app/db/repositories.py:829-856` (no delivered-update function).
- **Severity:** **Medium**.
- **Suggested fix:** Have `_try_dm` return success/failure, and on success call a
  new `repo.mark_notification_delivered(db, notification_id)` after
  `create_notification` returns the new row id. Store the returned id from
  `create_notification`.

---

**C2. Per-admin DB error aborts the entire event's notification loop**
- **Summary:** `_on_system_event` iterates `admin_ids` and for each calls
  `is_notification_enabled` (1 query) + `create_notification` (1 query) +
  `_try_dm`. There is **no per-admin try/except**. If the DB raises for admin 1
  (e.g., a transient lock), the exception propagates to `EventBus.publish`
  (events.py:77-80), which logs it and **stops processing** — admins 2..N get
  no row and no DM for that event. A single DB blip silences all subsequent
  admins.
- **Location:** `app/core/notifications.py:89-105`.
- **Severity:** **Medium**.
- **Suggested fix:** Wrap each admin's body in its own `try/except` so one
  admin's failure doesn't skip the rest (mirroring the best-effort DM pattern
  already used for `_try_dm`).

---

**C3. `event.data` is not guarded against `json.dumps` serialization failure**
- **Summary:** `_on_system_event` passes `event.data` straight to
  `repo.create_notification` (notifications.py:101), which does
  `json.dumps(data or {}, ensure_ascii=False)` (repositories.py:853).
  `SystemEvent.data` is typed `dict[str, Any]`, so a caller could store a
  non-serializable value (e.g., a `datetime`, an enum, a custom object). A
  `TypeError` from `json.dumps` would propagate and abort the entire event loop
  for all remaining admins (see C2).
- **Location:** `app/core/notifications.py:101` +
  `app/db/repositories.py:853`.
- **Severity:** **Medium**.
- **Suggested fix:** Either validate/normalize `data` at `SystemEvent`
  construction (frozen dataclass — hard) or defensively serialize in
  `create_notification` with a fallback (`json.dumps(..., default=str)`), or
  guard the per-admin loop in C2.

---

**C4. No "resend failed DM" mechanism despite `delivered` column**
- **Summary:** The design doc §3.3 and Phase 2 roadmap describe a future "Resend"
  button and `mark_notification_delivered` repo function, but neither exists.
  Combined with C1 (`delivered` never updated), failed DMs are
  unrecoverable — the bot can't later distinguish "DM attempted" from "DM not
  attempted" and can't retry.
- **Location:** (absence) — no `mark_notification_delivered` in repositories.py;
  no resend handler in keyboards.py `notifications_list_kb`.
- **Severity:** **Medium**.
- **Suggested fix direction:** Implement `mark_notification_delivered(db,
  notification_id)` and add a "Resend" inline button for rows where
  `delivered=0`, calling `_try_dm` again.

---

### D. Config Kill-Switch & Fail-Open Gaps

---

**D1. Unknown / non-system event types are un-mutable (fail-open)**
- **Summary:** `_config_enabled` (notifications.py:133-151) returns `True` for
  any event type it doesn't explicitly recognize (the fallthrough `return True`).
  `is_notification_enabled` (repositories.py:950-963) also defaults to `True`
  for missing settings rows. The settings UI only renders toggles for types in
  `SYSTEM_EVENTS` (keyboards.py:259, texts.py:801). So a `SystemEvent` with an
  `event_type` *not* in `SYSTEM_EVENTS` will: pass the config kill-switch, pass
  the per-admin check, get a DB row, and get a DM — but the admin has **no way
  to mute it** (no config flag, no UI toggle). It is silently always-on.
- **Location:** `app/core/notifications.py:133-151` (fallthrough `return True`)
  + `app/bot/routers/admin/keyboards.py:259` / `app/bot/texts.py:801`
  (UI iterates only `SYSTEM_EVENTS`).
- **Severity:** **Medium**.
- **Suggested fix:** In `_on_system_event`, reject/guard against
  `event_type not in SYSTEM_EVENTS` — log a warning and skip (or require
  explicit registration), so unknown types default to **silent** (fail-secure)
  rather than fail-open.

---

**D2. `account_unauthorized` is not covered by `notify_on_error`**
- **Summary:** `_config_enabled` maps `flood_wait` and `peer_flood` to
  `notify_on_error` (notifications.py:142-143), but `account_unauthorized`
  falls through to the unconditional `return True` (line 151). So setting
  `NOTIFY_ON_ERROR=false` silences PeerFlood/FloodWait alerts but **not**
  unauthorized-account alerts — even though `account_unauthorized` has severity
  `error` and is arguably more alarming (a dead/blocked account mid-job). The
  design doc call it "always on (security-relevant)", but the *inconsistency*
  with other error-category events is a footgun: an operator who disables
  `notify_on_error` to reduce noise will still be paged on account_unauthorized
  but not on peer_flood.
- **Location:** `app/core/notifications.py:142-151`.
- **Severity:** **Medium**.
- **Suggested fix direction:** Either add `account_unauthorized` (and
  `account_added`/`account_removed`) to an explicit case in `_config_enabled`
  (e.g., a new `notify_on_account_events` flag, or fold them under
  `notify_on_error`), and document the choice. At minimum, add a comment
  explaining why these bypass the error kill-switch.

---

**D3. Severity → `disable_notification` mapping is not severity-table-driven**
- **Summary:** `_try_dm` uses `disable_notification=event.severity != "error"`
  (notifications.py:123). The docs §3.5.1 define info/warning/error → silent.
  The code matches the doc, but the logic is a **bare inequality** with no
  validation: an unknown severity (e.g., `"critical"`, `"fatal"`, or a typo)
  would silently be treated as non-`error` → `disable_notification=True` (silent).
  There is also no `SystemEvent` validation that `severity` is one of the three
  allowed values. If a publisher passes `severity="warn"` (typo for `"warning"`),
  it still maps to silent — but the inbox glyph would default to `›` (the
  `render_*` default), silently mislabeling it.
- **Location:** `app/core/notifications.py:123` +
  `app/core/events.py:97` (no validation on `severity: str`).
- **Severity:** **Low** (no typo currently; latent fragility).
- **Suggested fix direction:** Add a `NotificationSeverity` enum or validate
  against `{"info", "warning", "error"}` in `SystemEvent.__post_init__`
  (note: dataclass is frozen — use `__post_init__` before `object.__setattr__`
  is blocked, or a classmethod constructor).

---

### E. Performance & Query Patterns

---

**E1. NotificationService handler runs N+1 queries inline, blocking the publisher**
- **Summary:** For each event with N admins, `_on_system_event` issues 2N DB
  round-trips (`is_notification_enabled` + `create_notification` per admin),
  all awaited **inline** in the `EventBus.publish` coroutine
  (events.py:75-80). The EventBus docstring (events.py:5-6) explicitly warns "a
  slow subscriber delays the engine's on_progress callback (it is awaited
  inline)". SystemEvents are published from hot paths (e.g., `job_started`
  inside `_run_job` at job_manager.py:342, broadcast start at broadcast.py:281).
  A multi-admin deployment thus slows job/broadcast startup by the full
  per-admin DB+DM latency on the critical path.
- **Location:** `app/core/notifications.py:89-105` (per-admin loop) +
  `app/core/events.py:75-80` (inline await).
- **Severity:** **Medium** (scalability; blocking a hot path).
- **Suggested fix direction:** Offload the per-event fan-out to a background
  `asyncio.create_task` (fire-and-forget with its own error guard), or batch
  the per-admin `is_notification_enabled` + `create_notification` into a single
  transaction / batched queries. At minimum, don't block the publisher's `await
  self._bus.publish(...)`.

---

**E2. Settings screen issues 14 sequential DB queries**
- **Summary:** `cb_notify_settings` (notifications.py:108-110) and
  `cb_notify_toggle` (notifications.py:130-133) each loop over all 14
  `SYSTEM_EVENTS` calling `is_notification_enabled` one at a time — 14
  sequential round-trips per screen render. `cb_notify_toggle` re-queries all
  14 again after a single toggle.
- **Location:** `app/bot/routers/admin/notifications.py:108-110, 130-133` +
  `app/db/repositories.py:950-963` (one-row `fetch_one` per call).
- **Severity:** **Low** (14 cheap SQLite queries; noticeable only on slow DB).
- **Suggested fix direction:** Add a batch function
  `get_notification_settings(db, owner_id) -> dict[str, bool]` returning all 14
  toggles in one query.

---

**E3. No composite index for inbox ordering**
- **Summary:** `list_notifications` orders by `created_at DESC, id DESC`
  (repositories.py:880-884), but the only index on `notifications` is
  `idx_notifications_owner (owner_id)` (migrations.py V10). Without a
  `(owner_id, created_at DESC, id DESC)` index, SQLite must collect all of the
  owner's rows and sort them for every page request. Fine for small inboxes;
  degrades with thousands of notifications per admin.
- **Location:** `app/db/repositories.py:880-884` (ORDER BY) vs
  `app/db/migrations.py:270-272` (available indexes).
- **Severity:** **Low** (performance, scales with inbox size).
- **Suggested fix direction:** Add `CREATE INDEX idx_notifications_owner_created
  ON notifications(owner_id, created_at DESC, id DESC)` in the V10 migration
  (append-only — a V11 migration adding the index).

---

### F. Pagination & Inbox Rendering

---

**F1. Page-size constant is duplicated (maintainability hazard)**
- **Summary:** `NOTIFS_PER_PAGE = 10` is defined in the router (notifications.py:21),
  but the "Next" button condition in the keyboard hardcodes `10`
  (keyboards.py:240: `if len(notifications) >= 10:`). If someone raises
  `NOTIFS_PER_PAGE`, the keyboard's "has next page" heuristic stays at 10,
  breaking pagination.
- **Location:** `app/bot/routers/admin/notifications.py:21` vs
  `app/bot/routers/admin/keyboards.py:240`.
- **Severity:** **Low** (latent; currently consistent at 10).
- **Suggested fix:** Import and reference the constant, or compute `has_next =
  len(notifications) >= NOTIFS_PER_PAGE` in the router and pass it to the
  keyboard builder.

---

**F2. TOCTOU race between unread count and list in `_render_page`**
- **Summary:** `_render_page` calls `count_unread_notifications` (notifications.py:40)
  and then `list_notifications` (notifications.py:41-44) as **two separate
  queries**. A system event arriving between the two is reflected in the count
  but not the list (or vice-versa), producing an inconsistent inbox header
  (`غير مقروء: 4` while only 3 unread rows are visible).
- **Location:** `app/bot/routers/admin/notifications.py:38-49`.
- **Severity:** **Low** (narrow race window; cosmetic).
- **Suggested fix direction:** Fetch both in one transaction, or accept the
  eventual inconsistency and note it; a single combined query is not trivial due
  to the LIMIT/OFFSET on the list.

---

**F3. Negative page number is not validated**
- **Summary:** `cb_notify_page` parses the page via `_int_after` which accepts
  any integer, including negatives (`adm:notify:p:-1` → `page=-1`). The offset
  becomes `page * NOTIFS_PER_PAGE` = `-10`. SQLite clamps negative OFFSET to 0
  in modern versions, but this is an undocumented dependency; on some engines a
  negative OFFSET raises. There is also no upper bound, so `page=999999`
  produces an empty list page-by-page.
- **Location:** `app/bot/routers/admin/notifications.py:59-66`.
- **Severity:** **Low** (admin-only input; SQLite tolerates it).
- **Suggested fix:** Clamp `page = max(0, page or 0)`.

---

**F4. Inbox shows dismissed notifications with no clear visual distinction**
- **Summary:** `_render_page` fetches with `include_dismissed=True`
  (notifications.py:43), so dismissed rows appear in the list. They're auto-marked
  read by `dismiss_notification` (so the unread dot is a space `" "` not `•`),
  but there is **no "dismissed" indicator** — a dismissed row looks identical to
  a normally-read row. The design doc §3.6.2 inbox layout implies dismissed rows
  should be visually distinct.
- **Location:** `app/bot/routers/admin/notifications.py:43` +
  `app/bot/texts.py:775-778` (rendered with `unread_dot` only; no dismissed
  glyph).
- **Severity:** **Low** (UX clarity).
- **Suggested fix direction:** Add a `×` or strikethrough for dismissed rows,
  or exclude dismissed rows from the default inbox (show them only behind a
  "recently dismissed" filter).

---

**F5. Read/Dismiss single-notification handlers silently no-op and bounce to page 0**
- **Summary:** `cb_notify_read` (notifications.py:77) and `cb_notify_dismiss`
  (notifications.py:98) ignore the boolean return of the repo functions. If the
  notification was already read/dismissed or doesn't exist, the call is a silent
  no-op and the page re-renders with no feedback. Additionally, both handlers
  always re-render **page 0**, so acting on a notification on page 2 bounces the
  user to the first page.
- **Location:** `app/bot/routers/admin/notifications.py:77, 78, 98, 99`.
- **Severity:** **Low** (UX).
- **Suggested fix:** Surface a brief callback alert ("✓ تم التحديد" / "× تم
  الإخفاء") when the action actually took effect, and preserve the current page
  (re-render the same `page`) after the action.

---

### G. Admin Panel UI / Missing Features

---

**G1. Notification body and `data` are unreachable from the UI**
- **Summary:** The inbox list (`render_notifications_list`, texts.py:760-786)
  renders only `title` + `event_type` label per row. The `body` — which for
  `user_joined` contains the user's name/username/id, and for job/broadcast
  events contains invite/fail/skip counters and error details — is **never
  shown**. There is no "view detail" / open callback in
  `notifications_list_kb` (keyboards.py:220-249 — only ✓ read and × dismiss
  per row). `render_notification_card` (texts.py:738), which *would* render the
  body and timestamp, is never invoked by the router. So the persistent inbox
  delivers only a one-line title per event — the detail that distinguishes "a
  new user joined" from *which* user, or "a job failed" from *why*, is
  invisible. This undermines the design doc's stated goal (§1.3: "no record of
  what they were told").
- **Location:** `app/bot/texts.py:760-786` (list omits body) +
  `app/bot/routers/admin/keyboards.py:220-249` (no open/view button) +
  `app/bot/texts.py:738` (`render_notification_card` defined but unused).
- **Severity:** **High** (feature is non-functional for its stated purpose).
- **Suggested fix:** Add a `NOTIFY_OPEN` callback + handler that renders
  `render_notification_card(notif)` in-place; wire `render_notification_card`
  into the router. Show the body in the card.

---

**G2. `render_notification_card` is dead code (defined + tested, never called)**
- **Summary:** `render_notification_card` (texts.py:738) is exported in
  `__all__` (texts.py:53) and has a dedicated test
  (`tests/test_texts.py:342-352`), but **no router/handler ever calls it**. The
  test gives false confidence that the card view works.
- **Location:** `app/bot/texts.py:738` + `tests/test_texts.py:342`; grep for
  `render_notification_card` in `app/` non-test code → 0 call sites.
- **Severity:** **Low** (dead code, but masks the G1 gap).
- **Suggested fix:** Either wire it to a `NOTIFY_OPEN` handler (see G1) or
  remove it and its test.

---

**G3. Settings screen renders raw snake_case event types alongside Arabic labels**
- **Summary:** `render_notify_settings` (texts.py:804) emits
  `f"{on} {et}  {esc(label)}"` where `et` is the raw event_type (e.g.,
  `user_joined`) and `label` is the Arabic translation (e.g., `مستخدم جديد`).
  Every row thus shows both the English slug and the Arabic label — redundant
  and visually noisy for Arabic-speaking admins.
- **Location:** `app/bot/texts.py:804`.
- **Severity:** **Low** (UX).
- **Suggested fix:** Drop `{et}` and show only the Arabic label (the callback
  data already carries the event type for the toggle).

---

**G4. Settings keyboard: one button per row + awkward "All Read" + no back-to-inbox**
- **Summary:** `notify_settings_kb` (keyboards.py:259-261) puts each of the 14
  event types on its **own row** (14 vertical taps), has a "✓ All Read" button
  that navigates to the inbox (marking-all-read is out of place on a *settings*
  screen), and provides only "› رجوع" (back to *menu*, not back to inbox).
  Contrast `entries_kb` (keyboards.py:93-108) which lays out toggles compactly.
- **Location:** `app/bot/routers/admin/keyboards.py:252-264`.
- **Severity:** **Low** (UX polish).
- **Suggested fix:** Group toggles into 2-column rows; remove or relocate
  "All Read" to the inbox; add a "back to inbox" button.

---

**G5. `NOTIFY_SETTINGS` callback constant is missing from callbacks.py**
- **Summary:** Every notification callback has a `*_NOTIFY_*` constant in
  callbacks.py (NOTIFY, NOTIFY_PAGE, NOTIFY_READ, NOTIFY_DISMISS, NOTIFY_MARK_ALL,
  NOTIFY_TOGGLE) **except** settings, which is constructed inline as
  `f"{C.NOTIFY}:settings"` in both the router (notifications.py:102) and the
  keyboard (keyboards.py:246). This is fragile — a format change must be applied
  in two places with no compile-time check.
- **Location:** `app/bot/routers/admin/callbacks.py:60-66` (no
  `NOTIFY_SETTINGS`) + `app/bot/routers/admin/notifications.py:102` +
  `app/bot/routers/admin/keyboards.py:246`.
- **Severity:** **Low** (maintainability).
- **Suggested fix:** Add `NOTIFY_SETTINGS = f"{PREFIX}:notify:settings"` and use
  it in both sites.

---

**G6. `delete_notification` (GDPR cleanup) is unimplemented at the UI level**
- **Summary:** The docs §3.3 mention a `delete_notification` function for
  permanent/GDPR removal. The function exists in repositories.py:928, but **no
  handler or keyboard button exposes it**, and (per A3) it lacks owner scoping.
  This is a missing-feature gap, not strictly a bug.
- **Location:** `app/db/repositories.py:928-933` (exists, unscoped, unwired).
- **Severity:** **Low** (documented future work).
- **Suggested fix direction:** If/when implementing GDPR cleanup, add owner
  scoping (see A3) and an admin handler.

---

### H. Documentation & Code Consistency

---

**H1. `set_bot` docstring and method name contradict the design doc**
- **Summary:** The class docstring (notifications.py:10-11) says the bot is
  injected "per-call (`start(bot)` / `notify(...)`)", but the actual method is
  `set_bot` (notifications.py:67-69) — there is no `start` or `notify` method.
  The design doc §4.3 (NotificationSystem.md:415) writes `await
  notifications.set_bot(bot)`, but `set_bot` is **not async**
  (`def set_bot(self, bot: Any) -> None`). `await`-ing a non-awaitable raises
  a confusing `TypeError`. (main.py:87 correctly calls it without `await`.)
- **Location:** `app/core/notifications.py:10-11` (docstring) +
  `notifications.py:67` (`def set_bot`) vs
  `docs/notifications/NotificationSystem.md:415` (`await notifications.set_bot(bot)`).
- **Severity:** **Low** (docstring/doc drift; production code is correct).
- **Suggested fix:** Update the docstring to say `set_bot(bot)`; fix the doc to
  drop `await`.

---

**H2. "Always on" comment for account events is misleading**
- **Summary:** `_config_enabled`'s fallthrough comment
  (notifications.py:149) reads: "account_added, account_removed,
  account_unauthorized — always on". But these **are** muteable per-admin via the
  settings UI (the toggle loop in `cb_notify_settings` iterates `SYSTEM_EVENTS`,
  which includes these three, and `is_notification_enabled` gates them). So
  "always on" is true only at the *global config* level, not the per-admin level.
  A reader could wrongly conclude these can't be disabled.
- **Location:** `app/core/notifications.py:149-151`.
- **Severity:** **Low** (misleading comment).
- **Suggested fix:** Reword to "no global config kill-switch; per-admin toggles
  still apply".

---

**H3. Design doc inbox layout doesn't match the rendered list**
- **Summary:** NotificationSystem.md §3.6.2 (lines 316-335) depicts an inbox
  row with the body preview ("طلب 42 بدأ البوت") and a "منذ 5 دقائق" timestamp.
  The actual `render_notifications_list` (texts.py:774-782) renders only the
  title, event-type label, and a read/unread dot — no body preview, no
  relative timestamp. The doc over-specifies the UI.
- **Location:** `docs/notifications/NotificationSystem.md:316-335` (doc) vs
  `app/bot/texts.py:774-782` (code).
- **Severity:** **Low** (doc drift).
- **Suggested fix:** Update the doc's inbox layout to match the code, or
  implement the doc'd layout (body preview + timestamp).

---

**H4. Doc §3.3.1 says "No foreign key to `users`" — correct, but note the index gap**
- **Summary:** The doc explicitly justifies the no-FK design (good). But the
  doc's schema (NotificationSystem.md:150-168) shows only `idx_notifications_owner`
  and the partial unread index — it does **not** mention the ordering index gap
  (E3), nor that `list_notifications` is called with `include_dismissed=True`
  from the inbox (the doc §3.6.2 doesn't mention dismissed rows in the inbox
  at all). Minor doc drift.
- **Location:** `docs/notifications/NotificationSystem.md:147-168, 316-335`.
- **Severity:** **Low**.
- **Suggested fix:** Note the dismissed-in-inbox behavior and the missing
  ordering index in the doc.

---

### I. Testing Gaps

---

**I1. No cross-admin authorization tests for read/dismiss (IDOR untested)**
- **Summary:** `tests/test_notify_router.py` seeds notifications for `owner_id=1001`
  and always uses `make_cb(..., user_id=1001)`. No test seeds a notification for
  admin 1002 and then acts on it as admin 1001. The IDOR (A1/A2) is therefore
  **not covered by any test** — tests pass despite the vulnerability.
- **Location:** `tests/test_notify_router.py` (all handler tests use user_id
  1001 for both seed and callback).
- **Severity:** **Low** (test coverage gap).
- **Suggested fix:** Add a test that seeds a notification for admin B, then
  asserts that admin A's `cb_notify_read`/`cb_notify_dismiss` call does not
  modify B's row (this test would currently FAIL, exposing A1/A2).

---

**I2. The "marks rows delivered" test codifies an incorrect invariant**
- **Summary:** `test_notifier_marks_rows_delivered` (test_notifications.py:303-315)
  asserts `all(r["delivered"] == 0 for r in rows)` and a comment says "`delivered`
  is not auto-set by the service". The test therefore **blesses** the C1 bug — if
  the `delivered` flag is ever implemented (Phase 2), this test must be updated.
- **Location:** `tests/test_notifications.py:312-314`.
- **Severity:** **Low** (test encodes a known-future fix as a current invariant).
- **Suggested fix:** Add a TODO marker, or rephrase the test as "delivered
  remains 0 until a resend/delivered-update path is implemented".

---

## 3. Severity Summary

| ID  | Summary                                          | Severity |
|-----|--------------------------------------------------|----------|
| A1  | `mark_notification_read` IDOR (no owner scope)     | High     |
| A2  | `dismiss_notification` IDOR (no owner scope)       | High     |
| B1  | Double/contradictory events on broadcast cancel    | High     |
| C1  | `delivered` flag never set (dead column)           | Medium   |
| C2  | Per-admin DB error aborts all admins              | Medium   |
| C3  | `event.data` not json-guarded                     | Medium   |
| C4  | No resend mechanism for failed DMs                 | Medium   |
| D1  | Unknown event types un-mutable (fail-open)         | Medium   |
| D2  | `account_unauthorized` bypasses `notify_on_error`  | Medium   |
| E1  | Inline N×2 queries block publisher (hot path)      | Medium   |
| G1  | Notification body/data unreachable from UI        | High     |
| A3  | `delete_notification` lacks owner scope (latent)  | Low      |
| B2  | `job_interrupted` never published                  | Medium   |
| B3  | `flood_wait` never published                       | Medium   |
| B4  | `job_cancelled` severity: code `info`/doc `warning` | Low     |
| B5  | No dedup for duplicate events (QUESTION)           | Question |
| D3  | Severity mapping not table-driven                  | Low      |
| E2  | 14 sequential settings queries                     | Low      |
| E3  | Missing `(owner_id, created_at)` index             | Low      |
| F1  | Duplicated `NOTIFS_PER_PAGE` constant             | Low      |
| F2  | TOCTOU race in unread count + list                | Low      |
| F3  | Negative page not validated                       | Low      |
| F4  | Dismissed rows not visually distinct              | Low      |
| F5  | Read/dismiss silent no-op + page bounce           | Low      |
| G2  | `render_notification_card` dead code               | Low      |
| G3  | Settings shows raw event_type + Arabic label       | Low      |
| G4  | Settings keyboard layout / nav UX                 | Low      |
| G5  | Missing `NOTIFY_SETTINGS` callback constant        | Low      |
| G6  | `delete_notification` unimplemented in UI          | Low      |
| H1  | `set_bot` docstring/doc `await` mismatch           | Low      |
| H2  | "Always on" comment misleading                     | Low      |
| H3  | Doc inbox layout ≠ code                            | Low      |
| H4  | Doc doesn't mention dismissed-in-inbox / index gap | Low      |
| I1  | No cross-admin authz tests for read/dismiss        | Low      |
| I2  | "marks rows delivered" test blesses the bug        | Low      |

**Totals:** 4 High · 9 Medium · 21 Low · 1 Question · 0 Critical (35 findings).

---

## 4. File:Line Reference Index

| Area | Key files & representative lines |
|---|---|
| NotificationService | `app/core/notifications.py:30-151` (handler 73-105; `_try_dm` 109-129; `_config_enabled` 133-151; `set_bot` 67-69) |
| EventBus / SystemEvent | `app/core/events.py:53-122` (publish 75-80; SystemEvent 83-100; SYSTEM_EVENTS 105-121) |
| Admin inbox router | `app/bot/routers/admin/notifications.py:19-139` (`_render_page` 38-49; handlers 52-139) |
| Notifications repo | `app/db/repositories.py:829-963` (create 829; list 859; count 888; read 898; mark-all 908; dismiss 918; delete 928; settings 936-963) |
| DB schema (V10) | `app/db/migrations.py:234-273` |
| Callbacks (constants) | `app/bot/routers/admin/callbacks.py:60-66` |
| Keyboards | `app/bot/routers/admin/keyboards.py:220-264` |
| Text renderers | `app/bot/texts.py:693-813` (`render_notification_card` 738; `render_notifications_list` 760; `render_notify_settings` 789; event labels 703-724) |
| Event publishers | middlewares.py:67-76 (`user_joined`); account_service.py:70-79, 116-124; broadcast.py:281-290, 315-323, 677-697; job_manager.py:256-282, 342-351, 472-499, 557-571 |
| Config | `app/config.py:53-57` (notify_on_* fields) |
| Wiring | `app/main.py:86-88` (`set_bot` called sync); `app/bot/__init__.py:68` (`dp["notifications"]`) |
| Filters | `app/bot/routers/admin/filters.py:1-15` (IsAdmin) |
| Tests | `tests/test_notifications.py:1-411`; `tests/test_notify_router.py:1-273`; `tests/test_events.py:1-138`; `tests/test_texts.py:342-364` |
| Design doc | `docs/notifications/NotificationSystem.md` (§3.2 taxonomy 109-128; §3.3 schema 143-198; §3.5 severity 280-296; §3.6 UI 298-341; §4.3 wiring 407-423; §5 roadmap 434-446) |

---

## 5. Top Recommendations (by impact)

1. **Immediate (High):** Scope `mark_notification_read` and `dismiss_notification`
   by `owner_id` at the repository layer and thread the admin id through the
   handlers (A1, A2). Add cross-admin authorization tests (I1).
2. **Immediate (High):** Fix the broadcast-cancel double-publish — either
   suppress the worker's `broadcast_completed` publish when
   `cancel_evt.is_set()`, or introduce a `broadcast_cancelled` event type (B1).
3. **Short-term (High):** Wire `render_notification_card` to a view/open action
   so the body and `data` are reachable; the inbox is otherwise a title-only
   log (G1, G2).
4. **Short-term (Medium):** Update the `delivered` flag after a successful DM
   and implement a resend path (C1, C4); harden the per-admin loop with
   try/except so one admin's DB error can't silence the rest (C2); guard
   `event.data` serialization (C3).
5. **Short-term (Medium):** Reject unknown `event_type`s that aren't in
   `SYSTEM_EVENTS` rather than failing open (D1); align `account_unauthorized`
   with the `notify_on_error` category or document the intentional exception
   (D2).
6. **Soon (Medium):** Publish the declared-but-missing `job_interrupted` (on
   boot recovery) and `flood_wait` (on FloodWait in the transfer engine) events
   (B2, B3).
7. **Soon (Medium):** Decouple the per-admin fan-out from the publisher's `await
   self._bus.publish(...)` so DM/row writes don't block job/broadcast startup
   (E1).
8. **Polish (Low):** Fix the duplicated page-size constant (F1), the missing
   `NOTIFY_SETTINGS` constant (G5), the negative-page guard (F3), and the doc
   drift (H1, H3, B4) before the next release to avoid compounding confusion.
