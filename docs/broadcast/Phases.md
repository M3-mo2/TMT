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
- `test_database.py` updated: migration version assertions now expect `[1,2,3,4,5]`.
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
<!-- Phase 2 agent fills this in on completion -->

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
<!-- Phase 3 agent fills this in on completion -->

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
<!-- Phase 4 agent fills this in on completion -->

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
<!-- Phase 5 agent fills this in on completion -->

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
<!-- Phase 6 agent fills this in on completion -->

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
