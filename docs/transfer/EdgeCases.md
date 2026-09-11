# Member Transfer — Edge Cases Report

This document catalogs every identifiable edge case across the member transfer
pipeline: entity resolution, preflight inspection, job lifecycle, transfer
engine execution, progress reporting, and finalization. Each section maps to
the source file(s) responsible and the user-visible outcome.

---

## Table of Contents

1. [Entity Resolution Edge Cases](#1-entity-resolution-edge-cases)
2. [Preflight Inspection Edge Cases](#2-preflight-inspection-edge-cases)
3. [Job Lifecycle Edge Cases](#3-job-lifecycle-edge-cases)
4. [Transfer Engine Edge Cases](#4-transfer-engine-edge-cases)
5. [Client Pool Edge Cases](#5-client-pool-edge-cases)
6. [Progress Reporting Edge Cases](#6-progress-reporting-edge-cases)
7. [Database & Finalization Edge Cases](#7-database--finalization-edge-cases)
8. [Configuration & Settings Edge Cases](#8-configuration--settings-edge-cases)
9. [Session & Security Edge Cases](#9-session--security-edge-cases)
10. [Bot Wizard / FSM Edge Cases](#10-bot-wizard--fsm-edge-cases)

---

## 1. Entity Resolution Edge Cases

Source: `app/tg/resolver.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| R-01 | Empty or whitespace-only input | User sends `""` or `"   "` | Raises `ResolutionError("invalid")` | Wizard stays on current step; user sees Arabic error |
| R-02 | Malformed URL input | User sends `"https://evil.com/group"` | Raises `ResolutionError("invalid")` — regex does not match | Safe rejection |
| R-03 | Unknown username (`@ghost`) | `get_entity` raises `ValueError` | Raises `ResolutionError("not_found")` | Wizard stays; user prompted to retry |
| R-04 | Private/inaccessible channel | `get_entity` raises `ChannelPrivateError` | Raises `ResolutionError("no_access")` | User cannot transfer to/from inaccessible groups |
| R-05 | Expired invite hash | `CheckChatInviteRequest` raises `InviteHashExpiredError` | Raises `ResolutionError("invalid")` | User must obtain a fresh invite link |
| R-06 | Invalid invite hash | `CheckChatInviteRequest` raises `InviteHashInvalidError` | Raises `ResolutionError("invalid")` | Same as expired |
| R-07 | Invite preview (not yet joined) | `ChatInvite` returned (not `ChatInviteAlready`) | Returns `ResolvedEntity(id=0, is_member=False, via_invite_link=True)` | **Critical**: `id=0` means the entity cannot be addressed by the client; preflight catches this for dest membership |
| R-08 | Numeric ID for unknown entity | `get_entity` raises `ValueError` | Raises `ResolutionError("unresolvable_numeric")` — Arabic explains the limitation | User is guided to use username/link instead |
| R-09 | FloodWait during resolution | `get_entity` raises `FloodWaitError` | Maps to `ResolutionError("unresolvable")` — not classified as FLOOD_WAIT | Resolution does not retry; user must wait and retry manually |
| R-10 | Broadcast channel as source/dest | `Channel` with `broadcast=True, megagroup=False` | `kind="channel"`, `is_groupish=False` | Rejected by preflight `supported_type` check and by `JobManager.create_job` |
| R-11 | User entity as source/dest | `User` returned by `get_entity` | `kind="user"`, `is_groupish=False` | Rejected by preflight `supported_type` check |
| R-12 | Basic chat (non-megagroup) | `Chat` entity | `kind="chat"`, `is_groupish=True` | Allowed — uses `AddChatUserRequest` instead of `InviteToChannelRequest` |
| R-13 | Members count unavailable | `GetFullChannelRequest` fails with RPC/network error | `members_count=None` propagated silently | Preflight may report WARN for over-cap; no failure |
| R-14 | Unicode/special chars in input | Input like `"@name"` with invisible Unicode | Regex may or may not match | If regex matches, Telethon's `get_entity` handles the actual lookup; no crash expected |

---

## 2. Preflight Inspection Edge Cases

Source: `app/tg/preflight.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| P-01 | Same group for source and dest | `source.id == dest.id` | `dest_diff = FAIL` | Job creation blocked; user told groups must differ |
| P-02 | Both IDs are 0 (invite previews) | Both source and dest are invite previews not yet joined | `source.id == dest.id == 0` → `dest_diff = PASS` (false positive: 0==0 should fail) | **Bug**: two invite previews with id=0 would pass the same-group check; however, they can never be used as source because membership check requires `is_member=True` for invite-only groups |
| P-03 | Dest is invite preview with `is_member=False` | User enters a `t.me/+hash` link they haven't joined | `dest_member = FAIL` — explicit check on `via_invite_link and is_member is False` | User must join the dest group first |
| P-04 | Source is invite preview with `is_member=False` | User enters an invite link for a group they haven't joined | `source_member = FAIL` (same logic as P-03) | User must join the source group first |
| P-05 | Invite preview with `id=0` and `is_member=True` | `ChatInviteAlready` returned — account is already a member | `dest_diff = PASS` (0 is used for comparison), `source_member = PASS` via `get_permissions` | **Edge**: `id=0` means `_peer_of()` returns `None`, so membership/invite-rights checks degrade to `UNKNOWN` |
| P-06 | Source participants hidden | `iter_participants` returns 0 users, but `members_count > 0` or is `None` | `source_participants = UNKNOWN` — "قائمة الأعضاء غير متاحة قد تكون مخفية" | User is warned; transfer may still proceed with unknown participant list |
| P-07 | Source participants list empty with count=0 | `iter_participants` returns 0, `members_count=0` | `source_participants = PASS` — empty source, nothing to transfer | Silently succeeds; transfer engine will produce 0 invites |
| P-08 | Source member count exceeds `max_members` | `members_count > max_members` | `source_participants = WARN` — user warned only N will be processed | Transfer proceeds but caps at `max_members`; extra members silently skipped |
| P-09 | Dest invite rights unknown (basic chat) | `dest.kind == "chat"` | `dest_invite_rights = UNKNOWN` — cannot pre-verify | User is told the first invite is the real test; may fail at runtime |
| P-10 | Dest invite rights: admin without invite permission | Admin in dest but `invite_users=False` | `dest_invite_rights = FAIL` | Job creation blocked |
| P-11 | Dest invite rights: plain member (not admin) | Account is a regular member of the dest supergroup | `dest_invite_rights = UNKNOWN` — API does not expose invite rights for non-admins | User may proceed; runtime invite may succeed or fail with `ChatAdminRequiredError` |
| P-12 | Account is Telegram-restricted | `get_me()` returns `restricted=True` | `account_restriction = WARN` | Warning only; transfer may still be attempted but operations may fail |
| P-13 | `get_permissions` returns `None` | Edge case in Telegram API | Both membership and invite-rights checks degrade to `UNKNOWN` | Transfer proceeds with degraded confidence |
| P-14 | RPC error during `get_permissions` | Network issue or API error | Check degrades to `UNKNOWN` | Non-blocking; transfer can proceed |
| P-15 | `iter_participants` RPC error during source probe | Network issue during source participant fetch | `source_participants = UNKNOWN` | Non-blocking; user sees warning |
| P-16 | FloodWait during preflight probe | Any probe call triggers `FloodWaitError` | Classified as `_PROBE_ERRORS`; check becomes `UNKNOWN` | Preflight never raises; user proceeds with degraded info |

---

## 3. Job Lifecycle Edge Cases

Source: `app/core/job_manager.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| J-01 | Account not found | `repo.get_account` returns `None` | Raises `ServiceError("الحساب غير موجود.")` | Wizard shows error; job never created |
| J-02 | Account unauthorized (session revoked) | `record.status == UNAUTHORIZED` | Raises `ServiceError("جلسة الحساب غير صالحة")` | User must re-login the account |
| J-03 | Account limited (cooldown active) | `record.status == LIMITED` with future `limited_until` | Raises `ServiceError` with remaining time (e.g., "1 ساعة و 29 دقيقة") | User must wait for cooldown to expire |
| J-04 | Account limited with expired cooldown | `record.status == LIMITED` but `limited_until` is in the past | Job creation proceeds normally | Account effectively treated as active |
| J-05 | Account limited with unparseable `limited_until` | Corrupt or missing timestamp string | `_parse_limited_until` returns `None`; treated as active | **Potential bug**: an account with `status=limited` but no parseable timestamp would be allowed to create a job |
| J-06 | Account busy (another job running) | `account_id in self._active_accounts` or DB count > 0 | Raises `ServiceError("الحساب مشغول الآن")` | User must wait or cancel the running job |
| J-07 | User at capacity | Active job count >= `max_running_jobs_per_user` | Raises `ServiceError("وصلت إلى الحد الأقصى")` | User must wait for existing jobs to finish |
| J-08 | Non-groupish entities | `source.is_groupish or dest.is_groupish` is `False` | Raises `ServiceError("المصدر والهدف يجب أن يكونا مجموعتين مدعومة")` | Prevented at creation; double-checked in preflight |
| J-09 | Race: account lock held during creation | Two concurrent `create_job` calls for same account | `asyncio.Lock` serializes access; second call finds account in `_active_accounts` | Raises `MSG_ACCOUNT_BUSY` |
| J-10 | Job queued → cancelled race | User cancels a job that is transitioning from QUEUED to RUNNING | CAS transition fails for cancellation; `cancel_job` re-reads and falls through to cooperative cancel | Exactly-once finalization via CAS |
| J-11 | Boot recovery: jobs left in-flight | Process restart with jobs in `CREATED/VALIDATING/QUEUED/RUNNING` | `recover()` transitions them all to `INTERRUPTED` | User sees "توقفت العملية بسبب إعادة تشغيل الخادم" |
| J-12 | Session decryption failure during job start | `self._crypto.decrypt(record.session_encrypted)` raises `CryptoError` | Account marked `UNAUTHORIZED`; job fails with `MSG_SESSION_INVALID` | User must re-login |
| J-13 | Preflight fail during job runner | Re-run preflight finds a FAIL check | Job fails immediately with the first FAIL check's Arabic message | Transfer never starts; user informed |
| J-14 | Engine raises unexpected exception | `RuntimeError` or similar from the engine | Caught by generic `except Exception`; job fails with `MSG_UNEXPECTED` | Account slot released; user sees generic error |
| J-15 | `CancelledError` during job runner | `shutdown()` cancels all tasks | Job finalized as `CANCELLED` unless already finalized | Exactly-once finalization prevents double-write |
| J-16 | PeerFlood during transfer | Engine returns `abort_kind=PEER_FLOOD` | Account marked `LIMITED` with `peer_flood_cooldown_seconds` cooldown; job fails | User cannot use the account until cooldown expires |
| J-17 | AUTH_REVOKED during transfer | Engine returns `abort_kind=AUTH_REVOKED` | Account marked `UNAUTHORIZED`; client discarded from pool | User must re-login the account |
| J-18 | AdminRequired during transfer | Engine returns `abort_kind=ADMIN_REQUIRED` | Job fails; account NOT marked fatal (not `account_fatal`) | Job fails but account remains usable for other operations |
| J-19 | EntityPrivate during transfer | Engine returns `abort_kind=ENTITY_PRIVATE` | Job fails; account NOT marked fatal | Account remains usable |
| J-20 | Deadline exceeded during transfer | `loop.time() > params.deadline` | Engine aborts with Arabic message "انتهت المدة المسموحة للعملية" | Job fails; user can retry with adjusted timeout |

---

## 4. Transfer Engine Edge Cases

Source: `app/tg/transfer.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| T-01 | Dest peer unresolvable (`_peer_of` returns `None`) | `dest.id == 0` (invite preview) or `dest.kind` not in supported types | Engine aborts with `INPUT_INVALID` | Job fails immediately |
| T-02 | Source peer unresolvable | Same as T-01 but for source | Engine aborts with `INPUT_INVALID` | Job fails immediately |
| T-03 | Cancel during dest member fetch | `cancel_event.set()` while iterating dest participants | `was_cancelled = True`; returns `None` | Job cancelled; no invites sent |
| T-04 | Cancel during source member fetch | `cancel_event.set()` while iterating source users | Same as T-03 | Job cancelled |
| T-05 | Cancel between invites | `cancel_event.set()` in the invite loop between members | Loop exits; `was_cancelled = True` | Job cancelled; partial invites may have succeeded |
| T-06 | Cancel during FloodWait sleep | `cancel_event.set()` while sleeping in `state.sleep()` | Sleep is sliced (0.2s); cancel detected on next slice | Job cancelled promptly; no further invites |
| T-07 | Network error during dest fetch | `ConnectionError`, `TimeoutError`, `OSError` | Classified as `TRANSIENT`; if `attempt < 2`, retries once | If retry also fails, job aborted |
| T-08 | Network error during source fetch | Same as T-07 | Same retry logic | Same outcome |
| T-09 | FloodWait error during dest iteration | `FloodWaitError` while fetching dest members | Classified as FLOOD_WAIT; if `attempt < 2`, retries after sleep | If wait exceeds `flood_wait_max`, job aborted |
| T-10 | FloodWait error during invite | `FloodWaitError` while inviting a user | Sleeps for `wait_seconds` (if <= `flood_wait_max`); retries invite once | If second attempt also gets FloodWait, counts as `failed` |
| T-11 | FloodWait exceeding cap | `wait_seconds > flood_wait_max` | Job aborted with Arabic message mentioning the wait duration | Account NOT marked fatal; user can retry later |
| T-12 | PeerFlood on invite | `PeerFloodError` | Job aborted; account marked `LIMITED` with cooldown | **Critical**: prevents hammering the account |
| T-13 | UserPrivacyRestrictedError on invite | Privacy settings block the invite | Counts as skip with reason `"privacy"`; no abort | Other users continue being invited |
| T-14 | UserNotMutualContactError on invite | No mutual contact with user | Counts as skip with reason `"not_mutual"` | Other users continue |
| T-15 | UserChannelsTooMuchError on invite | User is in too many channels | Counts as skip with reason `"channels_too_much"` | Other users continue |
| T-16 | UserKickedError on invite | User is banned in the dest group | Counts as skip with reason `"kicked"` | Other users continue |
| T-17 | UserDeactivatedError/UserDeactivatedBanError on invite | Deleted/banned Telegram account | Counts as skip with reason `"deleted_account"` | Other users continue |
| T-18 | ChatAdminRequiredError during invite | Account lacks admin rights in dest | Job aborted (not per-member) | Account NOT marked fatal |
| T-19 | ChannelPrivateError during invite | Dest became private/inaccessible mid-transfer | Job aborted | Account NOT marked fatal |
| T-20 | Transient error on invite (first attempt) | `ConnectionError`, `TimeoutError`, etc. | Retries the same user once | If retry succeeds, user is invited |
| T-21 | Transient error on invite (second attempt) | Same transient error after retry | Counts as `failed` for this user | Other users continue |
| T-22 | Unexpected exception on invite | Exception not in `_EXPECTED_ERRORS` | Re-raised; caught by job runner; job fails generically | Account slot released |
| T-23 | Participant without `id` attribute | `getattr(user, "id", None)` returns `None` | Counts as skip with reason `"other"` | Logged per RULES §5 |
| T-24 | Bot in source list | `getattr(user, "bot", False)` is `True` | Counts as skip with reason `"bot"` | Bots are never invited |
| T-25 | User already in dest | `user_id in dest_ids` | Counts as skip with reason `"already_member"` | Avoids redundant invite attempts |
| T-026 | `max_members` caps source list | Source has more users than `max_members` | Iterator is broken early; only `max_members` users collected | Extra users silently not processed |
| T-027 | Source list empty | No users in source or all filtered | `users = []`; invite loop runs 0 iterations | Job completes with 0 invites |
| T-028 | Invite delay with jitter | Normal operation | Sleep = `invite_delay + random.uniform(0, jitter)` | Jitter adds randomness to avoid burst patterns |
| T-029 | Zero invite delay | `invite_delay=0, invite_jitter=0` | No sleep between invites | May trigger FloodWait faster |
| T-030 | Deadline already passed at loop start | `loop.time() > params.deadline` on first iteration | Abort on first check; 0 invites sent | Job fails with timeout message |
| T-031 | Iterator cleanup on early break | `max_members` cap causes break from `iter_participants` | `_close_iterator()` calls `aclose()` on the iterator | Prevents resource leak |
| T-032 | Iterator close failure | `aclose()` raises network error | Caught by `_EXPECTED_ERRORS`; logged at debug level | Non-critical; cleanup best-effort |
| T-033 | `get_input_entity` failure for user | User entity not cached in the session | Raises `ValueError` or `RPCError`; classified by `_handle_invite_error` | Counts as skip or abort depending on kind |
| T-034 | `get_input_entity` failure for dest | Dest entity not in session | Same as T-033 but for the dest peer | Could abort the job |

---

## 5. Client Pool Edge Cases

Source: `app/tg/client_pool.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| C-01 | Concurrent `get()` for same account | Two jobs on same account (prevented by exclusivity) | Per-account lock serializes; single client returned | Safe due to exclusivity guard |
| C-02 | `get()` after `discard()` | Account marked unauthorized mid-flight | `discard()` disconnects and removes; next `get()` creates fresh client | Caller may get a different client than expected |
| C-03 | Client disconnected mid-operation | Network issue during transfer | `client.is_connected()` check in `get()` reconnects; but mid-operation disconnect is not retried at pool level | Transfer engine classifies as `TRANSIENT`; may retry |
| C-04 | `close_all()` during active jobs | Shutdown path | `close_all()` calls `discard()` per account; active jobs may get disconnected clients | Jobs get `CancelledError` from asyncio; finalize as CANCELLED |
| C-05 | Client factory raises | `TelegramClient()` constructor fails | Exception propagates from `get()` | Job runner catches; job fails |
| C-06 | Empty session string | Corrupt DB or bug | `StringSession("")` may raise or create a broken client | Connection will fail; classified as AUTH_REVOKED or TRANSIENT |

---

## 6. Progress Reporting Edge Cases

Source: `app/bot/reporter.py`, `app/core/job_manager.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| RPT-01 | Reporter not registered | Job created without reporter registration | Events published to bus have no subscriber; silently ignored | User sees no progress card |
| RPT-02 | Message edit fails (deleted chat) | User deletes the progress message | `edit_message_text` raises; caught and logged | Final summary also fails to send |
| RPT-03 | Throttled DB writes skip intermediate states | Phase unchanged, < 2s since last write | DB update skipped; event still published | Telegram message may lag behind DB; latest event always reaches bus |
| RPT-04 | Phase change forces immediate write | Phase transitions (e.g., `inviting` → `waiting`) | DB updated immediately regardless of time | User sees accurate phase in progress card |
| RPT-05 | `on_progress` exception propagates | Bug in reporter callback | Engine catches and logs; job continues | Reporting failure never kills the job |
| RPT-06 | Double finish event | Race between completion and shutdown | `Reporter._on_finished` pops card mapping; second event finds `None` | Silently ignored |
| RPT-07 | Progress DB write failure | DB locked or network issue during `update_job_progress` | Caught and logged; event still published | DB counters may lag; final write in `_finalize` is the authoritative one |

---

## 7. Database & Finalization Edge Cases

Source: `app/db/repositories.py`, `app/core/job_manager.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| DB-01 | Double finalization (cancel races completion) | Cancel flag set while engine is completing | Guarded CAS: `transition_job` from `RUNNING` only succeeds once | **Exactly-once**: whichever call wins the CAS sets the final status |
| DB-02 | Counter write failure in `_finalize` | DB error during `update_job_progress` after CAS | Caught and logged; status already set | Counters may be stale; status is correct |
| DB-03 | Event publish failure in `_finalize` | Bus subscriber raises | Caught and logged; status is correct in DB | Telegram final summary not sent |
| DB-04 | Audit log failure | `repo.audit` raises | Not caught (would propagate) | **Potential issue**: could crash the finalization path |
| DB-05 | Account deleted during job | `ON DELETE SET NULL` FK constraint | Job's `account_id` becomes `None`; `render_job_card` shows "محذوف" | Job history preserved; account reference lost |
| DB-06 | Job state corrupted by manual DB edit | Manual SQL or bug | CAS transition may fail; `_finalize` returns `False` | Job stuck in non-final state; recovery on next boot marks it INTERRUPTED |
| DB-07 | Concurrent `transition_job` calls | Shutdown cancels while engine completes | CAS ensures exactly one wins; other returns `False` | Safe by design |
| DB-08 | `account_limited` marking failure in `_mark_account_fatal` | DB error when setting LIMITED status | Caught and logged; job still finalized | Account not marked limited; user may create another job on it |
| DB-09 | `account_unauthorized` marking failure | Same as DB-08 but for AUTH_REVOKED | Same as DB-08 | Account still appears active; next job may fail again |

---

## 8. Configuration & Settings Edge Cases

Source: `app/config.py`, `app/core/settings.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| CFG-01 | `max_members_per_job` set to 1 | User override | Only 1 member processed per transfer | Very slow; user may be confused |
| CFG-02 | `invite_delay_seconds` set to 0 | User override or config | No delay between invites | May trigger FloodWait immediately |
| CFG-03 | `flood_wait_max_seconds` set very low (e.g., 1) | User override | Any FloodWait > 1s aborts the job | Extremely aggressive; almost any FloodWait aborts |
| CFG-04 | `job_timeout_seconds` set very low (e.g., 60) | User override | Job times out after 60 seconds | May abort before any invites are sent |
| CFG-05 | `max_concurrent_jobs` set to 1 | Config | Only one job runs globally | All other users' jobs queue |
| CFG-06 | `max_running_jobs_per_user` set to 1 | Config (default) | One active job per user | User must wait or cancel before starting another |
| CFG-07 | `invite_delay_jitter_seconds` negative | Config validation | `pydantic` rejects with `ge=0` | Config load fails at startup |
| CFG-08 | `peer_flood_cooldown_seconds` set to 0 | Config | PeerFlood marks account as limited with `until=now` | Account immediately usable again; defeats the cooldown purpose |
| CFG-09 | User setting override not validated | User enters non-numeric text | `int(raw)` raises `ValueError`; user sees `M_SETTING_INVALID` | Safe rejection |
| CFG-10 | User setting override allows zero/negative | User enters `0` for `invite_delay_seconds` | Accepted (config allows `ge=0`) | See CFG-02 |

---

## 9. Session & Security Edge Cases

Source: `app/security/crypto.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| SEC-01 | Master key changed after sessions encrypted | Key rotation without re-encrypting sessions | All `decrypt()` calls fail with `CryptoError` | All accounts marked `UNAUTHORIZED`; users must re-login |
| SEC-02 | Corrupted session string in DB | DB corruption or partial write | `decrypt()` raises `CryptoError` | Account marked `UNAUTHORIZED` |
| SEC-03 | Master key file permissions too open | Shared environment | `os.chmod` attempts to set 0700; best-effort | Security warning logged |
| SEC-04 | `SESSIONS_MASTER_KEY` env var has trailing whitespace | Manual config | `env_key.strip()` cleans it | Handled by `strip()` |
| SEC-05 | Concurrent key file creation | Two processes start simultaneously | `os.O_EXCL` flag prevents overwrite; second process reads existing key | Safe; both processes use the same key |

---

## 10. Bot Wizard / FSM Edge Cases

Source: `app/bot/routers/transfers.py`

| # | Edge Case | Trigger | Current Behavior | Impact |
|---|-----------|---------|-----------------|--------|
| W-01 | FSM data stale after timeout | User starts wizard, waits long time, then confirms | FSM state may be cleared by aiogram; `_wizard_data` returns `None` values | User sees `M_WIZARD_CANCELLED`; must restart |
| W-02 | Account deleted between wizard steps | User picks account, then it's deleted before confirm | `accounts.get()` returns `None`; `ServiceError` raised | Wizard shows error |
| W-03 | Source/dest group deleted between wizard steps | User resolves source, group is deleted before confirm | Re-resolution in `confirm_transfer` fails; `ResolutionError` raised | Wizard shows error |
| W-04 | Preflight crash | `run_preflight` raises unexpected exception | Caught; `checks = []`; user sees `M_ERR_GENERIC` | Wizard stays; user retries |
| W-05 | Empty message text | User sends empty message in source/dest step | `raw = (message.text or "").strip()` → empty string; early return | Handler does nothing; user prompted again |
| W-06 | User sends non-text message (photo/sticker) | Media message in source/dest step | `message.text` is `None`; `raw` becomes empty string | Handler ignores; user prompted again |
| W-07 | Reporter registration fails | `reporter.register()` raises | Caught and logged; transfer still starts | User doesn't see progress card but transfer runs |
| W-08 | Preflight placeholder edit fails | Telegram API error editing the "checking..." message | Caught and logged; user sees the original placeholder | Minor UX issue; transfer still starts |
| W-09 | Source resolved but dest resolution fails | Source is valid; dest is invalid | User sees dest resolution error; can retry | Safe; wizard stays on dest step |
| W-10 | Refresh button after source/dest changed | User clicks refresh; source or dest group was deleted | Re-resolution in `_run_check` fails; error shown | User can retry or cancel |
| W-11 | Multiple rapid confirm clicks | User double-clicks confirm | `create_job` may be called twice; second call finds account busy | Second call raises `ServiceError`; only one job created |
| W-12 | Source and dest same group (caught by preflight) | User enters same group for both | `source.id == dest.id` checked in `_run_check`; same as P-01 | Duplicate check in wizard and preflight |

---

## Summary of Critical Edge Cases

The following edge cases have the highest severity or are most likely to affect
production usage:

1. **J-05**: Account with `status=limited` but unparseable `limited_until` — may
   bypass cooldown. (Severity: Medium)

2. **P-02**: Two invite previews with `id=0` bypassing the same-group check —
   mitigated because invite-preview sources always fail the membership check.
   (Severity: Low)

3. **T-12**: PeerFlood aborting the job and marking the account limited — by
   design, but users may not understand the cooldown. (Severity: Medium)

4. **DB-08/DB-09**: `_mark_account_fatal` failures being logged but not
   preventing finalization — account state may be inconsistent with the job
   outcome. (Severity: Medium)

5. **SEC-01**: Master key change invalidating all sessions — requires all users
   to re-login. (Severity: High if key rotation is performed)

6. **T-11**: FloodWait exceeding cap aborts the job but does NOT mark the
   account as limited — user can immediately retry, potentially hitting the
   same wall. (Severity: Low-Medium)

7. **CFG-08**: `peer_flood_cooldown_seconds=0` defeats the cooldown purpose.
   (Severity: Low — config-level)

---

*Generated by the Build Agent from source code analysis of the TMT member
transfer pipeline.*
