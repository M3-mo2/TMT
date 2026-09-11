# System Notification Engine — Design & Implementation

> Status: **Implemented** (branch `agent/feature-34656420282`) | Docs: `docs/notifications/NotificationSystem.md`

## 1. Problem Statement

The TMT bot operators previously had **no real-time visibility** into what was happening
inside the system between their direct interactions.  Specifically:

1. **Owner notification on bot join** — When a *new* Telegram user starts the bot
   (first `/start`), the owner receives no signal.  There is no audit trail of
   adoption growth, no way to spot-test new users, and no security alert when
   an unexpected account begins using the bot.

2. **Operational event visibility** — Transfer jobs, account additions, and
   broadcast campaigns proceed silently.  A PeerFlood or an account going
   unauthorized mid-job is only noticed when the operator re-checks the
   dashboard hours later.

3. **Persistent inbox** — Even when operators *do* receive a DM, there is no
   record of what they were told.  Missed DMs are lost forever.

This document proposes and describes a complete system-notification engine that
addresses all three issues through a unified **EventBus → NotificationService**
pipeline with a persistent admin inbox UI.

---

## 2. Feature 1: Owner Notification on Bot Join

### 2.1 Current State

When any user sends `/start`, the `UserGateMiddleware` creates or updates the
user row in `users` but fires **no event**.  The owner is oblivious.

### 2.2 Proposed Behaviour

On the *first* contact (insert, not update), the middleware publishes a
`SystemEvent` of type `user_joined` to the shared `EventBus`.  The
`NotificationService` (see §4) receives it, writes a persistent notification
row for every admin, and DMs each admin with a human-readable Arabic summary.

#### 2.2.1 Data Carried

```
SystemEvent(
    event_type="user_joined",
    severity="info",
    title="مستخدم جديد",           # pre-rendered Arabic label
    body="<full Arabic body with first_name, username, user_id>",
    data={"user_id": 12345},
)
```

#### 2.2.2 Text Rendering

The body is pre-rendered by `UserGateMiddleware._render_join_body()` in
`app/bot/middlewares.py:115`.  This keeps the `core/` layer free of
`texts.py` imports (RULES §1) — the core `NotificationService` only forwards
pre-rendered text.

#### 2.2.3 Owner Detection

Admins are identified via `Config.admin_id_list` (comma-separated in
`ADMIN_IDS` env var).  No database lookup is required, because admins may not
yet exist in the `users` table when the very first user joins.

#### 2.2.4 Design Decision: New-User Detection

| Approach | Pros | Cons | Decision |
|---|---|---|---|
| `INSERT … ON CONFLICT` with row-count | No extra query | Ambiguous under retries | ❌ |
| `SELECT` then `INSERT`/`UPDATE` | Deterministic `is_new` flag | One extra round-trip (acceptable — first-join events are rare) | ✅ |
| `RETURNING` clause (SQLite 3.35+) | Single round-trip | Not available on older SQLite | ⚠️ (future) |

The current implementation uses a **`SELECT` existence check** followed by
`INSERT … ON CONFLICT DO UPDATE` (upsert).  `upsert_user` now returns
`bool` (`True` = new user), enabling the middleware to publish only on the
insert path.

### 2.3 Security Considerations

- **Privacy**: The notification includes the user's `first_name`, `username`
  (if public), and Telegram `user_id`.  It does **not** expose phone numbers,
  session data, or message history.
- **Rate**: Every new user triggers exactly one event — no amplification.
  Bots with viral adoption will see linear growth in owner notifications,
  which is the intended behaviour.
- **Opt-out**: Admins may disable `user_joined` notifications via the admin
  UI (§5.4) or the `NOTIFY_ON_USER_JOIN=false` config flag.

### 2.4 Future Enhancements

- **Webhook relay**: Push `user_joined` events to an external observability
  platform (Datadog, Slack) via a configurable webhook URL.
- **Aggregation**: Batch new-user alerts during high-growth periods
  (e.g. "5 new users joined in the last hour").
- **PII redaction**: Allow operators to suppress usernames in notifications.

---

## 3. Feature 2: Better Notification System

### 3.1 Event Taxonomy

The `SYSTEM_EVENTS` frozenset defines all event types that may surface as
system notifications:

| Event Type | Severity | Category | Source |
|---|---|---|---|
| `user_joined` | info | user | `UserGateMiddleware` |
| `account_added` | info | account | `AccountService.save_login` |
| `account_removed` | info | account | `AccountService.remove` |
| `account_unauthorized` | error | account | `JobManager._mark_account_fatal` |
| `peer_flood` | error | error | `JobManager._mark_account_fatal` |
| `flood_wait` | warning | error | `JobManager` (pending) |
| `job_started` | info | job | `JobManager._run_job` |
| `job_completed` | info | job | `JobManager._finalize` |
| `job_failed` | error | job | `JobManager._finalize` |
| `job_cancelled` | warning | job | `JobManager.cancel_job` |
| `job_interrupted` | warning | job | `JobManager` recovery path |
| `broadcast_started` | info | broadcast | `Broadcaster.start` |
| `broadcast_completed` | info | broadcast | `Broadcaster._run_campaign` |
| `broadcast_failed` | error | broadcast | `Broadcaster._run_campaign` / `cancel` |

> **Note**: `account_added` and `account_removed` events are always-on (no
> config toggle).  They are security-relevant and low-volume, so fail-open is
> the safe default.

### 3.2 Config Surface

New config keys in `.env` (documented in `.env.example`):

| Key | Default | Controls |
|---|---|---|
| `notify_on_user_join` | `true` | Show DM on new user join |
| `notify_on_job_events` | `true` | Show DMs for job lifecycle events |
| `notify_on_error` | `true` | Show DMs for error/flood events |
| `notify_on_broadcast_events` | `true` | Show DMs for broadcast lifecycle |

Per-admin overrides live in the `notification_settings` table (see §3.3).

### 3.3 Data Model

#### 3.3.1 `notifications` Table

One row **per admin per event** — this allows each admin to independently
mark-read / dismiss without affecting others.

```sql
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,              -- admin's Telegram user_id
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,                    -- pre-rendered Arabic
    body TEXT NOT NULL,                     -- pre-rendered Arabic
    data TEXT NOT NULL DEFAULT '{}',        -- JSON metadata
    created_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,   -- 1 if DM send succeeded
    read_at TEXT,                           -- NULL = unread
    dismissed INTEGER NOT NULL DEFAULT 0    -- 1 if dismissed from inbox
);

CREATE INDEX idx_notifications_owner ON notifications(owner_id);
CREATE INDEX idx_notifications_unread ON notifications(owner_id, read_at, dismissed)
    WHERE read_at IS NULL AND dismissed = 0;
```

**Design decisions**:

- **No foreign key to `users`**: admins may be configured but not yet in the
  `users` table (e.g. the very first `user_joined` event).  Using a plain
  `INTEGER` column avoids insert failures.
- **`delivered` flag**: Set to `1` when the DM is sent successfully.  Currently
  the service creates rows before attempting the DM (best-effort), so `delivered`
  remains `0` if the send fails.  This enables a future "resend" button.
- **Soft-dismiss vs delete**: Dismissing marks `dismissed=1` (keeps the row for
  audit).  A separate `delete_notification` function exists for permanent
  removal (GDPR cleanup).
- **`created_at` in app code**: Uses `now_iso()` from `repositories.py`, not
  SQLite `datetime('now')`, for cross-engine portability (RULES §6).

#### 3.3.2 `notification_settings` Table

Per-admin toggles for each event type.  Missing rows default to **enabled**
(fail-open at insert time — the `NotificationService` logs a row without
inserting a settings row, so the default is the safe "show me everything"
behaviour).

```sql
CREATE TABLE notification_settings (
    owner_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(owner_id, event_type)
);
```

### 3.4 Architecture

```
                    ┌──────────────────┐
                    │   EventBus (in-   │  shared singleton, created in main.py
                    │   process pubsub) │
                    └────┬──────┬───────┘
                         │      │
              ┌──────────┘      └──────────┐
              │                              │
      ┌───────▼────────┐         ┌─────────▼──────────┐
      │ UserGate-      │         │ JobManager         │
      │ middleware     │         │ (job_started, etc.)│
      └────────────────┘         └────────┬───────────┘
                                          │
      ┌────────────────┐    ┌────────────┴───────────┐
      │ AccountService │    │ Broadcaster            │
      │ (account_*)    │    │ (broadcast_*)          │
      └────────────────┘    └────────────┬───────────┘
                                          │
              ┌──────────┬───────────────┼───────────────┐
              │          │               │               │
      ┌───────▼──┐   ┌───▼────┐   ┌──────▼─────┐   ┌─────▼──────┐
      │ Reporter │   │ Notifi- │   │ Bot DMed   │   │ Admin UI   │
      │ (live    │   │ cation  │   │ (admin     │   │ (inline    │
      │ card)    │   │ Service │   │ inbox)     │   │ keyboards) │
      └──────────┘   └───┬─────┘   └────────────┘   └────────────┘
                         │
                         ▼
                   ┌──────────┐    ┌────────────────┐
                   │ DB:      │    │ Bot: send_     │
                   │ notifica-│    │ message DM     │
                   │ tions    │    │ each admin     │
                   └──────────┘    └────────────────┘
```

#### 3.4.1 Processing Pipeline

```
EventBus.publish(SystemEvent)
    │
    ├─ NotificationService._on_system_event(event)    [subscribed on startup]
    │     1. Check config kill-switch (notify_on_*)
    │     2. For each admin_id in config.admin_id_list:
    │         a. Check per-admin setting (is_notification_enabled)
    │         b. Create notification row  (create_notification)
    │         c. Best-effort DM           (_try_dm → bot.send_message)
    │
    └─ JobProgressReporter._on_progress / _on_finished   [existing, unchanged]
```

#### 3.4.2 Layer Boundaries (RULES §1)

```
app/bot/middlewares.py  →  core/events.py  →  core/notifications.py  →  db/repositories.py
(UserGate)                 (SystemEvent)      (NotificationService)      (CRUD)
     ↓                        ↓                    ↓                       ↓
pre-render Arabic            bus pubsub         subscribe + handle          SQL
     ↑ (texts.py)           ↑ (bus)              ↑ (bus)              ↑ (Database)
```

The `NotificationService` in `core/` imports `db/` and `core/events.py` but
**never** imports `bot/` or `texts.py`.  Arabic pre-rendering happens at the
call-site (middleware, services) which already has access to `texts.py`.

### 3.5 NotificationService Detail

**File**: `app/core/notifications.py`

Key methods:

| Method | Responsibility |
|---|---|
| `subscribe()` | Register `_on_system_event` on the `EventBus` |
| `unsubscribe()` | Clean shutdown (idempotent) |
| `set_bot(bot)` | Inject the `aiogram.Bot` instance (created after dispatcher) |
| `_on_system_event(event)` | Main handler — config gate → per-admin loop → row + DM |
| `_try_dm(admin_id, event)` | Best-effort DM with `disable_notification` based on severity |
| `_config_enabled(event_type)` | Map event types to config flags (user_joined → notify_on_user_join, etc.) |

#### 3.5.1 Severity → Silent/Fail

- **`info`** → DM sent with `disable_notification=True` (silent)
- **`warning`** → DM sent with `disable_notification=True` (silent)
- **`error`** → DM sent with `disable_notification=False` (audible alert — pushes notification sound)

This ensures critical errors (PeerFlood, job failures, unauthorized accounts)
break through Do-Not-Disturb while informational events don't spam.

#### 3.5.2 Error Resilience

- A single `SystemEvent` with a large `admin_ids` list iterates per-admin; a
  DM failure for one admin does **not** prevent delivery to others.
- `publish()` on the `EventBus` catches and logs handler exceptions — a
  failing notification never crashes the originating operation (job,
  broadcast, middleware).
- Missing `notification_settings` rows default to enabled (fail-open).

### 3.6 Admin Inbox UI

**File**: `app/bot/routers/admin/notifications.py`

#### 3.6.1 Screens

| Screen | Callback Data | Handler |
|---|---|---|
| Inbox (page 1) | `adm:notify` | `cb_notify_list` |
| Paginated inbox | `adm:notify:p:<n>` | `cb_notify_page` |
| Mark-as-read | `adm:notify:read:<id>` | `cb_notify_read` |
| Mark-all-read | `adm:notify:markall` | `cb_notify_mark_all` |
| Dismiss | `adm:notify:dismiss:<id>` | `cb_notify_dismiss` |
| Settings | `adm:notify:settings` | `cb_notify_settings` |
| Toggle setting | `adm:notify:toggle:<type>` | `cb_notify_toggle` |

#### 3.6.2 Inbox Layout

```
⟡ مركز الإشعارات
📬 غير مقروءة: <code>3</code>
―――――――――――――――――――――
1. [✓] مستخدم جديد
   طلب 42 بدأ البوت
   منذ 5 دقائق

2. [✓] اكتملت عملية نقل
   150 مدعو، 0 متخطي، 0 فشل
   منذ 10 دقائق
   [📖] [🗑]
   › الصفحة التالية ››
⟡|المشرفون المشتركون بهم ↼ <code>2</code>
›|<code>1001</code>
›|<code>67890</code>
استخدم الأزرار للتنقل ↓
```

Buttons: ✓ (read), 🗑 (dismiss), pagination «/».

#### 3.6.3 Settings Layout

All `SYSTEM_EVENTS` appear as toggle buttons (✓ active / × muted), with a
count of shared admins and their IDs listed at the bottom.

### 3.7 Testing Strategy

| Test File | Coverage |
|---|---|
| `tests/test_notifications.py` | `NotificationService` (DM, config killswitch, per-admin settings, error resilience, non-system-event filtering) + repository CRUD (create, list, count, read-state mutations, settings upsert/select) |
| `tests/test_notify_router.py` | Admin inbox handlers (list, pagination, read, mark-all, dismiss, malformed IDs, settings screen, toggle) using real SQLite via `db` fixture |
| `tests/test_texts.py` | `render_notifications_list`, `render_notify_settings`, `render_notification_card`, label functions |
| `tests/test_events.py` | `SystemEvent` dataclass, `SYSTEM_EVENTS` set membership |
| `tests/test_database.py` | V10 migration applied (tables + indexes) |
| `tests/test_dispatcher.py` | `NotificationService` injected into dispatcher workflow data |

**Test philosophy**: Handler tests use the **real `db` fixture** (SQLite in
memory) rather than mocking `repo`, because mocking async functions with
`MagicMock` introduces type errors (`TypeError: object MagicMock can't be
used in 'await' expression`).  Only `safe_edit` is mocked (via
`monkeypatch`), since it depends on a live `aiogram.Message` object.

### 3.8 Migration

**V10** (appended, never edited — RULES §6):

```sql
CREATE TABLE notifications (...);
CREATE TABLE notification_settings (...);
CREATE INDEX idx_notifications_owner ...;
CREATE INDEX idx_notifications_unread ...;
```

Applied atomically within a single `BEGIN IMMEDIATE`/`COMMIT` block by
`apply_migrations()`.

---

## 4. Implementation Summary

### 4.1 Files Created

| File | Purpose |
|---|---|
| `app/core/notifications.py` | `NotificationService` — bus subscriber, DM sender, config gate |
| `app/bot/routers/admin/notifications.py` | Admin inbox router (list/read/dismiss/settings/toggle) |
| `docs/notifications/NotificationSystem.md` | This document |
| `tests/test_notifications.py` | Service + repository tests |
| `tests/test_notify_router.py` | Handler tests |

### 4.2 Files Modified

| File | Change |
|---|---|
| `app/core/events.py` | Added `SystemEvent` dataclass + `SYSTEM_EVENTS` frozenset |
| `app/db/migrations.py` | Added V10 migration (notifications + notification_settings tables) |
| `app/db/repositories.py` | Added `upsert_user` return value (bool) + 8 notification CRUD functions |
| `app/config.py` | Added `notify_on_user_join`, `notify_on_job_events`, `notify_on_error`, `notify_on_broadcast_events` fields |
| `app/bot/middlewares.py` | `UserGateMiddleware` accepts optional `bus`, publishes `user_joined` on new user |
| `app/core/account_service.py` | Accepts optional `bus`, publishes `account_added`/`account_removed` |
| `app/core/job_manager.py` | Publishes `job_started`/`job_completed`/`job_failed`/`job_cancelled`/`job_interrupted`/`peer_flood`/`account_unauthorized` |
| `app/core/broadcast.py` | Publishes `broadcast_started`/`broadcast_completed`/`broadcast_failed` |
| `app/bot/__init__.py` | `build_dispatcher` accepts `notifications`, injects `dp["notifications"]`, passes `bus` to middleware |
| `app/main.py` | Creates `NotificationService`, wires lifecycle, passes to `build_dispatcher` |
| `app/bot/routers/admin/callbacks.py` | Added `NOTIFY` constants |
| `app/bot/routers/admin/keyboards.py` | Added `notifications_list_kb`, `notify_settings_kb` |
| `app/bot/routers/admin/router.py` | Registered notifications router |
| `app/bot/texts.py` | Added notification strings, renderers, label functions |
| `.env.example` | Added new config vars |

### 4.3 Wiring (main.py)

```python
# Create the shared EventBus
bus = EventBus()

# Create + wire NotificationService
notifications = NotificationService(db, config, bus)
await notifications.set_bot(bot)
notifications.subscribe()

# Pass bus to middleware via build_dispatcher
dp = build_dispatcher(bot=bot, db=db, config=config, bus=bus, notifications=notifications)

# ... on shutdown:
notifications.unsubscribe()
```

### 4.4 Verification

- `python -m pytest -q` → **483 passed**
- `python -c "import app"` → clean import, no errors

---

## 5. Roadmap

### Phase 2 (next)
- `flood_wait` event publishing from `JobManager` (currently documented but not emitted)
- `delivered=1` update after successful DM (requires `_try_dm` to return success and
  `mark_notification_delivered` repo function)
- Admin UI "Resend" button for failed DMs
- Webhook relay to external observability platforms

### Phase 3 (future)
- Notification aggregation for burst events
- PII redaction options in notifications
- Per-admin global enable/disable (not just per-event-type)
- Admin UI dark mode toggle

---

## 6. References

- PRD §12, §14 — Event bus and job progress
- RULES.md §1 — Layer boundaries
- RULES.md §3 — Never lose data silently
- RULES.md §6 — Append-only migrations
- RULES.md §7 — Emoji whitelist in `texts.py`
