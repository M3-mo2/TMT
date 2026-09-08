# Broadcast Campaign Engine

## Overview

The Broadcast Campaign Engine is a **first-class subsystem** inside the admin panel,
not a feature bolted onto the existing admin menu. It transforms the ad-hoc
"send one message to all users" stub into a full **campaign lifecycle system**
with segmentation, scheduling, analytics, retry, and resiliency.

It is built in **6 sequential phases** (see [`Phases.md`](./Phases.md) for the
contract each phase must satisfy before the next can begin).

---

## 1. Vision

An admin opens the admin panel and sees a dedicated **Broadcast Center**:

```
⟡ مركز البث

المسودات     2       ⏳ جاري التشغيل: 1
المجدولة    1
المكتملة   142       ⛧ آخر إكمال: 97.35%

آخر بث
1,284 مستلم · 3:14 · 9.3 رسالة/ثانية

[+] جديد   [⏳ مجدول]   [▣ يوابع]   [⟡ تحليلات]
```

From this screen the admin can:

- Compose a new broadcast from any of their messages (formatting/media/keyboard preserved).
- Select a **target audience** from a builder (all users, active users, segmented by accounts/registration/last-seen, exclusion lists, "don't send to previously contacted").
- **Preview** the audience size and estimated duration before sending.
- **Test-send** to themselves / a small group.
- **Schedule** for a future time or "in 2 hours".
- Watch a **live progress card** while it runs, with **pause / resume / cancel**.
- **Inspect analytics** per campaign and per recipient error.
- Find past campaigns in **history**, with statuses:
  `draft | scheduled | running | completed | cancelled | failed | interrupted`.

---

## 2. Core Concepts

### 2.1 Campaign (Broadcast)

A campaign is a persisted entity in the database:

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PK` | Surrogate key |
| `admin_id` | `INTEGER` | Who created it |
| `label` | `TEXT` | Admin-given name |
| `source_chat_id` / `source_message_id` | `INTEGER` | The admin's original message (for `copy_message`) |
| `mode` | `TEXT` | `copy` (verbatim) or `personalized` (template) |
| `content_html` | `TEXT` | Template text (personalized mode only) |
| `status` | `TEXT` | `draft \| scheduled \| running \| completed \| cancelled \| failed \| interrupted` |
| `scheduled_for` | `TEXT \| NULL` | ISO timestamp for scheduled sends |
| `created_at` / `started_at` / `finished_at` | `TEXT` | Lifecycle timestamps |
| `total_recipients` | `INTEGER` | Audience size at start |
| `sent` / `blocked` / `failed` / `skipped` / `cancelled` | `INTEGER` | Aggregated counters |
| `avg_rate` | `REAL` | Messages/sec over completed duration |
| `ab_test_id` | `INTEGER \| NULL` | Links A/B test variants |
| `recurrence_rule` | `TEXT \| NULL` | Cron-like rule (V3) |

### 2.2 Recipient (BroadcastRecipient)

Per-user delivery record:

| Column | Type | Notes |
|---|---|---|
| `broadcast_id` | `INTEGER FK` | |
| `user_id` | `INTEGER FK` | |
| `status` | `TEXT` | `pending \| sent \| blocked \| failed \| skipped \| delivered` |
| `attempt_count` | `INTEGER` | How many send attempts |
| `last_error` | `TEXT \| NULL` | Last error message |
| `last_attempt_at` | `TEXT \| NULL` | For backoff scheduling |
| `sent_at` | `TEXT \| NULL` | When delivered |

**Deduplication:** `(broadcast_id, user_id)` is unique → re-running `start` never double-sends.

### 2.3 Exclusion List

`broadcast_exclusions(broadcast_id, user_id)` — users explicitly excluded from a campaign
(e.g. admins, VIPs, previously-contacted users).

### 2.4 Error Classification

Errors from `Bot.copy_message` are classified into:

| Kind | Behavior |
|---|---|
| `RETRY_FLOOD` | `TelegramRetryAfter` with small `retry_after` → sleep + retry once |
| `RETRY_DELAYED` | `TelegramRetryAfter` with large `retry_after` → requeue for delayed retry |
| `RETRY_TRANSIENT` | `TelegramServerError` / `TelegramNetworkError` → requeue, exponential backoff |
| `PERMANENT_BLOCKED` | User blocked the bot / deactivated / chat not found → mark `blocked`, never retry |
| `PERMANENT_FAIL` | Everything else → mark `failed`, log detail |

### 2.5 Rate Limiter

A `TokenBucket`-style limiter with two dimensions:

1. **Concurrency** — an `asyncio.Semaphore(max_bcast_concurrency)` caps simultaneous `copy_message` calls.
2. **Throughput** — a sliding window tracks send timestamps; the worker sleeps to stay under `bcast_max_rate_per_second`.

### 2.6 Audience Filter (Segmentation)

The `AudienceResolver` translates an `AudienceFilter` dataclass into a **single SQL query**:

```sql
SELECT u.id FROM users u
LEFT JOIN accounts a ON a.owner_id = u.id
WHERE u.id NOT IN (SELECT user_id FROM broadcast_exclusions WHERE broadcast_id = ?)
  AND (filter conditions...)
```

Supported dimensions:

| Filter | SQL |
|---|---|
| `target=all` | no status filter |
| `target=active` | `u.updated_at > datetime('now', '-30 days')` |
| `target=inactive` | `u.updated_at <= datetime('now', '-30 days')` |
| `target=blocked` | `u.is_blocked = 1` |
| `with_accounts=true` | `EXISTS(SELECT 1 FROM accounts WHERE owner_id=u.id)` |
| `without_accounts=true` | `NOT EXISTS(...)` |
| `account_count_min=N` | subquery `COUNT(*) >= N` |
| `account_count_max=N` | subquery `COUNT(*) <= N` |
| `registered_days_ago=N` | `u.created_at > datetime('now', '-N days')` |
| `last_seen_days_ago=N` | `u.updated_at > datetime('now', '-N days')` |
| `exclude_admins=true` | `u.id NOT IN (admin ids)` |
| `exclude_previously_contacted=true` | `NOT EXISTS(SELECT 1 FROM broadcast_recipients br JOIN broadcasts b ON br.broadcast_id=b.id WHERE br.user_id=u.id AND b.mode IS NOT NULL AND br.status IN ('sent','delivered'))` |

### 2.7 Lifecycle State Machine

```
draft ──────► scheduled ────► running ──────┬────────► completed
 │             │          │                │         (finished_at set)
 │             │          └────────────────┼────────► cancelled
 │             │                             └────────► failed
 │             │                                            (error set)
 │             └──────────────────────────┐
 │                                          ▼
 │                                     interrupted
 │                                          │
 │                                          │ resume
 │                                          ▼
 └─► running (manual start now)
```

Transitions are **guarded** by DB checks (like `JobManager.transition_job`):
you cannot start a `running` campaign, cannot cancel a `completed` one, etc.

### 2.8 Personalization vs Copy Mode

| Mode | Mechanism | Template | Formatting |
|---|---|---|---|
| `copy` | `Bot.copy_message(chat_id, from_chat_id, message_id)` | N/A (uses the admin's original message) | **Perfect** — Telegram re-sends the exact message |
| `personalized` | `Bot.send_message(chat_id, text=rendered, ...)` | `content_html` with `{first_name}`, `{accounts_count}`, etc. | HTML parse mode; user values escaped via `esc()` |

**Key insight:** `copy_message` is always used when possible because it preserves
media albums, inline keyboards, polls, and native formatting. Personalization
is an explicit opt-in because it **cannot** use `copy_message`.

### 2.9 Scheduling & Sweeper

A background sweeper coroutine (modeled on `LoginFlowManager.start_sweeper`) runs
every 30 s. It:

1. Loads all `scheduled_for <= now AND status = 'scheduled'`.
2. Sets them to `running` and calls `Broadcaster.start()`.

A `recurrence_rule` column (V3) lets the sweeper create new scheduled campaigns
from recurring templates.

### 2.10 Crash Recovery

On boot, `Broadcaster.recover()`:

1. Loads all `status = 'running'`.
2. For each:
   - If `finished_at IS NULL` and `sent + blocked + failed + skipped < total_recipients`:
     **resume** by re-spawning the worker task (source message ref is persisted,
     recipients are in the DB).
   - Else: mark `interrupted` (the previous run was killed without finishing).

The in-memory `asyncio.Event` cancel/pause flags are rebuilt fresh — a campaign
that was `running` at crash time will continue sending (no intended cancel was
lost, since cancels are persisted as `status = 'cancelled'`).

---

## 3. Component Map

```
app/bot/routers/admin/broadcast.py    ← thin FSM router (compose → confirm → send)
app/bot/routers/admin/callbacks.py    ← callback-data constants for broadcast buttons
app/bot/routers/admin/keyboards.py    ← keyboard builders
app/bot/texts.py                      ← all Arabic UI strings + renderers
app/core/broadcast.py                 ← Broadcaster service (the engine)
app/core/broadcast_models.py          ← Broadcast dataclass, ErrorKind, AudienceFilter
app/core/rate_limiter.py              ← TokenBucket rate limiter
app/db/migrations.py                  ← V5+: broadcasts + broadcast_recipients + exclusions
app/db/repositories.py                ← broadcast_* repository functions
app/config.py                         ← new config knobs
app/main.py                           ← wires Broadcaster into build_dispatcher, calls recover()
```

---

## 4. Configuration

New config knobs in `app/config.py`:

| Key | Type | Default | Purpose |
|---|---|---|---|
| `max_bcast_concurrency` | `int` | `10` | Simultaneous `copy_message` calls |
| `bcast_max_rate_per_second` | `int` | `25` | Throughput cap |
| `bcast_flood_retry_threshold` | `int` | `60` | Max `retry_after` to retry inline (seconds) |
| `bcast_edit_interval` | `float` | `3.0` | Min seconds between progress-card edits |
| `bcast_retry_attempts` | `int` | `3` | Max retry attempts for transient failures |
| `bcast_retry_backoff_base` | `float` | `2.0` | Exponential backoff base |
| `bcast_batch_size` | `int` | `50` | Recipients loaded per DB cursor page |

---

## 5. Error Handling Strategy

1. **Never let a send exception kill the worker.** Every `copy_message` is wrapped
   in `try/except TelegramAPIError, asyncio.CancelledError, Exception`.
2. **Classify, don't guess.** The `classify_error` function maps exceptions to
   `ErrorKind`. No `except Exception: pass`.
3. **Persist every failure.** A failed `broadcast_recipients` row has `last_error`
   set so the admin can inspect what went wrong.
4. **Rate-limit-aware.** `TelegramRetryAfter` is respected — the limit applies
   **globally** to the bot, so respecting it protects both broadcast and user
   transfers.
5. **Audit trail.** `audit_log` writes: `broadcast_started`, `broadcast_finished`,
   `broadcast_cancelled`, `broadcast_paused`, `broadcast_resumed`. Recipient-level
   events are **not** audited (would be thousands of rows) — only aggregate.

---

## 6. Testing Strategy

### 6.1 Existing Patterns

- `tests/fakes.py` provides `FakeTelegramClient`, `FakeClientPool` for Telethon.
- `tests/test_reporter.py` provides `FakeBot` with `send_message` / `edit_message_text`.
- `tests/conftest.py` provides a real `Database` fixture with migrations applied.
- Async tests use `asyncio_mode = "auto"` (pytest-asyncio).

### 6.2 New Test Doubles

Extend `FakeBot` (or create `FakeBroadcastBot` in `tests/fakes.py`):

```python
class FakeBroadcastBot:
    def __init__(self, *, fail_for: dict[int, Exception] = None,
                 delay: float = 0.0):
        self.calls: list[dict] = []
        self.fail_for = fail_for or {}
        self.delay = delay
    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        self.calls.append({"chat_id": chat_id, "from_chat_id": from_chat_id,
                           "message_id": message_id})
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(message_id=999)
    async def send_message(self, chat_id, text, **kw):
        self.calls.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=999)
    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.calls.append({"chat_id": chat_id, "message_id": message_id, "text": text})
```

### 6.3 Test Coverage Per Phase

| Phase | Test File | What's Tested |
|---|---|---|
| V1 | `tests/test_broadcast_db.py` | Migration schema, `insert_recipients` bulk, `get_broadcast` |
| V1 | `tests/test_audience_resolver.py` | Each filter dimension produces correct SQL + results |
| V1 | `tests/test_rate_limiter.py` | Throughput cap respected, concurrency semaphore |
| V1 | `tests/test_broadcaster.py` | Happy path, forbidden error → blocked, flood retry, transient retry, backoff |
| V2 | `tests/test_bcast_scheduling.py` | Sweeper picks up scheduled, `recover()` resumes interrupted |
| V2 | `tests/test_bcast_pause.py` | Pause/resume event flow, DB state |
| V3 | `tests/test_bcast_personalization.py` | Template rendering + escaping |
| V3 | `tests/test_bcast_ab.py` | A/B split deterministic + analytics |

All tests run **offline** against the in-memory / tmp file SQLite (via the `db` fixture).

---

## 7. Security & Compliance Notes

- **Callback data is user-controlled input (RULES §4).** Every broadcast callback handler
  parses the campaign ID from `int(data.removeprefix(prefix))` and re-verifies ownership
  via the DB join, never trusting the callback value alone.
- **Secrets:** Source message IDs and recipient user IDs are not secrets, but they are
  never logged. The admin's original message content is fetched by Telegram's `copy_message`,
  never stored in our DB.
- **PII:** User names/usernames in `broadcast_recipients` are not stored (only the `user_id` FK).
  Personalization renders at send time from `users` table columns.
- **Rate limits:** Respecting `TelegramRetryAfter` globally protects the bot from being
  rate-limited, which could disrupt user-facing transfers.

---

## 8. Dependencies

No new third-party dependencies. `croniter` was considered for V3 recurrence but a simple
`interval_days` integer field suffices for the core use cases; cron can be added later.

---

## Implementation Notes (Post-Spec)

The following notes document how the spec was realized in code, including
deviations and implementation decisions not captured in the per-phase contracts.

### V7 Migration: draft_data + recurrence_rule columns

V6 was already used for `filter_json` (added in Phase 3 for audience recovery).
V7 (`app/db/migrations.py`) adds two `ALTER TABLE broadcasts ADD COLUMN`
statements:
- `draft_data TEXT` — serialized FSM state JSON, snapshot of the
  `AudienceFilter` at every step so `cb_bcast_draft_resume` can restore mid-flow.
- `recurrence_rule TEXT` — stores a recurrence definition (e.g. `"daily"`)
  for Phase 3 recurring-campaign support.

### V8 Migration: ab_test_id FK + ab_tests table + broadcast_templates table

V8 (`app/db/migrations.py`) creates two new tables and adds a foreign key:
- `ab_tests(id PK, name TEXT, created_at TEXT)` — one row per A/B test group.
- `broadcast_templates(id PK, name TEXT, content_html TEXT, parse_mode TEXT, is_personalized INTEGER, created_at TEXT)` — reusable message templates.
- `ALTER TABLE broadcasts ADD COLUMN ab_test_id INTEGER REFERENCES ab_tests(id)` — links each variant Broadcast to its parent test.

### Broadcaster Sweeper

Runs at a `bcast_sweep_interval` (config field `bcast_sweep_interval: int = Field(default=30, ge=5)`
in `app/config.py`). The `run_sweeper()` coroutine loops: queries
`repo.list_scheduled_broadcasts(db)` (SQL: `status='scheduled' AND scheduled_for <= now`),
promotes each due campaign to `running` via `start()`, then sleeps for the interval.
Controlled by `start_sweeper(bot)` / `stop_sweeper()` — mirrors `LoginFlowManager.start_sweeper`.

### Personalization

When `mode='personalized'`, `_send_one()` calls `bot.send_message` with a template
rendered via `_safe_format()`. The engine is `_SafeFormatter` (a subclass of
`string.Formatter`) that overrides `get_value()` to return `""` for missing keys
instead of raising `KeyError`. User values are escaped with `html.escape(str(v),
quote=True)` before interpolation — the core layer uses stdlib `html.escape`,
NOT `texts.py.esc()`, because `app/core/` must not depend on `app/bot/` (RULES §1).

### A/B Testing

`create_ab_test(name, splits, *, admin_id, source_chat_id, source_message_id)`
inserts an `ab_tests` row then creates one draft Broadcast per variant via
`repo.create_broadcast(..., ab_test_id=)`. At delivery time,
`_ab_test_split(user_id, num_variants)` assigns a variant using
`user_id % num_variants` — deterministic, even, and stateless. The spec's
weighted form (`user_id % 100 < weight_pct`) reduces to this for equal splits.

### Admin Router

15 handler functions in `app/bot/routers/admin/broadcast.py`, backed by `BcastFSM`
with 3 states (`compose`, `target`, `scheduled_for`). `scheduled_for` parsing
is handled by `_parse_schedule_time()`, which supports ISO timestamps
(`2024-01-15T20:00:00Z`), relative offsets (`+2h`, `+1d`, `+30m`), and natural
language / Arabic keywords (`tomorrow`, `غداً`, `اليوم`). Returns `None` for
unparseable or past inputs. Campaign IDs from callback data are parsed via
`_int_after()` (returns `None` on malformed input — callback data is user input,
RULES §4).

### cancel() Deviation

The spec (Phase 6 §2.8) describes `cancel(self, campaign_id) -> None`, but the
actual implementation keeps the Phase 3 signature
`cancel(self, campaign_id, bot) -> bool`. The `bot` parameter is accepted for
API compatibility with the Phase 4 router handler signature but is unused in the
method body. Returns `False` if the campaign is not currently running.
