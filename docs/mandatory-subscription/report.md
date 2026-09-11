# Mandatory-Subscription Gate — UI/UX & Process Risk Report

**Scope:** `app/bot/gate.py`, `app/bot/middlewares.py`, `app/bot/keyboards.py`,
`app/bot/texts.py`, `app/bot/routers/common.py`, `app/bot/routers/admin/channels.py`,
`app/bot/routers/admin/keyboards.py`, `app/bot/callbacks.py`, `app/db/repositories.py`,
`app/db/migrations.py` (v9), and the `channels` table.

**Status:** Build-time inspection of branch `agent/feature-34656115182`.
**Verdict:** The mandatory-subscription gate is *functionally* correct for the happy
path (user joins → verify → clears), but it has several **silent failure modes that
lock users out with no in-app diagnostic for the operator**, a few **recoverable 500s
in the admin UI**, and a **drift from the project's own no-emoji UI contract**.
Findings are severity-rated below: **S1** critical, **S2** high, **S3** medium,
**S4** low.

---

## 0. How the feature works today

1. **Configure (admin-only).** `/admin` → `↢ الاشتراك الإجباري` → tabs
   `📢 القنوات` / `👥 المجموعات` → `↢ إضافة قناة|مجموعة` → admin sends a ref
   (`@name`, `t.me/name`, `+hash`, or numeric id) → bot resolves via
   `bot.get_chat` → stores `channel_id/title/invite_link/type` in `channels`
   (`app/db/migrations.py:229`). Every add/toggle/delete calls
   `reset_user_gates()` which sets `users.gate_cleared=0` for **all** users.
2. **Enforce (every update).** `UserGateMiddleware` (`app/bot/middlewares.py:69`)
   runs as the outer Update middleware: upserts the user → blocks if blocked →
   **bypasses the gate for the `gate:verify` callback** → then, if the user is
   not `gate_cleared` and `active_channels()` is non-empty, calls
   `check_membership()` (`app/bot/gate.py:17`) for every entry.
3. **Verify membership.** `check_membership` calls `bot.get_chat_member` per
   channel; **fail-closed** (one exception ⇒ returns `False`). On success the
   user is allowed through; on failure `_show_gate()` renders the gate screen
   (`render_gate_screen`, `gate_kb`) with one join-URL button per entry + a
   `✅ تحقق من الاشتراك` verify button.
4. **User proves membership.** Tap verify → `cb_gate_verify`
   (`app/bot/routers/common.py:67`) re-checks membership → on success sets
   `gate_cleared=1` and re-renders the main menu; on failure alerts
   `M_GATE_NOT_VERIFIED`.

---

## 1. Findings

### F-1 · Fail-closed on a single unverifiable channel blocks *everyone*, with no in-app operator signal — **S1**

`check_membership` (`app/bot/gate.py:28-41`) iterates the mandatory set and returns
`False` on the **first** `Exception` from `bot.get_chat_member`. For the membership
call to succeed the **bot itself must be an admin** of every mandatory
channel/group. If the bot loses admin rights in *one* entry (channel demoted the
bot, bot removed from a group, channel deleted, Telegram hiccup), `get_chat_member`
raises → `check_membership` returns `False` → **every** non-cleared user is
permanently stuck at the gate on their next interaction.

The **only** signal is a `logger.warning`
(`app/bot/gate.py:34`) that lives in server logs. No user-facing message explains
*why* verify keeps failing, and there is **no admin-facing indicator** (no counter,
no audit event, no "bot missing admin rights" banner). Result: a total, silent
denial-of-service for all end users, diagnosable only by someone with shell access
to `grep` the logs.

**Recommendation.** `check_membership` should classify failures:
- API "chat not found / bot was removed" → distinct from transient errors and from
  "user is not a member". Surface a per-entry status so the gate screen (and an
  admin dashboard) can say *which* entry is unverifiable.
- Write an `audit_log` row on repeated gate failures (rule: never lose data
  silently, RULES §3).
- Add an admin widget: "mandatory entries the bot cannot read" (green/red dot per
  entry), so the operator can self-serve instead of tailing logs.

### F-2 · Admin can be locked out of `/admin` by their own gate — **S2**

The user gate is enforced in the outer middleware **before** the admin router's
`IsAdmin` filter (`app/bot/routers/admin/router.py:10-11`, `app/bot/routers/admin/menu.py:27`).
Any user in `admin_ids` who has not cleared the gate (or is not a member of *every*
mandatory channel) is blocked from **all** handlers — including `/admin` itself.

Operational scenario: an operator adds a mandatory channel they do **not**
personally belong to (e.g. a promotional channel owned by a third party, or a group
the operator isn't in). The operator is immediately unable to open `/admin` to
fix it; the only resolution is to ask another member to remove the offending
channel, or to restart the operator's session in a context where they *are* a
member.

**Recommendation.** Exempt admin users (or at least allow `/admin`) from the
user gate, or gate only on **non-admin** users. The gate's purpose is subscriber
acquisition for *end users*; the operator configuring it should not be subject to
the gate they are configuring.

### F-3 · `cb_gate_verify` gives no per-entry feedback — "verify" just says "not verified" — **S3**

On failure, the verify callback answers with the generic toast
`M_GATE_NOT_VERIFIED` ("لم يتم العثور على جميع الاشتراكات. يرجى الاشتراك أولاً
ثم أعد المحاولة.") at `app/bot/texts.py:225`. The user is told *none* of:
which channel they are missing, or whether the bot can even verify membership.
With 3–4 mandatory channels the user must guess which tab/permission is the
problem and re-tap verify repeatedly, producing the same opaque toast each time.

**Recommendation.** Return the subset of entries the user has **not** joined (or
that the bot cannot read, per F-1) and list them inline on the verify response,
e.g. "لم تنضم إلى: قناة أ، المجموعة ب". Re-render the gate keyboard so the
missing ones are visually highlighted.

### F-4 · Gate screen is shown only once per process session; later attempts get a bare toast with **no** join links — **S3**

`_show_gate` (`app/bot/middlewares.py:95-113`) uses an **in-memory** set
`self._gate_shown` to avoid re-sending the full gate screen. After the first
blockage, every subsequent interaction (new message or button tap while still
blocked) gets only `M_GATE_PLEASE_VERIFY` (`app/bot/texts.py:226`) — a one-line
toast — **without** the channel list or the verify button attached.

Two compounding problems:
- The join links + `✅ تحقق من الاشتراك` button exist only on the *original* gate
  message. If the user dismisses it or opens a new device view, they cannot
  re-trigger the screen from within the conversation without scrolling to the
  original message.
- `_gate_shown` is **per-process and in-memory**, so it is lost on every bot
  restart. After a restart the user is re-shown the full screen (spam), and
  during an uptime it degrades to toasts. The two states are inconsistent
  sources of truth (`gate_cleared` in the DB vs `_gate_shown` in RAM).

**Recommendation.** Either remove the `_gate_shown` de-duplication (the gate
screen is small and idempotent), or — better — only ever send the full screen and
skip the alert-only path. Make the "shown" state derive from the DB
(`is_gate_cleared`) rather than a RAM set, so it is consistent across restarts.

### F-5 · After `reset_user_gates`, existing users only get a toast — they cannot see the **new** mandatory entries — **S3**

`reset_user_gates` (`app/bot/repositories.py:485`) clears `gate_cleared` for all
users whenever any channel is added/toggled/deleted. Combined with F-4: a user who
already had `_gate_shown` set now receives only the terse toast on their next
interaction. Their **previously-cached** gate screen still in chat lists the
**old** channel set, so the freshly-added mandatory entry is invisible to them
until they scroll up and re-read stale content.

**Recommendation.** When the mandatory set changes, the gate screen should be
re-pushed to blocked users (or at least the verify button should re-render the
current screen), so the *current* required set is always visible.

---

## 2. UI / iconography defects

### F-6 · Inconsistent icons for the same entity across the two screens — **S3**

The mandatory-subscription feature uses **two different icon sets** for the same
entity types in the same feature:

| Screen | File | Channel icon | Group icon |
|---|---|---|---|
| Admin list / tabs (keyboard + text) | `admin/keyboards.py:94`, `texts.py:251` | `📢` | `👥` |
| User gate screen (keyboard) | `keyboards.py:180` | `📢` | `👥` |
| **User gate screen (text)** | `texts.py:235` | **`👤`** | **`🔰`** |

`render_gate_screen` (`app/bot/texts.py:230-242`) renders the gate **text** with
`👤` for a channel and `🔰` for a group, while the very same screen's inline
keyboard (`gate_kb`, `app/bot/keyboards.py:176-184`) renders `📢`/`👥` for the
same entries. A user therefore sees one icon in the message body and a *different*
icon next to it on the join button — for the same channel.

**Recommendation.** Use a single shared icon mapper (e.g. `entry_glyph(entry)` in
`texts.py`) used by both `render_gate_screen` and `gate_kb`, so channel/group
representation is consistent everywhere.

### F-7 · Gate text uses emoji that violate the project's own UI contract — **S4** (rule drift)

`RULES.md §7` ("Arabic UI contract") explicitly **forbids emoji** and restricts
decoration to `✓ ! × ? ›` and `↺`-style symbols. However the entire bot
(including the gate) is built with emoji (`👤`, `🔰`, `📢`, `👥`, `👋`, `💳`,
`📚`, `🔐`, `⚙️`, `🗑️`, …). `tests/test_texts.py:26` even maintains
`_ALLOWED_SYMBOLS` that whitelists `👤🔰💳✅✨📚🔐🏷`, so the "no emoji" rule is
**not enforced** — the codebase has drifted from its own spec. The gate screen is
affected (`👤`/`🔰`, see F-6); the broader drift is a *codebase-wide* concern
flagged here only because the gate inherits it.

**Recommendation (gate only).** Drop the emoji from the gate screen and keyboard
(replace with `✓`/`×`/text labels). The wider emoji drift should be reconciled
project-wide in a separate pass (it is out of scope for this report's feature).

### F-8 · Gate screen join link is interpolated into an HTML attribute without escaping — **S4** (latent injection)

`render_gate_screen` (`app/bot/texts.py:237-238`) interpolates
`entry['invite_link']` raw into
`<a href='{invite_link}'>انضم ↢</a>`. Only `entry['title']` is passed through
`esc()`. The `invite_link` originates from the admin-provided ref / `chat.invite_link`
(`app/bot/routers/admin/channels.py:135`), so it is not arbitrary user input — but it
is **admin-supplied** content placed unescaped into a single-quoted attribute. A
malformed or single-quote-bearing link value could break the attribute or, in a
future renderer that doesn't auto-link, introduce markup. (URL buttons in the
keyboard are unaffected — they take the literal string.)

**Recommendation.** Escape attribute values, or — simpler — drop the `<a href>` text
link and rely solely on the URL inline button (the keyboard already provides a join
button). Keeping two join paths (text link + button) is redundant.

---

## 3. Recoverable server errors (admin UI)

### F-9 · Duplicate mandatory channel → unhandled `IntegrityError` (500) — **S2**

`channels` has `UNIQUE(channel_id)` (V3, `app/db/migrations.py:119`) but **no**
`ON CONFLICT`/upsert. `add_channel` (`app/bot/repositories.py:430-436`) is a plain
`INSERT`, and in `entry_ref_entered` (`app/bot/routers/admin/channels.py:123-158`)
the `add_channel` + `reset_user_gates` calls sit **outside** the `try/except` that
guards `bot.get_chat`. Adding the same channel twice (e.g. pasting `@name` again,
or re-adding a channel that is simultaneously a "group") raises
`aiosqlite.IntegrityError`, which is not caught by the handler → aiogram logs a 500
and the admin gets **no feedback** (the entry is not added, no message is sent).

**Recommendation.** Make `add_channel` idempotent (`INSERT … ON CONFLICT(channel_id)
DO UPDATE` to refresh `title`/`invite_link`/`is_active` and `type`, or `DO
NOTHING` + a "updated" message), and wrap the whole save path so a duplicate is
reported as `✅ تم تحديث القناة` rather than a silent 500.

### F-10 · `toggle_channel` / `delete_channel` rely on raw `id` in callback data with no FK guard — **S4**

`CH_TOGGLE`/`CH_DELETE` encode `type:id` parsed by `_int_pair`
(`app/bot/routers/admin/channels.py:40`). `_int_pair` correctly rejects
non-integer suffixes (returns `None` → `M_ENTRY_NOT_FOUND` shown). Low risk, but
the `id` is the row's `id` (not `channel_id`); if an operator's client somehow
sends a stale `id` after a delete, the `UPDATE … WHERE id=?` affects 0 rows and
the handler silently re-renders the (now-stale) tab rather than signaling
"not found". `_int_pair` is also re-implemented per-router (`_int_pair` here vs
`_int_after` in `users.py`), a small duplication. Defensive but worth unifying.

**Recommendation.** Have `toggle_channel`/`delete_channel` return the affected-row
count (they already do, `bool`) and branch on it: show `M_ENTRY_NOT_FOUND` when
0 rows changed. Consolidate the `_int_pair`/`_int_after` int-parsing helper.

---

## 4. Process / data-integrity issues

### F-11 · Mandatory-entry changes have **no audit trail** — **S3**

Adding, toggling, or deleting a mandatory channel writes **no** `audit_log` row
(`app/bot/routers/admin/channels.py:138-220` never calls `repo.audit`). Compare
with `users.py` which audits every block/notify/delete (`users.py:152,222,269-276`).
Operators cannot answer "who removed the channel users were complaining about" or
"when did the gate lock everyone out". `reset_user_gates` is the only side effect,
and it is silent.

Additionally, the `channels` table has **no** `created_at` / `created_by` /
`updated_at` columns (`app/db/migrations.py:117-123,229-232`), so history is not
even stored at the row level.

**Recommendation.** (a) Add `audit_log` calls on add/toggle/delete with the acting
admin id; (b) add `created_at`/`updated_at` (and optionally `created_by`) to
`channels` via a new migration.

### F-12 · `_gate_shown` + `gate_cleared` = two sources of truth for gate state — **S3**

The persistent fact ("user may proceed") lives in `users.gate_cleared`
(`repositories.py:476-487`), but the *display* decision ("have I shown the gate
screen this session?") lives in the in-memory `self._gate_shown` set
(`app/bot/middlewares.py:39,78,108,113`). They can disagree:
- Bot restart clears `_gate_shown` but not `gate_cleared` → user re-shown the full
  gate screen even though they already cleared it this "logical session" (then
  immediately auto-cleared). Mild spam.
- After `reset_user_gates` (F-5), `gate_cleared` is 0 but `_gate_shown` is still
  populated for in-session users → they get toasts instead of the screen,
  defeating the purpose of the reset.

**Recommendation.** Derive display behavior from `gate_cleared`+the mandatory set
alone; remove `_gate_shown` or move it to the DB/user row.

### F-13 · "restricted" members are treated as fully subscribed — **S4** (policy)

`check_membership` (`app/bot/gate.py:40`) accepts `member.status in
("creator","administrator","member","restricted")`. Telegram's `restricted` status
means the user *is* in the chat but is muted/limited by an admin. Counting a
restricted user as "subscribed" is a policy choice; depending on the operator's
intent (e.g. "must be an active subscriber", not "must be a muted ex-member"),
this may over-grant. The code does not document or parameterize the decision.

**Recommendation.** Either exclude `restricted` (treat as not-subscribed for a
"must be an active member" gate) or make the accepted-status set a config flag,
and document the rationale in `gate.py`.

### F-14 · Gate re-checks on every update, but never *proactively* — **S4** (UX)

The gate is only re-evaluated when the user sends a message or taps a button. If a
user joins all the channels *between* interactions, they must still trigger an
update (or tap `✅ تحقق من الاشتراك`) to clear the gate — there is no background
"refresh". This is by design (cheap, no push API), but the **only** explicit
recovery is the verify button (F-4 shows that button can disappear), so a user
who joined everything but only ever taps blocked buttons can be left with toasts.

This is the same root cause as F-4/F-5; listing it separately to stress that the
verify button is the **sole** user-driven escape hatch and it must always be
reachable.

**Recommendation.** Guarantee a reachable verify path: always offer a persistent
"✅ تحقق من الاشتراك" control, and have tapped-blocked-buttons (F-6 alert in
`_show_gate`) suggest tapping that control rather than a bare toast.

---

## 5. Test-coverage gap

### F-15 · The mandatory-subscription feature has **no dedicated tests** — **S3**

`tests/` contains gate coverage **only** inside `tests/test_dispatcher.py`:
`test_gate_blocks_unsubscribed_user`, `test_gate_allows_verified_callback`,
`test_gate_blocks_when_bot_cannot_verify_membership`,
`test_gate_clears_when_user_already_member`. There is **no** test for:

- the admin channel CRUD (`channels.py`: add/toggle/delete, duplicate handling,
  the invite-link fallback FSM in `entry_invite_link_entered`);
- `cb_gate_verify` itself (`common.py:67`) — success, failure, and empty-mandatory
  cases;
- `reset_user_gates`, `is_gate_cleared`, `set_gate_cleared` (`repositories.py`);
- the icon inconsistency (F-6) that `test_render_gate_screen_lists_mandatory_entries`
  could have caught but does not check icon consistency;
- the admin-lockout (F-2) and the duplicate-IntegrityError (F-9) paths.

The F-1 test even documents the *intended* fail-closed behavior but no test
asserts that an admin is exempt (there is no exemption code at all today).

**Recommendation.** Add a `tests/test_gate.py` / `tests/test_admin_channels.py`
covering: duplicate add (idempotency), toggle/delete not-found signaling, verify
success/failure/empty, and gate-re-show-after-reset. This also hardens F-1/F-9
regression protection.

---

## 6. Prioritized summary

| ID | Title | Severity | Location |
|----|-------|----------|----------|
| F-1 | Fail-closed on one unverifiable channel blocks all users; no in-app operator signal | S1 | `gate.py:28-41` |
| F-2 | Admin locked out of `/admin` by their own gate | S2 | `middlewares.py:69`, `menu.py:27`, `router.py:10` |
| F-9 | Duplicate channel → unhandled IntegrityError (500) | S2 | `channels.py:123-158`, `repositories.py:430` |
| F-3 | `verify` gives no per-entry feedback | S3 | `common.py:67-87`, `texts.py:225` |
| F-4 | Gate shown once/process; later attempts = bare toast, no links | S3 | `middlewares.py:95-113` |
| F-5 | After `reset_user_gates` users can't see the *new* entries | S3 | `middlewares.py:108`, `repositories.py:485` |
| F-11 | No audit trail for mandatory-channel changes | S3 | `channels.py:138-220`, `migrations.py:117` |
| F-12 | Two sources of truth: `_gate_shown` (RAM) vs `gate_cleared` (DB) | S3 | `middlewares.py:39`, `gate.py` |
| F-6 | Inconsistent icons (👤/🔰 vs 📢/👥) on the same screen | S3 | `texts.py:235`, `keyboards.py:180` |
| F-15 | No dedicated tests for the subscription feature | S3 | `tests/` |
| F-7 | Gate uses emoji forbidden by RULES §7 (whole bot drifted) | S4 | `texts.py:235`, `RULES.md §7` |
| F-13 | "restricted" members counted as subscribed (policy) | S4 | `gate.py:40` |
| F-14 | No proactive gate refresh; verify is the sole escape hatch | S4 | `middlewares.py:69` |
| F-8 | Unescaped `invite_link` in an HTML attribute | S4 | `texts.py:237-238` |
| F-10 | Stale `id` after delete re-renders silently; helper duplicated | S4 | `channels.py:40`, `users.py:54` |

---

## 7. Recommended fix order (for the Review/Fix stages)

1. **S1 — F-1:** Make `check_membership` classify failure reasons and surface
   "which entry is unverifiable" to the user and to an admin dashboard; add an
   `audit_log` event on repeated failures.
2. **S2 — F-2:** Exempt admin users from the user gate (at least for `/admin`).
3. **S2 — F-9:** Make `add_channel` idempotent; report duplicates as "updated".
4. **S3 — F-4/F-5/F-12:** Remove the in-RAM `_gate_shown` de-duplication; always
   re-render the current gate screen on a blocked interaction; keep one source of
   truth (`gate_cleared` + the channel set).
5. **S3 — F-3:** Have `cb_gate_verify` list the missing/unverifiable entries.
6. **S3 — F-6:** One shared glyph mapper for channel/group across text + keyboard.
7. **S3 — F-11:** Audit channel add/toggle/delete; add timestamps to `channels`.
8. **S3 — F-15:** Tests for the admin CRUD + verify + idempotency paths.

> Note: nothing here was changed in code — this is a **design/inspection report**
> only. All findings are reproducible against `agent/feature-34656115182` and are
> ready to be triaged by the Review stage.
