# UI/UX and Mandatory Subscription Process — Problem Report

**Date:** 2026-09-11  
**Scope:** Bot UI layer (`app/bot/`), gate middleware (`app/bot/middlewares.py`, `app/bot/gate.py`), login flow (`app/tg/login.py`), transfer wizard (`app/bot/routers/transfers.py`), account management (`app/bot/routers/accounts.py`)  
**Status:** Analysis report — issues identified, no code changes made

---

## Table of Contents

1. [Mandatory Subscription Gate — Critical Issues](#1-mandatory-subscription-gate--critical-issues)
2. [Account Login Flow — UI/UX Problems](#2-account-login-flow--uiux-problems)
3. [Transfer Wizard — UI/UX Problems](#3-transfer-wizard--uiux-problems)
4. [Job Management — UI/UX Problems](#4-job-management--uiux-problems)
5. [Settings Flow — UI/UX Problems](#5-settings-flow--uiux-problems)
6. [General UI/UX Problems](#6-general-uiux-problems)
7. [Gate-Specific Edge Cases](#7-gate-specific-edge-cases)
8. [Summary and Recommendations](#8-summary-and-recommendations)

---

## 1. Mandatory Subscription Gate — Critical Issues

### G1. Gate bypass after TTL expiry — users see stale state

**Files:** `app/bot/middlewares.py:108`, `app/bot/routers/common.py:67-87`

The `_gate_shown` set in `UserGateMiddleware` is an **in-memory `set[int]`** that tracks whether the gate screen has already been shown to a user. This set is **not persisted** and **resets on server restart**. After a restart:

- A user who was previously shown the gate screen (and is still in `_gate_shown`) will NOT see the gate screen again on their next message — they will only see the short `M_GATE_PLEASE_VERIFY` reminder (`middlewares.py:109`).
- But if the user has NOT verified membership and the server restarts, `_gate_shown` is empty, so they will see the full gate screen again. This is inconsistent behavior.
- More critically: if the gate was cleared (`gate_cleared=1`) but the user leaves a mandatory channel later, and then the admin calls `reset_user_gates()`, the `_gate_shown` set still retains the user's ID — the user will see the short reminder instead of the full gate screen with join links.

**Impact:** Users may not see the full gate screen (with join links) when they should, or may see it when they shouldn't.

### G2. Gate verify callback shows menu without gate state feedback

**File:** `app/bot/routers/common.py:67-87`

When the user presses "✅ تحقق من الاشتراك" and all memberships are verified, the handler calls `set_gate_cleared()` and immediately renders the main menu. However:

- If the user was NOT a member and taps verify, they get `M_GATE_NOT_VERIFIED` as a **popup alert** (`show_alert=True`). But after dismissing the alert, the user is still on the gate screen — there is no re-render of the gate screen showing updated status. The user must manually re-tap verify.
- If the mandatory set is empty (no channels configured), `query.answer()` is called with no message, but the gate screen is NOT replaced — the user stays on the old gate screen which may show stale data.

**Impact:** Confusing UX after failed verification; no visual feedback that the gate screen is still blocking.

### G3. Gate screen does not auto-refresh or show remaining channels

**File:** `app/bot/gate.py:17-42`, `app/bot/texts.py:230-242`

The gate screen shows all mandatory channels with join links, but there is no mechanism to:
- Show which channels the user has already joined vs. which they haven't.
- Auto-update the screen after the user joins a channel.
- Indicate the total number of channels vs. joined count.

The user must tap "✅ تحقق من الاشتراك" repeatedly after joining each channel. If there are many mandatory channels, this is tedious.

**Impact:** Poor UX for multi-channel mandatory subscriptions.

### G4. Gate verify callback is not in the same router — event loop race possible

**File:** `app/bot/middlewares.py:65-67`

The middleware allows the `gate:verify` callback to pass through to the handler even when the gate is not cleared. However, the middleware runs `repo.is_gate_cleared()` and `repo.active_channels()` on every update. If the admin adds/removes mandatory channels while the user is tapping verify, the middleware may see a different mandatory set than the handler sees. This is a minor race condition but can cause inconsistent behavior.

**Impact:** Minor — could show stale channel list.

### G5. Gate: no indication of which specific channels are not yet joined

**File:** `app/bot/gate.py:28-41`

The membership check is all-or-nothing. If a user has joined 4 out of 5 mandatory channels, the gate screen does NOT indicate which specific channel(s) are missing. The user must join all channels, then tap verify to find out if they missed one.

**Impact:** Frustrating UX for users with many mandatory channels.

### G6. Gate: private channel/group membership check may fail silently

**File:** `app/bot/gate.py:29-39`

The `check_membership` function catches ALL exceptions from `bot.get_chat_member()` and treats them as failures (fail-closed). This is correct for security, but the user sees no explanation of WHY the check failed. The `M_GATE_NOT_VERIFIED` message is generic — it does not distinguish between "you haven't joined" and "the bot cannot verify (removed from channel, etc.)".

**Impact:** Users may be permanently locked out if the bot is removed from a mandatory channel as admin.

### G7. Gate: no handling of bot being removed from mandatory channels

**File:** `app/bot/gate.py:33-38`

If the bot is removed as admin from a mandatory channel, `get_chat_member` raises an exception. The fail-closed behavior means ALL users are now blocked. There is no admin notification or automatic removal of the stale mandatory channel entry.

**Impact:** Entire bot can become unusable if a mandatory channel's bot admin status changes.

---

## 2. Account Login Flow — UI/UX Problems

### L1. Phone number validation is too permissive

**File:** `app/bot/routers/accounts.py:166`

```python
if len(phone) < 5 or not phone.startswith("+"):
```

The validation only checks for minimum length and `+` prefix. It does NOT validate:
- Country code correctness (e.g., `+000` would pass).
- Digit count (E.164 requires 7-15 digits after country code).
- That the number actually belongs to Telegram (detected only at the API level).

This means users can enter clearly invalid numbers and only discover the error after `LoginFlowManager.start()` is called (which involves a network round-trip to Telegram).

**Impact:** Extra network round-trip delay for obviously invalid inputs; poor UX.

### L2. Login flow TTL is not communicated to user

**File:** `app/tg/login.py:79`

The login flow has a TTL (default 600s / 10 minutes), but this is never shown to the user. The user has no idea how much time they have to complete the flow. If they take too long, they see `M_LOGIN_EXPIRED` with no indication of why.

**Impact:** Users may be confused by sudden flow expiry.

### L3. Login flow phone message is not deleted from chat

**File:** `app/bot/routers/accounts.py:159-181`

After the user sends a phone number, the code and password messages are deleted (per RULES §3/§7), but the phone number message itself is NOT deleted. This means the user's phone number remains visible in the chat history.

**Impact:** Security concern — phone number is a PII that persists in chat.

### L4. Login failure during `start()` does not clear FSM state properly

**File:** `app/bot/routers/accounts.py:169-178`

When `LoginFlowManager.start()` raises a `LoginFailure`, the handler catches it and shows an error, but does NOT clear the FSM state. The user is still in `AddAccountFSM.phone` state. If they send another phone number, the handler will try again correctly. However, the user may not realize they need to re-enter their phone number — the error message is shown, but the FSM state is not explicitly reset, which could confuse other handlers.

**Impact:** Minor — FSM state remains in `phone` state after failure, which is actually correct for retry, but the UX flow is unclear.

### L5. Code deletion may fail silently — secrets remain visible

**File:** `app/bot/routers/accounts.py:195`

`delete_quietly(message)` is best-effort. If the bot lacks permission to delete messages (e.g., in a group chat, though the bot is gated to private chats), the code/password remains visible in chat. The handler logs this but does not warn the user.

**Impact:** Security concern — login codes may persist in chat history.

### L6. No progress indicator during login network calls

**File:** `app/bot/routers/accounts.py:169-178`

When the user sends a phone number, the `LoginFlowManager.start()` call involves network I/O (connecting to Telegram, sending code). During this time, the user sees no loading indicator or feedback. The bot appears to hang until Telegram responds.

**Impact:** Poor UX — user may tap multiple times, causing duplicate flows.

### L7. 2FA password prompt does not indicate retry capability

**File:** `app/bot/texts.py:268-270`

The 2FA prompt says "الحساب محمي بتحقق بخطوتين، أرسل كلمة المرور" but does not inform the user that they can retry if the password is wrong. The `LoginFlowManager` keeps the flow alive for wrong passwords, but this is not communicated.

**Impact:** Users may think a wrong password terminates the flow.

### L8. Phone normalization strips all non-digit characters — overly aggressive

**File:** `app/bot/texts.py:304-313`

`normalize_phone()` strips ALL non-digit characters (except leading `+`). This means input like `+20 (100) 307-2694` becomes `+201003072694`, which is correct. However, it also strips the `+` from `+201003072694` if the user accidentally omits it — but then adds it back via `sign = "+" if cleaned.startswith("+") else ""`. The issue is that the sign is always `+` even for numbers that legitimately don't start with `+`. This is actually fine for E.164, but the validation at `accounts.py:166` checks `phone.startswith("+")` which would fail if normalization produced a number without `+`.

**Impact:** Minor — edge case in phone normalization.

### L9. Login flow does not handle concurrent login attempts for same user

**File:** `app/tg/login.py:115`

`LoginFlowManager.start()` calls `self.cancel(owner_id)` first, which cancels any existing flow. This means if a user is in the middle of a code entry step and starts a new phone flow, the old flow is silently cancelled. This is correct behavior but could be confusing if the user accidentally triggers a new flow.

**Impact:** Minor — accidental flow restart.

---

## 3. Transfer Wizard — UI/UX Problems

### T1. Source resolution failure clears entire wizard state

**File:** `app/bot/routers/transfers.py:138-142`

When source resolution fails (e.g., invalid link, private group), the handler catches `ServiceError` or `ResolutionError` and shows the error with `wizard_cancel()` keyboard. However, it does NOT clear the FSM state — the user stays in `TransferFSM.source`. If they retry with a valid source, the flow continues correctly. But if they tap the cancel button on the error message, the cancel callback works fine. The issue is that the error message shows `wizard_cancel()` which is just a cancel button — there is no "try again" option.

**Impact:** User must re-navigate from scratch after a resolution error.

### T2. Destination resolution error shows wrong keyboard

**File:** `app/bot/routers/transfers.py:173-174`

When `_run_check()` fails to resolve source or dest, it shows the error with `wizard_cancel()`. But this is on the dest step — the user has already provided a valid source. The error message does not indicate which resolution failed.

**Impact:** User cannot tell if the source or destination resolution failed.

### T3. Preflight report is not paginated — long reports may hit message limits

**File:** `app/bot/texts.py:352-355`

The `render_preflight()` function concatenates all checks into a single message. With 9 checks, each having an Arabic message, this can approach Telegram's 4096 character limit, especially with long Arabic text and details.

**Impact:** Message may be truncated or fail to send.

### T4. Transfer wizard does not show account status before source step

**File:** `app/bot/routers/transfers.py:107-118`

After the user picks an account, the wizard jumps to "ارسل يوزر او رابط المجموعه" without showing which account was selected. The user may not remember which account they picked if they took a break between steps.

**Impact:** Minor — lack of context reminder.

### T5. Preflight "refresh" button re-runs the entire check — slow for large groups

**File:** `app/bot/routers/transfers.py:236-257`

The "تحديث" (refresh) button re-resolves both source and dest and re-runs all 9 preflight checks. For large source groups, the `_source_participants` check iterates up to 200 members. This can take several seconds with no loading indicator.

**Impact:** User sees a delay after pressing refresh with no feedback.

### T6. Transfer wizard does not validate group types at source step

**File:** `app/bot/routers/transfers.py:124-149`

The source step accepts any valid Telegram entity. It does not check if it's a group (supergroup/chat) until the preflight stage. If the user enters a channel or user, they go through the entire wizard only to see a FAIL in preflight.

**Impact:** Wasted user effort on invalid inputs.

### T7. Confirm button has no "are you sure?" confirmation

**File:** `app/bot/routers/transfers.py:260-297`

Tapping "بدء النقل" immediately creates the job and starts the transfer. There is no secondary confirmation step. If the user accidentally taps the button, the transfer starts immediately.

**Impact:** Accidental job creation.

### T8. Transfer wizard does not show estimated transfer time or member count

**File:** `app/bot/texts.py:340-355`

The preflight report shows checks but does not display the source member count prominently. The user cannot easily gauge how long the transfer will take before confirming.

**Impact:** Users cannot make informed decisions about starting large transfers.

---

## 4. Job Management — UI/UX Problems

### J1. Jobs list only shows last 10 jobs — no pagination

**File:** `app/bot/routers/jobs.py:43-53`

The jobs list shows only the last 10 jobs. There is no way to see older jobs or paginate through job history.

**Impact:** Users with many jobs cannot see their full history.

### J2. Job card does not show total members in source group

**File:** `app/bot/texts.py:437-465`

The job card shows `invited`, `skipped`, `failed` but does not show the total source member count that was discovered during preflight. The `total` field is set during transfer but starts at 0.

**Impact:** Users cannot easily see the scope of a job.

### J3. Job cancel button appears on all job statuses including failed/completed

**File:** `app/bot/keyboards.py:128-134`

The cancel button only appears for active statuses (CREATED, VALIDATING, QUEUED, RUNNING). This is correct. However, the `job_detail` keyboard is used for both live cards and static job views, and the cancel button is conditionally shown. This is handled correctly.

**Impact:** None — actually correct behavior.

### J4. Reporter progress card does not show account name

**File:** `app/bot/reporter.py:51-62`

The initial progress card is sent with `render_progress_card()` which shows job ID and phase, but not the account name or source/dest titles. The user must look at the card to see which job is running, but the context is minimal.

**Impact:** Difficult to distinguish multiple concurrent jobs.

### J5. Job final summary does not show when the job was created or completed

**File:** `app/bot/texts.py:497-519`

The final summary shows counters but no timestamps. Users cannot see when the job started, how long it took, or when it finished.

**Impact:** No temporal context for job results.

### J6. Interrupted job message does not provide actionable next steps

**File:** `app/bot/texts.py:363-365`

`M_INTERRUPTED_NOTE` says "توقفت العملية بسبب إعادة تشغيل الخادم، يمكنك بدء عملية جديدة بنفس الإعدادات." But there is no button to start a new job with the same parameters. The user must manually navigate back through the wizard.

**Impact:** Inconvenient UX for job restart.

---

## 5. Settings Flow — UI/UX Problems

### S1. Settings do not show current global default values

**File:** `app/bot/routers/settings.py:40-48`, `app/bot/texts.py:173-186`

The settings screen shows the user's overrides, but when no override is set, it shows "—" for the value. The global Config default is not displayed. The user cannot see what the effective value is without an override.

**Impact:** Users don't know the default values and cannot make informed decisions about overrides.

### S2. Settings validation only checks integer — no range validation

**File:** `app/bot/routers/settings.py:70-98`

The settings value handler only validates that the input is an integer. It does NOT validate:
- `max_members_per_job` should be >= 1 and <= some reasonable maximum (e.g., 10000).
- `invite_delay_seconds` should be >= 0 and <= some maximum (e.g., 60).
- `flood_wait_max_seconds` should be >= 1.
- `job_timeout_seconds` should be >= 60.

The `UserSettings.set()` method does not validate ranges either.

**Impact:** Users can set extreme values that cause unexpected behavior.

### S3. Settings value is not persisted across restarts

**File:** `app/core/settings.py`

The `UserSettings` class stores overrides in a plain Python dict in memory. On server restart, all user settings are lost. There is no DB persistence for settings.

**Impact:** Users must reconfigure settings after every restart.

### S4. Settings reset button does not exist

**File:** `app/bot/keyboards.py:159-167`

There is no "reset" button to remove an override and revert to the global default. The `SettingsCB` has a "reset" action defined in the callback factory, but there is no handler for it and no button in the keyboard.

**Impact:** Users cannot revert to defaults without manually setting the current global value.

---

## 6. General UI/UX Problems

### U1. Main menu uses emoji — violates RULES §7

**File:** `app/bot/texts.py:82`

```python
name_line = f"↢ <b>أهلا بك يا <a href=\"tg://user?id={owner_id}\">{esc(display_name)}</a></b> 👋"
```

The main menu greeting line contains a 👋 emoji. RULES §7 forbids emoji in user-visible strings. This is present in `render_main_menu()` which is the first screen users see.

**Impact:** RULES violation.

### U2. Account card uses emoji — violates RULES §7

**File:** `app/bot/texts.py:321`

```python
f"<a href=\"tg://user?id={account.tg_user_id}\">{esc(account.display_name)}</a> ✿\n"
```

The `✿` character is not in the approved glyph set (`✓ ! × › = ?`). This is decorative and violates RULES §7.

**Impact:** RULES violation.

### U3. Job completed/cancelled/failed messages use emoji

**File:** `app/bot/texts.py:455-461`

```python
lines.append("✨|عمليه ناجحه .")
lines.append("✨|العمليه فشلت .")
lines.append("✨|تم الغي العمليه .")
```

These lines use the ✨ emoji which is not in the approved glyph set.

**Impact:** RULES violation.

### U4. Final summary uses emoji

**File:** `app/bot/texts.py:508`

```python
f"✨|العمليه تمت .",
```

**Impact:** RULES violation.

### U5. Admin menu uses emoji

**File:** `app/bot/texts.py:96-105`

```python
lines.append(f"🏷|عدد العمليات المكتمله ↼ <code>{completed_count}</code>")
```

The `🏷` emoji is used in the admin menu.

**Impact:** RULES violation (admin-only, but still).

### U6. Gate screen uses emoji

**File:** `app/bot/texts.py:235`

```python
icon = "👤" if entry.get("type") == "channel" else "🔰"
```

The gate screen uses 👤 and 🔰 emoji icons.

**Impact:** RULES violation.

### U7. Gate keyboard uses emoji

**File:** `app/bot/keyboards.py:180`

```python
icon = "📢" if entry.get("type") == "channel" else "👥"
```

**Impact:** RULES violation.

### U8. Broadcast center uses emoji

**File:** `app/bot/texts.py:560-593`

The broadcast center uses 📢, 👥, ✅, ❌ emoji throughout.

**Impact:** RULES violation.

### U9. `edit_or_answer` fallback is silent — user gets no feedback

**File:** `app/bot/routers/common.py:40-56`

When `edit_or_answer()` catches an exception (e.g., message not modified, message not found), it falls back to `query.answer()` with no text. The user sees a brief flash (the "callback answer" animation) but no message. This can happen when:
- The user taps the same button twice (message unchanged).
- The message was deleted by the user.
- The bot's edit permission is revoked.

**Impact:** Confusing UX — user taps button with no visible response.

### U10. Callback data collision possible for large account/job IDs

**File:** `app/bot/callbacks.py`

The `AccountCB` and `JobCB` use `int` fields for `account_id` and `job_id`. These are packed into callback data strings. For very large IDs (unlikely but possible), the callback data could exceed Telegram's 64-byte limit for callback data.

**Impact:** Theoretical — unlikely in practice.

### U11. Fallback handler clears FSM state on any unmatched message

**File:** `app/bot/routers/common.py:142-156`

The catch-all `fallback()` handler clears the FSM state and shows the main menu for ANY unmatched text message. This means if a user is in the middle of a transfer wizard and accidentally sends a message that doesn't match (e.g., a photo, sticker, or emoji), the wizard is cancelled without warning.

**Impact:** Accidental wizard cancellation.

### U12. No handling of non-text messages in FSM states

**File:** `app/bot/routers/accounts.py:159-181`, `app/bot/routers/transfers.py:124-149`

The FSM handlers (`phone_entered`, `code_entered`, `source_entered`, etc.) only handle `message.text`. If a user sends a photo, sticker, voice message, or other non-text content while in an FSM state, the message is silently ignored (no response). The user may not realize they need to send text.

**Impact:** Confusing UX — user sends non-text content and gets no response.

---

## 7. Gate-Specific Edge Cases

### E1. Gate bypass via `/start` command after gate cleared

**File:** `app/bot/middlewares.py:86-90`

In group chats, the bot answers only `/start` with `M_PRIVATE_ONLY`. But the middleware checks gate BEFORE checking chat type. If a user sends `/start` in a group and is not gate-cleared, they see the gate screen instead of `M_PRIVATE_ONLY`. This is because the gate check runs first.

**Impact:** Minor — group users see gate screen instead of private-chat redirect.

### E2. Gate verify with empty mandatory list clears gate but shows stale screen

**File:** `app/bot/routers/common.py:74-87`

If `mandatory` is empty (no channels configured), `check_membership` is never called, and `set_gate_cleared()` is called immediately. The gate screen is replaced with the main menu. This is correct. However, if the admin then adds channels while the user is on the gate screen, the user's gate_cleared is still 1 (not reset yet). The `reset_user_gates()` call happens in the admin handler, which sets all users to `gate_cleared=0`. So on the user's next interaction, the gate will be re-checked. This is correct but the transition is not smooth.

**Impact:** Minor — brief window of inconsistency.

### E3. Gate: user can clear gate by sending any message after channels are removed

**File:** `app/bot/middlewares.py:70-81`

If all mandatory channels are removed (empty list), the gate check passes immediately (`if mandatory:` is false). The user is never blocked. This is correct behavior. However, if the user has a stale `gate_cleared=1` and the admin adds new channels, the `reset_user_gates()` sets `gate_cleared=0` for all users, so the gate will re-check. This is correct.

**Impact:** None — correct behavior.

### E4. Gate: race between middleware and handler for gate:verify

**File:** `app/bot/middlewares.py:65-67`

The middleware allows `gate:verify` through even when gate is not cleared. But the middleware does NOT inject `bot` into the handler data when gate is not cleared — it returns early at line 81 (`return None`). Wait — looking more carefully:

```python
if not await repo.is_gate_cleared(self._db, user.id):
    mandatory = await repo.active_channels(self._db)
    if mandatory:
        bot = data.get("bot")
        if bot is not None:
            all_joined = await check_membership(bot, user.id, mandatory)
            if all_joined:
                await repo.set_gate_cleared(self._db, user.id)
                self._gate_shown.discard(user.id)
            else:
                await self._show_gate(inner, bot, mandatory, user.id)
                return None  # <-- gate blocked, handler not called
```

The `gate:verify` callback is allowed through BEFORE the gate check (line 65-67). So the handler is called. The handler in `common.py` then calls `check_membership()` and `set_gate_cleared()` independently. This is correct — the verify handler does its own check.

But the middleware also injects `db` into handler data at line 66: `data["db"] = self._db`. This is only done for the verify callback path. For other callbacks when gate is blocked, `db` is NOT injected. This means if the verify handler needs other services (like `accounts`, `jobs`), they may not be available.

Looking at the verify handler:
```python
async def cb_gate_verify(
    query: CallbackQuery, callback_data: GateCB, db: Database, bot: Bot,
    accounts: AccountService, jobs: JobManager,
) -> None:
```

It needs `accounts` and `jobs`. These are injected by the dispatcher's workflow data, not by the middleware. So they should be available. The middleware only injects `db` — other services come from the dispatcher. This is correct.

**Impact:** None — correct behavior.

### E5. Gate: toggle inactive channel still blocks users

**File:** `app/bot/routers/admin/channels.py:183-193`

When an admin toggles a channel to inactive, `toggle_channel()` is called, and then `reset_user_gates()` is called. This means all users must re-verify. However, the inactive channel is still in the `active_channels` query result... let me check:

```python
async def active_channels(db: Database) -> list[dict[str, Any]]:
    """Return all active mandatory channels/groups."""
    rows = await db.fetch_all(
        "SELECT channel_id, title, invite_link, type FROM channels WHERE is_active=1"
    )
    ...
```

The query filters `is_active=1`, so inactive channels are excluded. This is correct. After toggle + reset, users only need to verify against active channels.

**Impact:** None — correct behavior.

### E6. Gate: duplicate channel IDs possible

**File:** `app/db/repositories.py` (add_channel)

If an admin adds the same channel twice, there is no UNIQUE constraint on `channel_id` in the `channels` table. This could lead to duplicate entries, and users would see the same channel listed twice in the gate screen.

**Impact:** Duplicate gate entries.

---

## 8. Summary and Recommendations

### Critical Issues (fix immediately)

| ID | Issue | Impact |
|---|---|---|
| U1-U8 | Emoji usage violates RULES §7 | Security/compliance |
| G1 | In-memory `_gate_shown` not persisted | Gate bypass possible |
| G6 | No explanation for gate check failures | Users permanently locked out |
| S3 | Settings not persisted across restarts | Data loss |

### High-Priority Issues (fix soon)

| ID | Issue | Impact |
|---|---|---|
| G3 | No channel-specific join status | Poor multi-channel UX |
| G5 | No indication of which channels not joined | Frustrating UX |
| L3 | Phone number not deleted from chat | PII exposure |
| L6 | No loading indicator during login | Confusing UX |
| T1 | Resolution error clears wizard context | User must restart |
| E6 | Duplicate channel entries possible | Gate duplication |

### Medium-Priority Issues (fix when convenient)

| ID | Issue | Impact |
|---|---|---|
| L1 | Phone validation too permissive | Extra network round-trip |
| L2 | Login TTL not shown to user | Confusing expiry |
| T7 | No confirmation before transfer start | Accidental jobs |
| T8 | No estimated transfer time shown | Informed decisions |
| J1 | No job history pagination | Limited history |
| J4 | Progress card lacks context | Hard to distinguish jobs |
| S1 | Settings don't show global defaults | Unclear effective values |
| S2 | No range validation on settings | Extreme values |
| S4 | No settings reset button | Cannot revert to defaults |
| U9 | Silent fallback in edit_or_answer | Confusing UX |
| U11 | Fallback clears FSM without warning | Accidental cancellation |
| U12 | Non-text messages ignored in FSM | Confusing UX |

### Low-Priority Issues (nice to have)

| ID | Issue | Impact |
|---|---|---|
| G2 | Verify callback no visual feedback | Minor confusion |
| G4 | Gate verify race condition | Minor inconsistency |
| T4 | No account reminder after pick | Minor context loss |
| J2 | Job card missing total members | Minor info gap |
| J5 | Job summary missing timestamps | Minor info gap |
| J6 | Interrupted job no restart button | Minor inconvenience |
| L4 | Login failure FSM state unclear | Minor confusion |
| L5 | Code deletion may fail silently | Minor security |
| L7 | 2FA retry not communicated | Minor UX |
| L8 | Phone normalization edge case | Minor validation |
| L9 | Concurrent login cancel | Minor UX |

### Files That Need Changes

1. **`app/bot/texts.py`** — Remove all emoji (U1-U8), add setting defaults display (S1), add login TTL notice (L2), add confirmation text (T7)
2. **`app/bot/middlewares.py`** — Persist `_gate_shown` or use DB-based tracking (G1)
3. **`app/bot/gate.py`** — Add per-channel status to gate check (G3, G5), add error explanations (G6)
4. **`app/bot/keyboards.py`** — Remove emoji (U7), add settings reset button (S4)
5. **`app/bot/routers/accounts.py`** — Delete phone message (L3), add loading indicator (L6), validate phone stricter (L1)
6. **`app/bot/routers/transfers.py`** — Add context to error messages (T2), add loading indicators (T5), add confirmation step (T7)
7. **`app/bot/routers/settings.py`** — Show global defaults (S1), add range validation (S2), persist to DB (S3)
8. **`app/bot/routers/common.py`** — Add feedback on fallback (U9), warn before FSM clear (U11)
9. **`app/db/repositories.py`** — Add UNIQUE constraint on channel_id (E6), add settings table (S3)
10. **`app/db/migrations.py`** — Add settings migration (S3)
