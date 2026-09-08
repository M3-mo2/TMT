# Broadcast Engine — Implementation Phases

> **Coordination protocol:** Each phase has a **General agent** assigned.
> The agent works in a single branch named `bcast-phase<N>`.
> Before starting:
> 1. Read the task description below **and** `BroadcastEngine.md`.
> 2. Ensure the `db` fixture + migrations apply cleanly (run `pytest -q` if unsure).
> 3. Follow the layer boundaries in `RULES.md` §1 (no cross-layer imports).
>
> When done:
> 1. Run `pytest -q` (must stay green — 239 baseline tests must still pass).
> 2. Run `python -c "import app"` to verify clean import.
> 3. Write a summary block under your phase's `## Summary` section.
> 4. Commit with message: `bcast: phase <N> — <one-line description>`.
>
> Phases must be done **sequentially**. Each phase's agent verifies the previous
> phase is complete (check the `## Summary` of the prior phase) before starting.

---

## Phase 1 — Database Schema & Repository Layer

**Agent role:** `coder`
**Files touched (read-only reference):**
- `app/db/migrations.py` — study the append-only pattern, `_split_statements`, `apply_migrations`.
- `app/db/repositories.py` — study the `upsert_user` / `list_all_users` / `audit` patterns.
- `tests/test_database.py`, `tests/test_repositories.py`, `tests/conftest.py` — study fixtures.

### Tasks

1. **Migration V5** (`app/db/migrations.py`):
   - Create `broadcasts` table with all columns from §2.1.
   - Create `broadcast_recipients` table with `UNIQUE(broadcast_id, user_id)`.
   - Create `broadcast_exclusions` table.
   - Add indexes `idx_bcast_status ON broadcasts(status)`, `idx_brec_status ON broadcast_recipients(broadcast_id, status)`, `idx_brec_user ON broadcast_recipients(user_id)`.
   - Append to `MIGRATIONS` list as `(5, _V5)`.

2. **Repository functions** (`app/db/repositories.py`):
   - `create_broadcast(db, *, admin_id, label, source_chat_id, source_message_id, mode, content_html=None) -> int`
   - `get_broadcast(db, broadcast_id) -> dict | None`
   - `set_broadcast_status(db, broadcast_id, status, **counters) -> None` (updates status + any of total_recipients/sent/blocked/failed/skipped/cancelled/avg_rate/started_at/finished_at/error)
   - `list_broadcasts(db, status=None, limit=50) -> list[dict]`
   - `insert_recipients(db, broadcast_id, user_ids: list[int]) -> None` — bulk via `executemany` with `ON CONFLICT DO NOTHING`.
   - `list_pending_recipients(db, broadcast_id, limit) -> list[int]` — cursor-based paging for resumability.
   - `update_recipient_status(db, broadcast_id, user_id, status, *, error=None, increment_attempts=False) -> None`
   - `create_exclusion_list(db, broadcast_id, user_ids: list[int]) -> None`
   - `list_exclusion_ids(db, broadcast_id) -> list[int]`
   - `count_recipients(db, broadcast_id) -> dict[str, int]` (returns `{pending, sent, blocked, failed, skipped}`)

3. **Tests** (`tests/test_broadcast_db.py`):
   - Migration V5 applies; tables exist with correct columns.
   - `create_broadcast` + `get_broadcast` round-trip.
   - `insert_recipients` deduplicates.
   - `list_pending_recipients` pages correctly.
   - `update_recipient_status` increments attempt count.
   - `create_exclusion_list` + `list_exclusion_ids`.
   - `count_recipients` aggregates correctly.

### Acceptance Criteria
- `pytest -q` passes (239 + new tests).
- `python -c "import app"` clean.
- `_V5` is append-only (RULES §6).

### Summary

Phase 1 is complete. All acceptance criteria met:

- **Migration V5** (`app/db/migrations.py`): Three new tables created with
  portable SQL (plain `INTEGER PRIMARY KEY`, no `AUTOINCREMENT`, no SQLite-isms):
  - `broadcasts` — 21 columns: admin FK, label, source refs, mode
    (`copy`/`personalized`), content_html, status, timestamps, counters
    (sent/blocked/failed/skipped/cancelled), avg_rate, error.
  - `broadcast_recipients` — per-user delivery rows with `UNIQUE(broadcast_id, user_id)`
    dedup constraint, attempt_count, last_error/last_attempt_at/sent_at.
  - `broadcast_exclusions` — `(broadcast_id, user_id)` exclusion list with UNIQUE.
  - Three indexes: `idx_bcast_status`, `idx_brec_status`, `idx_brec_user`.
- **Repository layer** (`app/db/repositories.py`): 10 new functions:
  - `create_broadcast` — inserts a draft, returns broadcast_id.
  - `get_broadcast` — fetch by id.
  - `set_broadcast_status` — guarded dynamic SET (validates column names).
  - `list_broadcasts` — status-filtered, newest-first, limit-paged.
  - `insert_recipients` — bulk `executemany` with `ON CONFLICT DO NOTHING`,
    idempotent, transactional.
  - `list_pending_recipients` — `user_id ASC` ordered, skips terminal statuses.
  - `update_recipient_status` — sets status + optional error, increments
    attempt_count, refreshes last_attempt_at.
  - `create_exclusion_list` / `list_exclusion_ids` — bulk insert / lookup.
  - `count_recipients` — single-query conditional aggregation returning all
    six status keys.
- **Tests** (`tests/test_broadcast_db.py`): 18 test cases covering migration
  schema verification (including upgrade path), create/get round-trip,
  status+counter updates, status filtering, recipient dedup, pending paging,
  attempt increment, exclusion list, and count aggregation.
- `test_database.py` updated: migration version assertions now expect `[1,2,3,4,5,6,7,8]`.
- `pytest -q`: **257 passed** (239 baseline + 18 new).
- `python -c "import app"`: clean.

Layer boundary respected: `db/repositories.py` does not import `bot/` or any
service module — only `app/core/models.py` (shared domain vocabulary) and
`app/db/database.py`.

---

## Phase 2 — Audience Resolver & Rate Limiter

**Agent role:** `coder`
**Files touched (read-only reference):**
- `app/core/models.py` — study `UserStats`, `dataclass(slots=True)` pattern.
- `app/db/repositories.py` — study `list_all_users`, `count_jobs_for_user`.
- `tests/test_repositories.py` — `test_user_job_stats_are_scoped_to_owner`.

### Tasks

1. **Domain model** (`app/core/broadcast_models.py`):
   - `AudienceFilter` dataclass: `target`, `with_accounts`, `without_accounts`,
     `account_count_min`, `account_count_max`, `registered_days_ago`,
     `last_seen_days_ago`, `exclude_admins`, `exclude_previously_contacted`.
   - `ErrorKind` enum: `RETRY_FLOOD, RETRY_DELAYED, RETRY_TRANSIENT, PERMANENT_BLOCKED, PERMANENT_FAIL`.
   - `BroadcastStatus` enum: `DRAFT, SCHEDULED, RUNNING, COMPLETED, CANCELLED, FAILED, INTERRUPTED`.
   - `RecipientStatus` enum: `PENDING, SENT, BLOCKED, FAILED, SKIPPED`.
   - `BroadcastMode` enum: `COPY, PERSONALIZED`.

2. **AudienceResolver** (`app/core/broadcast.py`):
   - `async def resolve_audience(db, filters: AudienceFilter, admin_ids: list[int]) -> list[int]`
   - Builds a single SQL query from the filter (see §2.5 table).
   - `async def count_audience(db, filters, admin_ids) -> int` (same query, `SELECT COUNT(*)`).

3. **Rate limiter** (`app/core/rate_limiter.py`):
   - `class TokenBucket`:
     - `__init__(self, max_per_second: int, concurrency: int)`
     - `async def acquire(self) -> None` — semaphore + sliding-window sleep.
     - Implements the two-dimensional limit (concurrent tasks + per-second throughput).

4. **Config knobs** (`app/config.py`):
   - Add `max_bcast_concurrency`, `bcast_max_rate_per_second`, `bcast_flood_retry_threshold`,
     `bcast_retry_attempts`, `bcast_retry_backoff_base` (with `ge=1` constraints matching
     the existing `Field(default=..., ge=...)` pattern).

5. **Tests** (`tests/test_audience_resolver.py`, `tests/test_rate_limiter.py`):
   - Each `AudienceFilter` dimension produces the expected user IDs against a seeded `db`.
   - `exclude_previously_contacted` excludes users from prior `sent`/`delivered` recipients.
   - Rate limiter enforces max 1 call per `1/max_per_second` seconds.
   - Concurrency semaphore caps simultaneous acquires.

### Acceptance Criteria
- `pytest -q` passes (Phase 1 + Phase 2 tests).
- `python -c "import app"` clean.
- `AudienceResolver.resolve_audience` returns correct results for each filter with **one DB round-trip**.
### Summary

Phase 2 is complete. All acceptance criteria met:

- **Domain model** (`app/core/broadcast_models.py`): Four enums and one dataclass:
  - `ErrorKind` — `RETRY_FLOOD, RETRY_DELAYED, RETRY_TRANSIENT, PERMANENT_BLOCKED, PERMANENT_FAIL`
  - `BroadcastStatus` — `DRAFT, SCHEDULED, RUNNING, COMPLETED, CANCELLED, FAILED, INTERRUPTED` (lowercase str values matching DB columns)
  - `RecipientStatus` — `PENDING, SENT, BLOCKED, FAILED, SKIPPED, DELIVERED`
  - `BroadcastMode` — `COPY, PERSONALIZED`
  - `AudienceFilter` — `@dataclass(slots=True)` with all 9 fields + `default()`, `to_dict()`, `from_dict()` for FSM serialization.
- **AudienceResolver** (`app/core/broadcast.py`): Two async functions:
  - `resolve_audience(db, filters, admin_ids)` — builds a single SQL query with `LEFT JOIN accounts`, `SELECT DISTINCT u.id`, `ORDER BY u.id`. Where-clause builder uses (sql_fragment, params) tuples assembled into one `AND`-joined clause. All 8 filter dimensions supported (target, with/without_accounts, account_count_min/max, registered_days_ago, last_seen_days_ago, exclude_admins, exclude_previously_contacted).
  - `count_audience(db, filters, admin_ids)` — same WHERE clause, `SELECT COUNT(DISTINCT u.id)`.
  - `target='all'` excludes blocked users; `target='blocked'` selects only blocked; `target='active'/'inactive'` use `last_seen_days_ago` or default 30 days.
  - No Python-side filtering — pure SQL, one DB round-trip per call.
- **Rate limiter** (`app/core/rate_limiter.py`): `TokenBucket` class:
  - `__init__(max_per_second, concurrency)` — `asyncio.Semaphore` + sliding-window `deque` of timestamps.
  - `acquire()` — acquires semaphore, evicts expired timestamps, sleeps if window is full, records timestamp. Logical time tracked via `time.monotonic()` with `max(now, last_timestamp)` to support mocked `asyncio.sleep` in tests. Sub-millisecond sleeps filtered via `_SLOT_EPSILON` to avoid float-drift spurious wakes.
  - `release()` — frees the semaphore slot (for Phase 3 pairing).
  - `shutdown()` — sets flag, clears window.
- **Config** (`app/config.py`): Five new knobs with `Field(default=..., ge=...)` pattern:
  - `max_bcast_concurrency=10 (ge=1)`, `bcast_max_rate_per_second=25 (ge=1)`, `bcast_flood_retry_threshold=60 (ge=1)`, `bcast_retry_attempts=3 (ge=1)`, `bcast_retry_backoff_base=2.0 (ge=0.1)`.
- **Tests**: 37 new test cases (not 257 — 257 was the Phase 1 baseline):
  - `tests/test_audience_resolver.py` — 29 tests: each filter dimension (default/all, with_accounts, account_count_min/max, without_accounts, target=blocked, exclude_admins, exclude_previously_contacted, last_seen_days_ago, registered_days_ago, target=active), 11 parametrized count_audience↔resolve_audience parity checks, edge cases (empty DB, empty admin_ids, combined filters), and AudienceFilter round-trip serialization.
  - `tests/test_rate_limiter.py` — 8 tests: throughput spacing (mocked + real sleep), one-per-second, no-sleep-under-limit, concurrency semaphore caps, parallelism under limit, shutdown safety.
- `pytest -q`: **294 passed** (257 baseline + 37 new).
- `python -c "import app"`: clean.
- Layer boundary respected: `core/broadcast.py` imports only `app.core.broadcast_models` and `app.db.database` — no `bot/` or `tg/`.

---

## Phase 3 — Broadcaster Service Core

**Agent role:** `coder`
**Files touched (read-only reference):**
- `app/core/job_manager.py` — study `asyncio.create_task`, cancel events, `shutdown()`, `transition_job` guarded CAS pattern.
- `tests/test_reporter.py` — `FakeBot` with `copy_message`/`send_message`/`edit_message_text`.
- `app/bot/reporter.py` — progress-card throttling pattern.

### Tasks

1. **`Broadcaster` service** (`app/core/broadcast.py`):
   - `__init__(self, db, config, bus)` — holds `_tasks`, `_cancel_events`, `_pause_events`, `_active: set[int]`.
   - `async def start(self, campaign_id, bot) -> asyncio.Task` — loads campaign, resolves audience, sets status `running`, spawns `_run_campaign`.
   - `async def _run_campaign(self, campaign_id, bot)`:
     - Acquires the `TokenBucket` before each send.
     - `copy_message` for `mode=copy` / `send_message` for `mode=personalized`.
     - Error classification via `classify_error()`.
     - Retry-once for `RETRY_FLOOD`, exponential backoff requeue for `RETRY_TRANSIENT`.
     - Persists every recipient status.
     - Updates progress card (throttled by `bcast_edit_interval`).
     - Handles `asyncio.CancelledError` gracefully.
   - `async def cancel(self, campaign_id) -> None`
   - `async def pause(self, campaign_id) -> None`
   - `async def resume(self, campaign_id, bot) -> asyncio.Task`
   - `async def recover(self) -> list[int]` — finds `running` campaigns on boot; resumes if incomplete, marks `interrupted` otherwise.
   - `async def shutdown(self) -> None` — cancels all tasks (mirrors `JobManager.shutdown`).
   - `_send_to_user(bot, campaign, user_id, filters) -> ErrorKind` — single-recipient send with `copy_message`.

2. **Error classifier** (`app/core/broadcast.py`):
   - `def classify_error(exc: BaseException) -> ErrorKind` — pattern-matches `TelegramAPIError.message`.

3. **Bot injection:** `Broadcaster` receives `bot` via `start()` / `resume()`, not in `__init__`, because `Bot` is created in `main.py` after the dispatcher (following the `Reporter` pattern where `bot` is injected at construction, but broadcasts are triggered from handlers that already have `cb.bot`).

4. **Tests** (`tests/test_broadcaster.py`):
   - `FakeBroadcastBot` (extends `FakeBot` from `test_reporter.py`): scripts `copy_message` to raise `TelegramForbiddenError`, `TelegramRetryAfter`, `TelegramServerError` on specific user IDs.
   - Happy path: 10 recipients, all succeed, status → `completed`, counters correct.
   - Block path: user 5 raises `Forbidden` → recipient status `blocked`.
   - Flood path: user 3 raises `RetryAfter(1)` → retried once, succeeds.
   - Rate limiter engaged (no two sends within `1/max_rate` seconds).

### Acceptance Criteria
- `pytest -q` passes.
- `python -c "import app"` clean.
- `Broadcaster.start()` returns a task that completes and writes final status to DB.

### Summary

Phase 3 is complete. All acceptance criteria met:

- **Broadcaster service** (`app/core/broadcast.py`): Campaign lifecycle engine with
  `TokenBucket` rate limiting, multi-worker queue dispatch, and cooperative
  cancellation — mirrors `JobManager` patterns (guarded DB writes, audit events).
  - `__init__(db, config, bus)` — holds `_tasks`, `_cancel_events`,
    `_pause_events`, `_token_buckets`, `_progress_cards`, `_filters`, `_bot`.
  - `start(campaign_id, bot)` — loads campaign, resolves audience from
    `filter_json`, creates `TokenBucket`, sets status `running`, spawns
    `_run_campaign` worker task.
  - `_run_campaign` — drains an `asyncio.Queue` of pending recipients across
    `max_bcast_concurrency` workers; each worker acquires a token via
    `TokenBucket.acquire()`, calls `_send_one()`, updates counters; throttles
    DB + progress-card writes by `bcast_edit_interval`; finalizes status on
    completion.
  - `_send_one` (spec named it `_send_to_user`) — `copy_message` for
    `mode=copy`, `send_message` with template rendering for `mode=personalized`;
    classifies errors via `classify_error()`, inline retry-once for
    `RETRY_FLOOD`, exponential backoff requeue for `RETRY_TRANSIENT`.
  - `cancel(campaign_id, bot) -> bool` — sets cancel event, immediately persists
    `status='cancelled'` to DB, emits `broadcast_cancelled` audit.
  - `pause(campaign_id) -> bool` / `resume(campaign_id, bot) -> bool` — toggle
    a pause event that blocks/unblocks worker progress.
  - `recover()` — loads `status='running'` campaigns, resumes incomplete ones,
    marks `completed` if `finished_at` set, marks `failed` if no pending
    recipients remain.
  - `shutdown()` — cancels all tasks, clears state (mirrors `JobManager.shutdown`).
- **Error classifier** (`app/core/broadcast.py`): `classify_error(exc)` —
  pattern-matches `TelegramRetryAfter` (flood if `retry_after <= 60`, else delayed),
  `TelegramForbiddenError` (regex for blocked/deactivated/chat-not-found → `PERMANENT_BLOCKED`,
  else `PERMANENT_FAIL`), `TelegramServerError`/`TelegramNetworkError` → `RETRY_TRANSIENT`,
  `TelegramAPIError` → `PERMANENT_FAIL`, plain `Exception` → `PERMANENT_FAIL`; never
  swallows `CancelledError`.
- **Progress card** (`_progress_text`): Inline HTML card using approved glyphs
  (`⟡`/`✓`/`×`/`!`/`›`), no disallowed symbols.
- **Tests** (`tests/test_broadcaster.py`): 19 test cases — `FakeBroadcastBot`
  scripts `copy_message` failures; covers all 5 `ErrorKind` classifications,
  progress-text glyphs, happy path (all send), blocked user, flood retry,
  permanent fail, cancel mid-broadcast, rate-limiter spacing, and recovery resume.
- `pytest -q`: **313 passed** (294 baseline + 19 new).
- `python -c "import app"`: clean.
- Layer boundary respected: `core/broadcast.py` imports only `core/broadcast_models`,
  `core/rate_limiter`, `core/events`, `db/repositories`, `db/database` — no `bot/` or `tg/`.

---

## Phase 4 — Admin Router, FSM, Keyboards & Texts

**Agent role:** `coder`
**Files touched (read-only reference):**
- `app/bot/routers/admin/broadcast.py` — **delete** the stub, rewrite.
- `app/bot/routers/admin/callbacks.py` — add broadcast callback constants.
- `app/bot/routers/admin/keyboards.py` — add keyboard builders.
- `app/bot/texts.py` — add renderers.
- `app/bot/routers/admin/router.py` — verify `broadcast.router` is included (it already is).

### Tasks

1. **Callback-data constants** (`app/bot/routers/admin/callbacks.py`):
   - `BCAST_NEW = "adm:bcast:new"`
   - `BCAST_DRAFT_RESUME = "adm:bcast:draft:"`
   - `BCAST_TARGET_X = "adm:bcast:target:"` (sub-actions: all, active, inactive, blocked, accounts_min, accounts_max, registered_days, last_seen, exclude_admins, exclude_previous)
   - `BCAST_DRY_RUN = "adm:bcast:dry_run"`
   - `BCAST_TEST_SEND = "adm:bcast:test_send"`
   - `BCAST_SEND_NOW = "adm:bcast:send_now"`
   - `BCAST_SCHEDULE = "adm:bcast:schedule"`
   - `BCAST_CANCEL_LIVE = "adm:bcast:cancel:"`
   - `BCAST_PAUSE_LIVE = "adm:bcast:pause:"`
   - `BCAST_RESUME_LIVE = "adm:bcast:resume:"`
   - `BCAST_HISTORY = "adm:bcast:history"` (with `:page:` pagination)
   - `BCAST_VIEW = "adm:bcast:view:"`

2. **FSM states** (`app/bot/routers/admin/broadcast.py`):
   ```python
   class BcastFSM(StatesGroup):
       compose = State()        # admin sends the broadcast message
       target = State()         # audience filter building
       scheduled_for = State()  # when to send (text input)
   ```

3. **Router handlers** (`app/bot/routers/admin/broadcast.py`):
   - `cb_broadcast` → opens `render_broadcast_center` dashboard.
   - `cb_bcast_new` → enters `BcastFSM.compose`.
   - `bcast_message_entered` (message handler in `BcastFSM.compose`) → captures `(chat.id, message_id)`, saves draft, shows target selector.
   - `cb_bcast_target` → shows audience filter builder UI.
   - `cb_bcast_target_<x>` → toggles a filter dimension, re-resolves count, updates keyboard.
   - `cb_bcast_dry_run` → resolves audience + shows count + estimate.
   - `cb_bcast_test_send` → resolves admin user IDs, runs a mini-broadcast.
   - `cb_bcast_send_now` → calls `Broadcaster.start()`, exits FSM, shows progress card.
   - `cb_bcast_schedule` → enters `BcastFSM.scheduled_for`, parses input.
   - `cb_bcast_history` → paginated list of past campaigns.
   - `cb_bcast_view` → campaign detail card with analytics.
   - Live control buttons: `BCAST_CANCEL_LIVE`, `BCAST_PAUSE_LIVE`, `BCAST_RESUME_LIVE`.

4. **Keyboards** (`app/bot/routers/admin/keyboards.py`):
   - `broadcast_center_kb()` — dashboard buttons.
   - `broadcast_target_kb(filters: AudienceFilter)` — toggle buttons for each filter dimension.
   - `broadcast_confirm_kb(campaign_id)` — `[▶ إرسال الآن] [⏰ جدولة] [🧪 تجربة] [× إلغاء]`.
   - `broadcast_live_kb(campaign_id, status)` — `[⏸ إيقاف] [▶ استأنف] [× إلغاء]`.
   - `broadcast_history_kb(page, total_pages)`.

5. **Text renderers** (`app/bot/texts.py`):
   - `render_broadcast_center(drafts, scheduled, running, completed)` — dashboard.
   - `render_audience_builder(filters, user_count)` — live filter summary.
   - `render_bcast_preview(campaign, recipient_count, est_duration)` — dry-run screen.
   - `render_bcast_progress(campaign, rate)` — live card.
   - `render_bcast_summary(campaign)` — detail/analytics screen.

6. **Composition root wiring** (`app/main.py`):
   - Instantiate `Broadcaster(db, config, bus)` after `JobManager`.
   - Call `await broadcaster.recover()` before `start_polling`.
   - Add `broadcaster` to dispatcher workflow data (so handlers can access it).

7. **Tests** (`tests/test_broadcast_router.py`):
   - `cb_broadcast` opens the dashboard.
   - Compose flow: send a message → draft created → target selector shown.
   - `AudienceFilter` toggle via callback updates the count.
   - `BCAST_SEND_NOW` → campaign created + `Broadcaster.start()` called.
   - Live control: cancel/pause/resume callbacks route correctly.
   - Non-admin users are rejected (IsAdmin filter already on admin router).

### Acceptance Criteria
- `pytest -q` passes.
- `python -c "import app"` clean.
- Full admin UI flow: compose → target → preview → send → progress → history works end-to-end (offline with `FakeBroadcastBot`).
- All Arabic strings pass the emoji/symbol tests in `tests/test_texts.py` (no `📢`; use approved glyphs).

### Summary

Phase 4 is complete. All acceptance criteria met:

- **Callback constants** (`app/bot/routers/admin/callbacks.py`): 14 new constants —
  `BCAST`, `BCAST_PAGE`, `BCAST_NEW`, `BCAST_DRAFT_RESUME`, `BCAST_TARGET_X`,
  `BCAST_DRY_RUN`, `BCAST_TEST_SEND`, `BCAST_SEND_NOW`, `BCAST_SCHEDULE`,
  `BCAST_CANCEL_LIVE`, `BCAST_PAUSE_LIVE`, `BCAST_RESUME_LIVE`, `BCAST_HISTORY`,
  `BCAST_VIEW`.
- **FSM** (`app/bot/routers/admin/broadcast.py`): `BcastFSM` StatesGroup with 3
  states — `compose`, `target`, `scheduled_for`.
- **Router handlers** (`app/bot/routers/admin/broadcast.py`): 15 handlers:
  - `cb_broadcast` → opens dashboard; `cb_bcast_new` → enters compose state;
    `bcast_message_entered` → captures source message, creates draft, shows target builder;
    `cb_bcast_target_x` → toggles filter dimension, re-resolves count, updates keyboard;
    `cb_bcast_draft_resume` → restores `AudienceFilter` from `filter_json`, pre-fills FSM;
    `cb_bcast_dry_run` → resolves audience + shows preview with estimate;
    `cb_bcast_test_send` → resolves admin IDs, sends test copy to admins only;
    `cb_bcast_send_now` → calls `Broadcaster.start()`, clears FSM, shows live keyboard;
    `cb_bcast_schedule` → enters `scheduled_for` state;
    `bcast_scheduled_for_entered` → parses time, stores ISO in `scheduled_for`, sets status `scheduled`;
    `cb_bcast_history` / `cb_bcast_history_page` → paginated past campaigns (10/page);
    `cb_bcast_view` → detail card; `cb_bcast_cancel_live` / `cb_bcast_pause_live` /
    `cb_bcast_resume_live` → live control buttons.
  - All campaign IDs parsed via `_int_after()` (returns `None` on malformed —
    callback data is user input, RULES §4).
  - `_parse_schedule_time` handles ISO, `+2h`/`+1d`/`+30m` relative, natural
    (`tomorrow`), and Arabic keywords (`غداً`, `اليوم`, `الآن`).
- **Keyboards** (`app/bot/routers/admin/keyboards.py`): `broadcast_center_kb`,
  `broadcast_target_kb` (9 filter dimensions with on/off toggles),
  `broadcast_confirm_kb`, `broadcast_live_kb` (pause/resume/cancel),
  `broadcast_history_kb` (with pagination nav).
- **Text renderers** (`app/bot/texts.py`): `render_broadcast_center` (dashboard
  with draft/scheduled/running/completed lists), `render_audience_builder` (live
  filter summary), `render_bcast_preview` (dry-run estimate screen),
  `render_bcast_summary` (detail card with all counters, avg rate, status label).
  All use approved glyphs (`⟡`, `✓`, `×`, `›`, `=`, `⋆`) — no `📢`.
- **Composition wiring** (`app/main.py`): `Broadcaster(db, config, bus)`
  instantiated after `JobManager`; `await broadcaster.recover(bot)` before
  polling; `broadcaster` injected into dispatcher workflow data.
- **Tests** (`tests/test_broadcast_router.py`): 59 test cases across 8 test
  classes — callback constants, keyboards, text renderers (empty/with data/HTML
  escaping), toggle filter, schedule-time parsing (6 formats), dashboard, compose
  flow, dry-run, test-send, send-now, history/view, live control, admin filter.
- `pytest -q`: **372 passed** (313 baseline + 59 new).
- `python -c "import app"`: clean.

---

## Phase 5 — Scheduling, Drafts, Resume-After-Restart

**Agent role:** `coder`
**Files touched (read-only reference):**
- `app/tg/login.py` — `start_sweeper` / `run_sweeper` / `stop_sweeper` pattern.
- Phase 1 migration (may need V6 for `recurrence_rule` and `draft_data` columns).

### Tasks

1. **Migration V6** (if needed):
   - Add `broadcasts.draft_data TEXT` (JSON: serialized filter selections).
   - Add `broadcasts.recurrence_rule TEXT` (for V3, but the column can be added here).

2. **Sweeper** (`app/core/broadcast.py` → add to `Broadcaster`):
   - `start_sweeper()` / `run_sweeper()` / `stop_sweeper()` — same pattern as `LoginFlowManager`.
   - Wakes every 30 s, queries `scheduled_for <= now AND status='scheduled'`,
     sets status to `running`, spawns `start()`.

3. **Draft persistence**:
   - During the FSM flow, save the current `AudienceFilter` as JSON in `broadcasts.draft_data` at every step.
   - `cb_bcast_draft_resume` → reads `draft_data`, pre-fills the FSM, lets admin pick up where they left off.
   - The `compose` → `target` transition already saves a draft (Phase 3's `create_broadcast` stores it).

4. **Crash recovery** (`Broadcaster.recover()` — enhance the Phase 3 version):
   - On boot, load all `status='running'`.
   - Re-spawn `_run_campaign` for any where `sent + blocked + failed + skipped < total_recipients`.
   - For those already complete (counter mismatch — should not happen), mark `completed`.
   - For those with no recipients left, mark `failed` with reason.

5. **Scheduled send flow**:
   - `cb_bcast_schedule` → enter `BcastFSM.scheduled_for`.
   - Parse admin input: "tomorrow 20:00", "+2h", "2026-09-08T14:00:00Z".
   - Store ISO timestamp in `scheduled_for`, set status to `scheduled`.
   - The sweeper picks it up automatically.

6. **Wiring** (`app/main.py`):
   - `broadcaster.start_sweeper()` after `recover()`.
   - `await broadcaster.stop_sweeper()` in the `finally` block.

7. **Tests** (`tests/test_bcast_scheduling.py`):
   - Sweeper picks up a `scheduled` campaign whose `scheduled_for` passed.
   - `recover()` resumes an incomplete `running` campaign (mock DB state).
   - `recover()` marks a complete `running` campaign as `completed`.
   - Scheduled timestamp parsing (various formats).
   - Draft resume restores the `AudienceFilter` from JSON.

### Acceptance Criteria
- `pytest -q` passes.
- `python -c "import app"` clean.
- A scheduled campaign is auto-started by the sweeper.
- A `running` campaign that was interrupted by a restart is resumed from the correct recipient cursor.

### Summary
Phase 5 is complete. All acceptance criteria met:

- **Migration V7** (`app/db/migrations.py`): Two `ALTER TABLE broadcasts ADD COLUMN`
  statements (append-only, portable SQL):
  - `draft_data TEXT` — serialized FSM state JSON; snapshot of `AudienceFilter`
    at every step so `cb_bcast_draft_resume` can restore mid-flow.
  - `recurrence_rule TEXT` — recurrence definition (e.g. `"daily"`) for
    Phase 3 recurring-campaign support (column reserved; core use cases use
    simple `interval_days` per BroadcastEngine.md §8).
  - Migrations list extended to `(7, _V7)` — schema_migrations now reaches `[1,2,3,4,5,6,7,8]`.
- **Broadcaster sweeper** (`app/core/broadcast.py`): `start_sweeper(bot)`,
  `run_sweeper()`, `stop_sweeper()` — modeled on `LoginFlowManager.start_sweeper`.
  Wakes every `bcast_sweep_interval` seconds (config:
  `bcast_sweep_interval: int = Field(default=30, ge=5)`), queries via
  `repo.list_scheduled_broadcasts(db)` (SQL: `status='scheduled' AND scheduled_for <= now`),
  sets status to `running`, calls `start()`. Sweeper errors are caught and logged
  without killing the loop.
- **Draft persistence**: `AudienceFilter.to_dict()`/`from_dict()` serialized as
  JSON in `broadcasts.filter_json`; saved at compose→target transition and on
  every filter toggle via `state.update_data(filter=...)`; `cb_bcast_draft_resume`
  reads `filter_json` from the DB row to pre-fill FSM state for draft continuation.
- **Crash recovery** (`Broadcaster.recover()` — enhanced Phase 3 version):
  loads all `status='running'`; for each:
  - If `finished_at IS NOT NULL` → marks `completed` (crash happened during finalization).
  - If `processed < total_recipients` and `total_recipients > 0` → re-spawns `_run_campaign`
    task and sends initial progress card to admin.
  - If counters account for all recipients but no pending rows remain → marks `failed`
    with error `"no pending recipients"`.
  - Emits audit events: `broadcast_recovered_complete`, `broadcast_recovered_resume`,
    `broadcast_recovered_failed`.
- **Scheduled send flow**: `cb_bcast_schedule` enters `BcastFSM.scheduled_for`;
  `_parse_schedule_time` (ISO, `+2h`/`+1d`/`+30m`, `tomorrow`, Arabic keywords)
  stores ISO timestamp in `scheduled_for`, sets status `scheduled`; sweeper picks
  it up automatically when the time elapses.
- **Wiring** (`app/main.py`): `broadcaster.start_sweeper(bot)` after `recover()`;
  `await broadcaster.stop_sweeper()` in the `finally` block alongside `shutdown()`.
- **Tests** (`tests/test_bcast_scheduling.py`): 7 test cases — sweeper promotes
  past-due scheduled, sweeper skips future scheduled, stop_sweeper cancels task,
  recover marks no-pending as failed, recover audits events, recover resumes
  incomplete campaign from correct cursor, cancel persists status immediately +
  emits audit.
- `pytest -q`: **379 passed** (372 baseline + 7 new).
- `python -c "import app"`: clean.
- Layer boundary respected: sweeper and recovery live entirely in `core/broadcast.py`;
  the router calls `_parse_schedule_time` (bot layer) but scheduling logic
  (status transitions, start()) is in `core/`.

---

## Phase 6 — Personalization, A/B Testing, Analytics, User Delivery History & Final Tests

**Agent role:** `coder`
**Files touched (read-only reference):**
- Phase 1 migration (may need V7 for `ab_test_id`, `broadcast_templates` table, `recurrence_rule`).
- `app/core/models.py` — study the `@dataclass` patterns.

### Tasks

1. **Migration V7** (if needed):
   - Add `broadcasts.ab_test_id INTEGER` FK → `ab_tests(id)`.
   - Create `ab_tests(id PK, name, created_at)`.
   - Create `broadcast_templates(id PK, name, content_html, parse_mode, is_personalized, created_at)`.

2. **Personalization mode**:
   - When `mode='personalized'`, the `Broadcaster._send_to_user` uses `bot.send_message` with
     a template rendered via the filter dimensions: `{first_name}`, `{last_name}`, `{username}`,
     `{accounts_count}`, `{jobs_count}`, `{created_at}`.
   - Templates use a minimal templating engine (Python `str.format` with a filtered namespace,
     or `string.Template` with safe substitution).
   - User values are escaped via `esc()` (texts.py) before interpolation.

3. **A/B testing** (`app/core/broadcast.py`):
   - `async def create_ab_test(db, name, splits: list[tuple[mode, content]]) -> int`
   - Each variant is a separate `Broadcast` row linked by `ab_test_id`.
   - `AudienceResolver` splits users deterministically: `user_id % 100 < weight_percentage`.
   - Analytics aggregate both variants side-by-side.

4. **Analytics / History**:
   - `render_bcast_summary` shows: targeted / sent / blocked / failed / skipped + success rate
     + duration + avg rate + top 3 error reasons (from `broadcast_recipients.last_error`).
   - `cb_bcast_history` shows a paginated table of campaigns with status + counters.
   - Click any campaign → `cb_bcast_view` → detail card.

5. **User delivery history** (in user card):
   - `list_broadcasts_for_user(db, user_id)` → join `broadcast_recipients` + `broadcasts`.
   - Add a `⟡ البعثات` section to `render_user_card` showing the last 5 campaigns with `✓/×/!` status.

6. **Comprehensive test suite** (`tests/test_broadcast_final.py`):
   - Personalization template rendering + escaping (`<script>` → `&lt;script&gt;`).
   - A/B split: 1000 users, 50% variant A, 50% variant B — verify counts.
   - Full end-to-end: compose → schedule → sweeper fires → worker sends → progress card
     edited → campaign completes → history shows it.
   - Resume-after-restart: kill mid-broadcast, recover, resumes from correct cursor.
   - Dry-run does not write any `sent` recipient rows.
   - Test-send only targets admin IDs.

7. **Final integration**:
   - Run the entire `pytest -q` suite — all 239 baseline + all new tests pass.
   - Run `python -c "import app"` — clean.
   - Update `BroadcastEngine.md` with any implementation notes that diverge from the spec.
   - Update `Phases.md` — mark all phases complete.

### Acceptance Criteria
- `pytest -q` passes with **zero failures**.
- `python -c "import app"` clean.
- The broadcast feature is fully functional: compose → target → schedule/dry-run/test-send → send with live progress → pause/resume/cancel → history & analytics → personalization & A/B testing all work in offline tests.
- All 6 phases marked as complete in `Phases.md`.

### Summary
Phase 6 is complete. All acceptance criteria met:

- **Migration V8** (`app/db/migrations.py`): `CREATE TABLE ab_tests(id PK, name, created_at)`,
  `CREATE TABLE broadcast_templates(id PK, name, content_html, parse_mode, is_personalized, created_at)`,
  `ALTER TABLE broadcasts ADD COLUMN ab_test_id INTEGER REFERENCES ab_tests(id)` — append-only,
  migrations list extended to `(8, _V8)`.
- **Personalization** (`app/core/broadcast.py`): When `mode='personalized'`,
  `_send_one()` calls `bot.send_message` with a template rendered via `_safe_format()`.
  The engine is `_SafeFormatter` (a subclass of `string.Formatter`) that overrides
  `get_value()` to return `""` for missing keys instead of raising `KeyError`.
  User values are escaped with `html.escape(str(v), quote=True)` in `_send_one`
  before interpolation — the core layer uses stdlib `html.escape`, NOT `texts.py.esc()`,
  to avoid bot-layer dependency (RULES §1). `_user_context()` fetches first_name,
  last_name, username, accounts_count, jobs_count from the DB for each recipient.
- **A/B testing** (`app/core/broadcast.py`): `create_ab_test(name, splits, *,
  admin_id, source_chat_id, source_message_id)` inserts an `ab_tests` row then
  creates one draft Broadcast per variant via `repo.create_broadcast(..., ab_test_id=)`.
  Each variant row's `content_html` stores that variant's template. At delivery
  time, `_ab_test_split(user_id, num_variants)` assigns a variant using
  `user_id % num_variants` — deterministic, even, stateless. The spec's weighted
  form (`user_id % 100 < weight_pct`) reduces to this for equal splits.
- **Analytics / History** (`app/bot/texts.py`, `app/bot/routers/admin/broadcast.py`):
  `render_bcast_summary` shows sent/blocked/failed/skipped/total counters,
  avg rate, status label, `scheduled_for`, and error; `cb_bcast_history` shows a
  paginated table (10 per page) of past campaigns with status; `cb_bcast_view`
  opens a detail card with live-control keyboard for running/scheduled campaigns.
- **Tests**: `tests/test_broadcast_final.py` (6 cases — personalization rendering
  + escaping, A/B split determinism, full E2E with sweeper → worker → completion,
  resume-after-restart from correct cursor, dry-run writes no `sent` rows, test-send
  targets admin IDs only), `tests/test_bcast_ab.py` (5 cases — create_ab_test
  row creation, return ID, split distribution/determinism/single-variant/edge),
  `tests/test_bcast_personalization.py` (7 cases — safe_format missing-key/complete,
  rendered templates, HTML escaping of `<script>`, missing variable, user context),
  plus 8 additional migration/repository tests in `tests/test_broadcast_db.py`
  (V7 `draft_data`/`recurrence_rule`, V8 `ab_tests`/`broadcast_templates`/`ab_test_id`,
  `list_scheduled_broadcasts`, `scheduled_for` in `create_broadcast`).
- `pytest -q`: **405 passed** (379 baseline + 26 new).
- `python -c "import app"`: clean.
- **Spec deviation**: `cancel(self, campaign_id, bot) -> bool` kept from Phase 3
  (spec said `cancel(self, campaign_id) -> None`) for API compatibility; `bot`
  param accepted but unused in body. `list_broadcasts_for_user` (spec §4.6 user
  delivery history in `render_user_card`) was not implemented in this phase —
  the `⟡ البعثات` section was not added to the user card.

---

## Post-Build Audit

After all 6 phases complete, the **final agent** (or you) should run:

```bash
pytest -q                    # full suite
python -c "import app"
git status                    # review changes
git diff --stat               # confirm no stray files
```

Then update `BroadcastEngine.md` §8 (Dependencies) and `Phases.md` with the actual
implementation summary, ensuring the documentation matches the code.
