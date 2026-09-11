# Appendix — UI Inventory: Mandatory-Subscription Flow

Reference of every user-facing string and icon in the mandatory-subscription
feature, with file:line. Used to ground the findings in `report.md` (notably
F-6 icon inconsistency and F-7 emoji drift).

## Admin side (`/admin`)

| Element | Source | Text / value | Notes |
|---|---|---|---|
| Admin menu button | `routers/admin/keyboards.py:20` | `↢ الاشتراك الإجباري` | label `BUT_MANDATORY_SUBSCRIPTION` (`texts.py:198`) |
| Tabs keyboard | `routers/admin/keyboards.py:82-87` | `📢 القنوات` / `👥 المجموعات` / `› رجوع` | callbacks `CH_TAB_CHANNELS`/`CH_TAB_GROUPS`/`MENU` |
| Empty channel list message | `texts.py:211` | `× لم يتم إضافة قنوات إجبارية بعد.` | `M_NO_CHANNELS` |
| Empty group list message | `texts.py:212` | `× لم يتم إضافة مجموعات إجبارية بعد.` | `M_NO_GROUPS` |
| Add-entry prompt | `texts.py:213` | `↢ أرسل معرف القناة/المجموعة أو الرابط (مثال: @name أو t.me/name):` | `M_ADD_ENTRY_PROMPT` |
| Entry added toast | `texts.py:214` | `✅ تم إضافة العنصر.` | `M_ENTRY_ADDED` |
| Entry toggled toast | `texts.py:216` | `✅ تم تحديث الحالة.` | `M_ENTRY_TOGGLED` |
| Entry delete prompt | `texts.py:245` | `× هل أنت متأكد من حذف هذا العنصر؟` | `M_ENTRY_DELETE_PROMPT` |
| Entry not-found (admin) | `texts.py:217` / `246` | `× لم يتم العثور على …` | `M_ENTRY_NOT_FOUND` / `M_ENTRY_LINK_PROMPT` |
| Entries list row glyph | `texts.py:251` | `📢` channel / `👥` group | `render_entries_list` |
| Entry list row | `texts.py:256-258` | `✅/❌ <title> — <invite_link>` | status glyph + escaped title + raw link |

## User side (the gate)

| Element | Source | Text / value | Notes |
|---|---|---|---|
| Gate screen title | `texts.py:220-223` | `↢ الاشتراك الإجباري` + body | `M_GATE_BLOCKED` |
| Gate entry icon (text) | `texts.py:235` | `👤` channel / `🔰` group | **inconsistent with keyboard (F-6)** |
| Gate join link | `texts.py:237-238` | `<a href='{invite_link}'>انضم ↢</a>` | link unescaped (F-8) |
| Gate keyboard join button | `keyboards.py:180-183` | `📢 <title>` / `👥 <title>` (URL button) | icon **differs** from text (F-6) |
| Verify button | `keyboards.py:184` | `✅ تحقق من الاشتراك` | `BUT_VERIFY_SUBSCRIPTION` (`texts.py:207`) |
| Verify success | `texts.py:224` | `✅ تم التحقق من الاشتراك! يمكنك الآن استخدام البوت.` | `M_GATE_VERIFIED` |
| Verify failure | `texts.py:225` | `× لم يتم العثور على جميع الاشتراكات …` | `M_GATE_NOT_VERIFIED` — no per-entry detail (F-3) |
| Blocked-on-button toast | `texts.py:227` | `↢ يجب الاشتراك في القنوات المطلوبة أولاً. استخدم الزر في الدردشة.` | `M_GATE_BLOCKED_ALERT` |
| Please-verify toast | `texts.py:226` | `↢ يرجى التحقق من الاشتراك أولاً.` | `M_GATE_PLEASE_VERIFY` — sent after first gate show (F-4) |

## In-code policy / constants

| Item | Source | Value | Notes |
|---|---|---|---|
| Accepted member statuses | `gate.py:40` | `creator, administrator, member, restricted` | `restricted` debatable (F-13) |
| Fail-closed on API error | `gate.py:28-41` | returns `False` on any exception | fail-closed = F-1 |
| Gate shown-once set | `middlewares.py:39` | `self._gate_shown: set[int]` | RAM-only (F-4, F-12) |
| Gate cleared flag | `migrations.py:230` | `users.gate_cleared INT DEFAULT 0` | reset by `reset_user_gates` |
| channels.type | `migrations.py:231` | `TEXT DEFAULT 'channel'` | discriminates channel/group |
| channels PK/unique | `migrations.py:119` | `channel_id INTEGER NOT NULL UNIQUE` | duplicate add → IntegrityError (F-9) |
| channels timestamps | `migrations.py:117-123` | **none** | no created_at/updated_at (F-11) |
| audit on channel change | `channels.py:138-220` | **none** | no `repo.audit` calls (F-11) |
