# PRD — Telegram Member Transfer Bot (TMT)

Status: Approved for implementation
Date: 2026-09-06
Owner: project

---

## 1. Problem definition

A Telegram user wants to move members between two Telegram groups (a *source* and a
*destination*) using one of their own Telegram **user accounts** (MTProto / Telethon),
driven from a comfortable **bot** interface (Bot API / aiogram).

The two Telegram APIs have fundamentally different roles:

| | Bot API (aiogram) | User API (Telethon) |
|---|---|---|
| Identity | the bot | the user's personal account |
| Can do | chat, buttons, state | read participants, invite members |
| Lifetime | stateless request/response | long-lived authenticated session |
| Failure modes | network, webhooks | FloodWait, session revoked, privacy limits |

The system must bridge the two without letting either leak into the other, manage the
full lifecycle of user-account sessions as sensitive credentials, and run member
transfer as a managed, cancellable, restart-safe background job — all while treating
Telegram's undocumented and runtime-observed limits as first-class facts, never
assumptions.

## 2. Goals

1. Add and remove personal Telegram accounts through an interactive, fully cancellable
   login flow (phone → code → optional 2FA password), with every Telegram login
   failure mode handled explicitly.
2. Store Telethon sessions as **encrypted strings inside SQLite** (Fernet), never on
   disk in plain form, never in logs, never in Git.
3. Resolve source/destination groups from any Telegram reference form
   (`@username`, `t.me/...`, invite links, numeric IDs) with one unified resolver, and
   honestly report what cannot be verified as `UNKNOWN`.
4. Run a deep **preflight** before any transfer: entity, type, membership, permission,
   restriction and connectivity checks — each with PASS / WARN / FAIL / UNKNOWN / INFO
   status and a clear Arabic explanation.
5. Execute transfers as **Jobs** with persisted lifecycle state, progress reporting,
   cooperative cancellation, FloodWait handling, per-user skip accounting, and
   crash recovery on restart.
6. Provide a complete Arabic-language UI (HTML formatting, inline keyboards, no emoji,
   no text-art) where users never need to know Telethon or MTProto exist.
7. Keep user isolation absolute: a bot user can only ever touch their own accounts and
   jobs.
8. Make the future steps cheap and non-breaking: PostgreSQL migration, multiple
   workers, admin panel.

## 3. Non-goals

- No admin panel now (data model and services only need to *permit* it later).
- No multi-worker execution now (but jobs must be worker-extractable).
- No broadcast channels as source/destination — groups and supergroups only.
- No member scraping/export features beyond what transfer needs.
- No scheduled/recurring transfers.
- No proxy configuration per account (may be added later in config).
- No PostgreSQL, Redis, Docker in v1 — SQLite only, single process.
- No official-api (bot) invitations — Bot API cannot add members; User API only.

## 4. User journeys

### J1 — Add an account
1. User opens bot → main menu → "الحسابات".
2. User taps "إضافة حساب" → bot asks for phone in international format.
3. User sends phone → bot sends Telegram code to the user's Telegram app → user sends
   the code.
4. If 2FA enabled → bot asks for the password → user sends it.
5. Bot validates, encrypts the session, stores the account, shows the account card.
   Every step has cancel; the flow expires after inactivity; typed code/password are
   deleted from the chat (user-visible input is removed where possible) and never
   persisted.

### J2 — Run a transfer
1. Main menu → "النقل" → choose account → send source group reference → send
   destination reference.
2. Bot runs the resolver, shows resolved titles, runs deep preflight, prints the
   inspection report (✓ / ! / × lines with Arabic explanations).
3. User confirms → job is created, queued, starts running. Bot shows a live progress
   card (done / total, success / skipped / failed, current wait if FloodWait).
4. User can cancel at any time → engine stops between invites, job becomes
   `cancelled` with progress preserved.
5. On completion the bot posts the final report; per-user skip reasons are summarized.

### J3 — Recover after restart
Server restarted mid-job → on boot the system marks all `running`/`queued` jobs as
`interrupted` (progress preserved) and tells the user the job can be re-run. Nothing
auto-resumes (re-running is safe and idempotent because already-invited members are
skipped by the destination-member set).

### J4 — Account dies mid-job
Session revoked / client drops → engine classifies the error, fails the job with an
Arabic explanation, marks the account `unauthorized` (requires re-login) and releases
it for other jobs.

## 5. Functional requirements

- FR1 Register/list/remove accounts per §5-flows above; one row per (bot user,
  telegram account); re-adding the same TG account for the same bot user replaces the
  session.
- FR2 Unified entity resolution accepting: `@name`, bare `name`,
  `https://t.me/name`, `t.me/name`, `https://t.me/+hash`, `t.me/joinchat/hash`,
  numeric `-100…` / `-…` IDs. Numeric IDs only resolve if the account already has the
  entity in its session cache; otherwise report "cannot resolve" honestly.
- FR3 Deep preflight (source, destination, account) per §8 of the master prompt with
  the result model of §10.
- FR4 Transfer job: fetch source members in batches, skip bots and already-present
  members, invite via the correct API per destination type
  (`channels.InviteToChannelRequest` for supergroups, `messages.AddChatUserRequest`
  for basic chats), classify per-invite errors, keep per-reason counters.
- FR5 Job controls: start (only after preflight), cancel, refresh view, re-run after
  failure/interruption.
- FR6 Live progress: throttled edits (≥ 4 s apart) of one progress message; final
  summary message.
- FR7 Limits (configurable): max concurrent jobs per process, max running jobs per
  user (default 1), max members per job (default 2000), inter-invite delay with
  jitter, FloodWait auto-wait cap (default 900 s), overall job timeout.
- FR8 Account health: `active` / `unauthorized` / `limited` (cooldown after
  PeerFlood) states; unauthorized accounts can re-login (re-add flow).
- FR9 All user-visible text in Arabic, HTML-formatted; secrets never echoed.

## 6. Non-functional requirements

- NFR1 Production-quality async Python 3.11+ code, fully type-hinted, no blocking
  calls on the event loop.
- NFR2 Tests run offline (fakes for Telethon and aiogram objects); no test touches
  Telegram production.
- NFR3 Logs: human-readable, level-configurable; secret redaction filter as a second
  line of defense; no secrets/phone codes/passwords/session strings in any sink.
- NFR4 SQLite in WAL mode, foreign keys on, every multi-row write transactional.
- NFR5 Startup < 2 s; graceful shutdown cancels job tasks and closes clients/DB.
- NFR6 One command to run: `python -m app`.

## 7. Security requirements & design

**Threat model.** Adversary goals: read session strings (full account takeover),
read login codes/passwords, access another user's accounts/jobs, or crash/abuse the
bot. Honest scope: an attacker with **root on the host** can read the DB *and* the
master key; encryption-at-rest defends against DB-file-only compromise (leaked
backup, lost device, accidental `.env`-less copy), not root. This is documented, not
hidden.

Decisions:

1. **Session storage** — Telethon `StringSession` encrypted with Fernet
   (AES-128-CBC + HMAC, `cryptography` lib), stored in the `accounts` table.
   StringSession was chosen over session *files* because: one atomic row per account
   (no file lifecycle, no path traversal surface, no per-file chmod races), trivially
   portable to a future server, and no stale `.session` files to leak in backups.
   Plaintext strings exist only in process memory, bound to a live client.
2. **Key management** — master key from `SESSIONS_MASTER_KEY` env var, or
   auto-generated into `data/keys/master.key` (created `0600`, directory `0700`) on
   first boot. Rotation: `app.security.crypto` exposes re-encrypt; rotation is manual
   (documented in README). Losing the key = losing sessions = re-login (acceptable,
   documented). Backup implication: DB without key is useless to the thief.
3. **Transient login data** — phone, code, 2FA password, `phone_code_hash` and the
   not-yet-authorized client live **only in memory** (`LoginFlowManager`) with a
   hard TTL (10 min) and immediate wipe on success/cancel/timeout; a background
   sweeper closes and discards expired clients. Never written to DB, never logged.
4. **Log hygiene** — a logging filter redacts anything shaped like a session string
   or long token; plus the rule (RULES.md) that secrets are never passed to a logger.
   Error messages shown to users are static Arabic strings; technical detail goes to
   logs only.
5. **User isolation** — every repository query is owner-scoped; every callback is
   verified (callback data embeds entity ids, handlers re-check ownership in the
   service layer); ownership is never taken from message text. Inline keyboards shown
   to other users in groups are inert (the bot is intended for private chats; group
   use is rejected at the gate except `/start`).
6. **Git hygiene** — `.env`, `data/`, `*.session` gitignored from the first commit;
   only `.env.example` is tracked; no real credentials anywhere in the repo.
7. **Filesystem** — everything mutable lives under `data/` (`0700`), DB and key
   inside it.

## 8. Telegram limitations (facts we design around)

- **FloodWait**: server-mandated sleeps with explicit seconds; invites are heavily
  limited (commonly dozens per day per account — *not documented, must be observed at
  runtime*).
- **PeerFloodError**: the account is temporarily banned from invites — treat as a
  hard stop, put the account in `limited` cooldown, never auto-retry.
- **USER_NOT_MUTUAL_CONTACT / USER_PRIVACY_RESTRICTED**: the single most common
  per-member skip reason; not an error of the job.
- Participant access: supergroups cap fetchable participants (~10k via offsets) and
  may **hide member lists entirely** (fetch returns 0 despite non-zero count) —
  preflight must detect and report this as UNKNOWN, not as success.
- Numeric IDs of unseen entities cannot be resolved via `get_entity` without the
  entity being in the session's access-hash cache.
- Invite permission is only *provably* verifiable when the account is admin of the
  destination (readable `admin_rights.invite_users`); for plain members the API
  offers no reliable check → preflight reports UNKNOWN, the first invite attempt is
  the actual test.
- Bots cannot be invited to groups by users in most cases → they are skipped with an
  informational counter.
- `AddChatUserRequest` (basic chats) has a small fwd_limit parameter and different
  error surface than `InviteToChannelRequest`.

Every one of the above is handled from **runtime API behavior**, and anything we
cannot verify reliably is surfaced as `UNKNOWN` — never assumed.

## 9. Failure scenarios → system behavior

| Scenario | Behavior |
|---|---|
| User abandons login mid-flow | TTL sweeper disconnects & discards temp client; FSM state expires |
| Invalid / expired code | Arabic message; retry allowed within TTL; resend offered (FloodWait-aware) |
| 2FA wrong password | Arabic message; retry; `PasswordHashInvalidError` mapped explicitly |
| Session revoked later | account → `unauthorized`; running job fails; user offered re-add |
| FloodWait during invite | if ≤ cap: wait once (cancellable), update status_detail; else job fails with clear Arabic message |
| PeerFlood | job fails; account → `limited` with cooldown; no retries |
| Network drop mid-job | Telethon auto-reconnects; transient errors retried per classification; job continues or fails per policy |
| Server restart mid-job | boot recovery marks `running`/`queued` → `interrupted`; user can re-run |
| Double-tap on a button | callback answers are idempotent; job creation is guarded by per-user state machine and DB constraints |
| Two jobs same account | prevented: per-account exclusive running job (in-memory lock + DB check) |
| Permission change mid-job | per-invite errors classified; fatal ones (admin required) abort job with explanation |
| Account deleted while in use | deletion refused while a running job uses the account; cancel offered |
| Telethon client died | pool detects dead connection on next use, reconnects once, else fails the job |
| DB write fails | transaction rollback; job marked failed; error logged, user told generically |

## 10. State transitions

**Account**: `active ⇄ unauthorized`, `active → limited → active (after cooldown)`,
`any → deleted` (blocked while running jobs exist).

**Job** (final states in **bold**):

```
created → validating → queued → running → completed
                              │   └────→ failed
                              │   └────→ cancelled
                              └────────→ cancelled  (cancel while queued)
validating/queued/running → interrupted (boot recovery)
failed/interrupted → (re-run creates a new job)
```

`running` carries `status_detail`: `fetching_members | inviting | waiting_flood:<s>`.
All transitions are guarded compare-and-set updates in SQL (`WHERE status IN (...)`),
so double-finalization is impossible.

**Login flow (in-memory)**: `idle → phone_sent → awaiting_code → awaiting_password → done`
with TTL expiry at every step and cancel from everywhere.

## 11. Data model

SQLite (WAL). All timestamps `TEXT` ISO-8601 UTC. All ids `INTEGER PRIMARY KEY`
(rowid-alias; portable to PG `BIGSERIAL`). Foreign keys ON.

```
users            id (=TG user id) PK, created_at, updated_at, is_blocked INT 0
accounts         id PK, owner_id FK→users, phone TEXT, tg_user_id INT,
                 tg_username TEXT, display_name TEXT,
                 session_encrypted TEXT, status TEXT,
                 limited_until TEXT NULL, added_at, last_validated_at NULL,
                 UNIQUE(owner_id, tg_user_id)
jobs             id PK, owner_id FK→users, account_id FK→accounts,
                 source_ref TEXT, dest_ref TEXT,
                 source_title TEXT, dest_title TEXT,
                 status TEXT, status_detail TEXT NULL,
                 total INT 0, invited INT 0, skipped INT 0, failed INT 0,
                 skip_reasons TEXT (JSON), error TEXT NULL,
                 created_at, started_at NULL, finished_at NULL,
                 cancel_requested INT 0
audit_log        id PK, ts, owner_id NULL, account_id NULL, job_id NULL,
                 event TEXT, detail TEXT (JSON)
schema_migrations version INT PK, applied_at
```

Indexes: `accounts(owner_id)`, `jobs(owner_id, status)`, `jobs(account_id, status)`,
`audit_log(ts)`.

Login-flow transient state is **not** in the DB (see §7.3).

## 12. Architecture decision

### Options considered

**A. Single-process layered monolith with clean seams** *(chosen)*
One asyncio process. Layers: `bot` (aiogram presentation) → `core` (domain services:
account service, job manager) → `tg` (Telethon: client pool, resolver, preflight,
transfer engine) → `db` (repositories). Cross-layer communication only via narrow
service calls; job progress flows engine → in-process event bus → bot-side reporter.

**B. Bot process + separate worker process with a queue (Redis / SQLite-as-queue)**
Isolates heavy work and scales horizontally, but: adds an infra dependency
contradicting "SQLite for now"; SQLite as a queue is locking-fragile (single writer);
two processes fighting over the same SQLite file adds busy-timeout latency to the
interactive bot; serialization boundary costs real engineering for zero present
benefit. Verdict: **premature** — but chosen seams make it a move, not a rewrite:
jobs are DB rows (source of truth), the engine is a pure async class with no aiogram
imports, and `JobManager` is the single place that turns rows into running tasks.

**C. Event-sourced / plugin microservices**
Massive over-engineering for one bot. Rejected without further discussion.

### Why A wins here
The system's real complexity is in *Telegram semantics* (sessions, FloodWait,
preflight honesty), not in distribution. A keeps deployment trivial, makes testing
cheap (fakes at two boundaries: Telethon and aiogram), and every future step the
master prompt demands (PostgreSQL, workers, admin panel) is an *extraction along an
existing seam*, not a redesign. Abstractions exist only where a second
implementation is plausible: crypto wrapper, repository module, event bus (3 small
modules), error classifier. No DI framework, no generic "Service/Manager/Factory"
ceremony.

### The seams that keep B/C cheap later
1. All job state in DB; nothing job-related lives only in RAM (except the task
   handle).
2. `JobManager.start/cancel` is the only API by which jobs begin; replacing its
   internals with a queue consumer does not touch handlers or engine.
3. `tg.transfer.TransferEngine` receives abstract client-callable interfaces —
   testable with fakes, runnable by any future worker.
4. Repositories are plain async functions over SQL — PG migration touches one module
   plus migration SQL, nothing else.
5. `audit_log` exists from day one — admin panel's data source.

## 13. Component responsibilities

```
app/
  config.py            pydantic-settings; validates env; no os.environ reads elsewhere
  logging_setup.py     logging config + secret-redaction filter
  db/
    database.py        aiosqlite wrapper: WAL, FK, tx helper, migration runner
    migrations.py      ordered SQL migrations (v1 schema)
    repositories.py    owner-scoped CRUD for users/accounts/jobs/audit
  security/crypto.py   Fernet: key load/generate, encrypt/decrypt, re-encrypt-all
  tg/                  ALL Telethon knowledge lives here
    errors.py          RPC/exception classification → domain kinds + Arabic messages
    resolver.py        unified entity resolution (username/link/invite-hash/id)
    client_pool.py     owns live clients; connect/disconnect/get; per-account lock
    login.py           LoginFlowManager: temp clients, phone/code/2FA steps, TTL sweep
    preflight.py       source/dest/account checks → CheckResult list
    transfer.py        TransferEngine: fetch→filter→invite loop, events, cancel
  core/
    models.py          dataclasses + enums (domain vocabulary)
    events.py          async pub/sub for job progress
    account_service.py add/remove/list accounts; bridges repos + crypto + pool
    job_manager.py     job lifecycle, queue, concurrency limits, cancel, recovery
  bot/                 ALL aiogram knowledge lives here
    texts.py           Arabic UI strings (HTML)
    keyboards.py       inline keyboard builders
    callbacks.py       CallbackData factories
    states.py          FSM states
    middlewares.py     user upsert/block check, context injection
    reporter.py        job events → throttled progress messages
    routers/           common, accounts, transfers, jobs handlers (thin)
  __main__.py          composition root: config → db → crypto → pool → engine → bot
```

Rules of engagement: handlers parse input and call services; services own decisions;
`tg` never imports `bot` or `db` (engine gets repos via callables? No — engine gets
what it needs as parameters; job manager owns DB writes); `db` imports nothing above
it.

## 14. Concurrency model

- Single asyncio loop. Bot polling + job tasks + TTL sweeper coexist.
- **Capacity**: semaphore `MAX_CONCURRENT_JOBS` (default 3) process-wide; per-user
  running-job cap (default 1) enforced in `JobManager` before dispatch.
- **Account exclusivity**: one running job per account. In-memory `asyncio.Lock` per
  account id + DB status check; the DB check is authoritative on recovery.
- **Client ownership**: `ClientPool` is the only creator/holder/closer of Telethon
  clients. Lazy connect, idempotent; per-account connect lock prevents double-login.
- **Cancellation**: cooperative `cancel_requested` flag + asyncio task cancel as
  backstop; engine checks between every invite and inside waits; finalization is
  exactly-once via guarded SQL.
- **Restart**: boot recovery marks orphans `interrupted` before any new job starts.
- **UI writes**: progress edits throttled ≥ 4 s; never concurrent edits of one
  message (single reporter task per job).

## 15. Job lifecycle

```
submit(preflight-passed params)
  → row(status=created) → validating (re-check account free/authorized)
  → queued → dispatch when capacity → running
  → engine loop: [fetch dest member set] → [fetch source batch] → invite per member
      per member: skip reasons → counters; FloodWait → cancellable wait; fatal → fail
  → final report event → completed/failed/cancelled (exactly-once finalize)
progress events emitted at: start, per-batch, flood-wait, per-invite-result (aggregated), end
```

## 16. Error strategy

`tg/errors.py` is the single mapping table:

| Kind | Retry? | User policy |
|---|---|---|
| FloodWait(small) | wait once ≤ cap | info line, continue |
| FloodWait(large) | no | fail job, clear Arabic message |
| PeerFlood | no | fail job, account limited |
| ChatAdminRequired / ChannelPrivate | no | fail with explanation |
| UserPrivacy / NotMutualContact | n/a | per-member skip counter |
| AuthKeyUnregistered / SessionRevoked / UserDeactivated | no | fail job, account unauthorized |
| Timeout / ConnectionError (transient) | bounded retries per item, then fail | info |
| Anything unexpected | no | fail job, generic Arabic message, full detail to log |

Raw exception text is never shown to users (§10 of master prompt). Every check in
preflight and every skip in the engine carries a *reason*.

## 17. Testing strategy

- pytest + pytest-asyncio, offline only. In-memory SQLite, real migrations run per
  test (fast, and they *are* a feature under test).
- **Fakes**: `FakeTelethonClient` implementing the exact surface `tg` uses
  (resolve/get_entity, get_participants/iter, invite, permissions, connect) with
  scriptable behaviors (raise FloodWait, privacy errors…). aiogram objects faked at
  the reporter/handler boundary (simple stand-in objects for message/callback).
- Coverage targets from the master prompt: crypto roundtrip + tamper, migrations,
  repo constraints + owner isolation, resolver parsing matrix, preflight PASS/WARN/
  FAIL/UNKNOWN paths, error classification table, job state machine (legal + illegal
  transitions, exactly-once finalize), cancellation between invites, FloodWait path,
  boot recovery, callback-data ownership.

## 18. Deployment considerations

- Linux box, systemd unit `Restart=on-failure`, `WorkingDirectory` with `data/`
  writable, `EnvironmentFile=.env`. Python 3.11+. `python -m app`.
- On boot: migrations → recovery → clients stay lazy (connected on first use).
- Config: `.env` (BOT_TOKEN, API_ID, API_HASH required; everything else has sane
  defaults documented in `.env.example`).

## 19. Scalability plan

1. **PostgreSQL**: swap `db/repositories.py` SQL + migration dialect. No business
   code changes (no SQLite-isms used: no `AUTOINCREMENT`, no `PRAGMA` in queries,
   ISO timestamps).
2. **Workers**: extract `JobManager` dispatch to a queue consumer; engine untouched
   (already side-effect-parameterized); sessions already DB-stored (any worker can
   decrypt with the shared key).
3. **Scale-up of limits**: all knobs are config; per-account exclusivity is already
   the hard constraint that keeps FloodWait predictable.
4. **Admin panel**: repos already expose stats-shaped queries; add admin-only router
   + `ADMIN_IDS`; `audit_log` is the feed. No blocking design debt anticipated.

## 20. Future Admin Panel considerations (data it will need)

users (count, blocked, activity), accounts (by status, limited cooldowns), jobs
(throughput, failure reasons histogram, avg duration), system health (queue depth,
client pool state, FloodWait log), audit trail, per-user limits for moderation.
All derivable from current tables + `audit_log`; nothing to retrofit.

## 21. Open questions / assumptions

- A1 Bot usage is assumed to be in **private chat**; group messages are ignored (by
  design, simplifies isolation). May become a config later.
- A2 The operator supplies API_ID/API_HASH (from my.telegram.org) and BOT_TOKEN via
  `.env`; no onboarding UI for these.
- A3 Source-group member visibility: if the source hides members, transfer is
  impossible; preflight reports UNKNOWN and the wizard blocks confirm (WARN + block).
- A4 Max members per job defaults to 2000 (safety rail, configurable); larger
  transfers should be split — Telegram's invite limits will dominate anyway.
- A5 aiogram MemoryStorage accepted for FSM (restarts lose in-dialog state; login
  flows are TTL-bound anyway). A persistent FSM storage is a future swap, not a
  redesign.
- A6 One Telegram account may be added by two different bot users (two sessions,
  two rows) — isolation-first choice; not deduplicated.

---

## 22. Implementation plan (stages)

1. Scaffold (pyproject, .env.example, .gitignore, config, logging).
2. DB layer + crypto + models.
3. `tg` layer: errors → resolver → client pool → login → preflight → transfer.
4. `core`: events, account service, job manager.
5. `bot`: texts/keyboards/callbacks/states → middlewares → routers → reporter.
6. Composition root + README.
7. Tests per §17; run, fix, repeat.
8. Security review (§27 of master prompt) + final review; git hygiene check; one
   commit.
