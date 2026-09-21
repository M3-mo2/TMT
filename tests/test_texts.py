"""Arabic UI contract tests: no emoji anywhere, HTML escaping, renderers."""

from __future__ import annotations

import re

import pytest

import app.bot.texts as texts
from app.core.models import (
    Account,
    AccountStatus,
    Check,
    CheckStatus,
    Job,
    JobStatus,
    UserStats,
)

# Emoji / pictographic ranges + variation selectors + ZWJ sequences.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0000FE00-\U0000FE0F\U0000200D\U00002B00-\U00002BFF\U0001F900-\U0001F9FF]"
)

_ALLOWED_SYMBOLS = set("✓!?×›=•#—…|←👤🔰💳✅✨↓⇐―⇜⤾𑗁📱≡📚🔐⤸⋆⟡↢🏷🎫")


def _iter_public_strings() -> list[str]:
    out = []
    for name in texts.__all__:
        value = getattr(texts, name)
        if isinstance(value, str):
            out.append(value)
    return out


def test_no_emoji_in_any_exported_string() -> None:
    # Strip approved decorative symbols before checking for stray emoji.
    _APPROVED = "👤🔰💳✅✨📚🔐🏷🎫"
    for value in _iter_public_strings():
        stripped = "".join(ch for ch in value if ch not in _APPROVED)
        assert not _EMOJI.search(stripped), f"emoji found in {value!r}"


def test_only_allowed_inline_symbols() -> None:
    for value in _iter_public_strings():
        for ch in value:
            if ord(ch) > 0x2000 and not ch.isalnum() and not ch.isspace():
                assert ch in _ALLOWED_SYMBOLS, f"unexpected symbol {ch!r} in {value!r}"


def test_render_preflight_glyph_mapping() -> None:
    checks = [
        Check("a", CheckStatus.PASS, "msg-a"),
        Check("b", CheckStatus.WARN, "msg-b"),
        Check("c", CheckStatus.FAIL, "msg-c"),
        Check("d", CheckStatus.UNKNOWN, "msg-d"),
        Check("e", CheckStatus.INFO, "msg-e"),
    ]
    rendered = texts.render_preflight(checks)
    assert rendered.startswith(texts.PREFLIGHT_TITLE)
    assert "✓ msg-a" in rendered
    assert "! msg-b" in rendered
    assert "× msg-c" in rendered
    assert "? msg-d" in rendered
    assert "› msg-e" in rendered


def test_esc_neutralizes_html() -> None:
    esc = texts.esc
    assert esc('<b>&"\'</b>') == "&lt;b&gt;&amp;&quot;&#x27;&lt;/b&gt;"
    assert texts.esc("<script>") == "&lt;script&gt;"


def test_mask_phone_keeps_country_and_last_four() -> None:
    assert texts.mask_phone("+201234567890") == "+20••••••7890"
    assert texts.mask_phone("+12025550123") == "+12•••••0123"
    assert texts.mask_phone("+1234") == "••••"
    assert texts.mask_phone("+201234567890") == "+20" + "•" * 6 + "7890"


def test_normalize_phone_strips_spaces_and_punctuation() -> None:
    assert texts.normalize_phone("+20 10 0307 2694") == "+201003072694"
    assert texts.normalize_phone("+1 (202) 555-0123") == "+12025550123"
    assert texts.normalize_phone("  +44 7700 900123  ") == "+447700900123"
    assert texts.normalize_phone("+20-10-0307-2694") == "+201003072694"
    assert texts.normalize_phone("201003072694") == "201003072694"
    assert texts.normalize_phone("invalid") == ""
    assert texts.normalize_phone("+20 10 0307 2694 ") == "+201003072694"


def _account() -> Account:
    return Account(
        id=1,
        owner_id=10,
        phone="+201234567890",
        tg_user_id=777,
        tg_username="some<one>",
        display_name="Test <User>",
        status=AccountStatus.ACTIVE,
        limited_until=None,
        added_at="2026-01-01T00:00:00Z",
        last_validated_at=None,
    )


def _job(status: JobStatus = JobStatus.RUNNING) -> Job:
    return Job(
        id=3,
        owner_id=10,
        account_id=1,
        source_ref="@src",
        dest_ref="@dst",
        source_title="Source <Group>",
        dest_title="Dest Group",
        status=status,
        total=100,
        invited=10,
        skipped=5,
        failed=1,
        skip_reasons={"privacy": 3, "bot": 2},
        error="boom <script>",
    )


def test_render_account_card_escapes_and_masks() -> None:
    card = texts.render_account_card(_account())
    assert "Test &lt;User&gt;" in card
    assert "@some&lt;one&gt;" in card
    assert "+20••••••7890" in card
    assert texts.account_status_label(AccountStatus.ACTIVE) in card


def test_render_job_card_shows_counts_reasons_and_error() -> None:
    card = texts.render_job_card(_job(), "My Account")
    assert "#3" in card
    assert "My Account" in card
    assert "✅|تم اضافه ↼" in card
    assert "10" in card
    assert "السبب: boom &lt;script&gt;" in card
    assert texts.skip_reason_label("privacy") in card


def test_render_job_card_deleted_account() -> None:
    card = texts.render_job_card(_job(), None)
    assert texts.M_DELETED_ACCOUNT in card


def test_render_jobs_list_glyphs() -> None:
    listing = texts.render_jobs_list([_job(JobStatus.COMPLETED), _job(JobStatus.FAILED)])
    assert texts.job_glyph(JobStatus.COMPLETED) in listing
    assert texts.job_glyph(JobStatus.FAILED) in listing


def test_render_progress_card_wait_and_note() -> None:
    card = texts.render_progress_card(9, "waiting", 5, 100, 3, 1, 1, wait_left=42, note="ملاحظة")
    assert "5/100" in card
    assert "42" in card
    assert "ملاحظة" in card
    assert texts.phase_label("waiting") in card


def test_render_final_summary_error_and_reasons() -> None:
    summary = texts.render_final_summary(
        9, JobStatus.FAILED, 0, 0, 0, "انتهت المدة", {"already_member": 7}
    )
    assert "#9" in summary
    assert "✨|العمليه تمت" in summary
    assert "السبب: انتهت المدة" in summary
    assert texts.skip_reason_label("already_member") in summary


def test_button_labels_are_short() -> None:
    for value in _iter_public_strings():
        if name := next(
            (n for n in texts.__all__ if n.startswith("BUT_") and getattr(texts, n) == value),
            None,
        ):
            assert len(value) <= 20, f"button too long: {name}"


def test_render_settings_shows_values_and_summary() -> None:
    values = {
        "max_members_per_job": 1500,
        "invite_delay_seconds": 2,
        "flood_wait_max_seconds": 900,
        "job_timeout_seconds": 14400,
    }
    rendered = texts.render_settings(values=values)
    assert texts.M_SETTINGS_TITLE in rendered
    assert "1500" in rendered
    assert texts.M_SETTINGS_SUMMARY in rendered


def test_render_admin_menu_shows_welcome_and_stats() -> None:
    rendered = texts.render_admin_menu(12, 45, 7)
    assert rendered.startswith("أهلا بك في لوحه التحكم .")
    assert "<code>12</code>" in rendered
    assert "<code>45</code>" in rendered
    assert "<code>7</code>" in rendered
    assert "استخدم الازرار للتنقل ↓" in rendered
    assert "―" * 20 in rendered


def test_render_user_card_shows_details_and_stats() -> None:
    user = {
        "id": 1001, "first_name": "سارة", "last_name": "محمد", "username": "sarah",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-09-07T12:00:00Z",
        "is_blocked": 0,
    }
    acc = Account(
        id=1, owner_id=1001, phone="+15550001111", tg_user_id=77, tg_username="acct1",
        display_name="Acct 1", status=AccountStatus.ACTIVE, limited_until=None,
        added_at="t", last_validated_at=None,
    )
    stats = UserStats(accounts=1, jobs=3, completed=2, failed=1, active=0)
    rendered = texts.render_user_card(user, [acc], stats)
    assert "سارة محمد" in rendered
    assert "@sarah" in rendered
    assert "<code>1001</code>" in rendered
    assert "نشط" in rendered
    assert "2026-09-07" in rendered
    assert "<code>2</code>" in rendered   # completed
    assert "<code>1</code>" in rendered   # failed
    assert "استخدم الازرار للتنقل ↓" in rendered
    assert "✿" not in rendered          # card uses approved glyphs, not the account-card flourish


def test_users_list_kb_labels_by_name_and_paginates() -> None:
    from app.bot.routers.admin import callbacks as C
    from app.bot.routers.admin.keyboards import users_list_kb
    users = [
        {"id": 10, "first_name": "سارة", "last_name": "محمد", "username": "sarah"},
        {"id": 11, "first_name": "محمد", "last_name": None, "username": None},
        {"id": 12, "first_name": None, "last_name": None, "username": None},
    ]
    kb = users_list_kb(users, page=0, total_pages=3)
    rows = kb.inline_keyboard
    # buttons open the detail card, labeled by name — never a bare id
    assert rows[0][0].callback_data == f"{C.USER_OPEN}10"
    assert "سارة" in rows[0][0].text and "@sarah" in rows[0][0].text
    assert rows[1][0].callback_data == f"{C.USER_OPEN}11"
    assert "محمد" in rows[1][0].text and "11" not in rows[1][0].text.replace("#", "")
    assert rows[2][0].callback_data == f"{C.USER_OPEN}12"  # only id available -> #id fallback
    # page 0 of 3: a "Next" control exists, and the Back button returns to the menu
    nav_labels = [b.text for b in rows[-2]]
    assert "التالي" in " ".join(nav_labels)
    assert rows[-1][0].callback_data == C.MENU


def test_user_detail_kb_exposes_controls_per_account() -> None:
    from app.bot.routers.admin import callbacks as C
    from app.bot.routers.admin.keyboards import user_detail_kb
    user = {"id": 5, "is_blocked": False}
    accounts = [
        {"id": 1, "tg_user_id": 77, "tg_username": "acct1"},
        {"id": 2, "tg_user_id": 78, "tg_username": None},
    ]
    kb = user_detail_kb(user, accounts)
    rows = kb.inline_keyboard
    assert rows[0][0].callback_data == f"{C.USER_DEL_ACC_CONFIRM}1"
    assert "حذف" in rows[0][0].text
    assert rows[1][0].callback_data == f"{C.USER_DEL_ACC_CONFIRM}2"
    actions = [b.callback_data for b in rows[2]]
    assert f"{C.USER_TOGGLE_BLOCK}5" in actions
    assert f"{C.USER_NOTIFY}5" in actions
    assert rows[3][0].callback_data == C.USERS


def test_render_gate_screen_lists_mandatory_entries() -> None:
    mandatory = [
        {"title": "قناة أ", "invite_link": "https://t.me/a", "type": "channel"},
        {"title": "<script>evil</script>", "invite_link": "https://t.me/b", "type": "group"},
    ]
    rendered = texts.render_gate_screen(mandatory)
    assert rendered.startswith(texts.M_GATE_BLOCKED)
    assert "قناة أ" in rendered
    assert "<script>" not in rendered  # escaped
    assert "&lt;script&gt;" in rendered
    assert texts.M_GATE_PLEASE_VERIFY in rendered


def test_render_entries_list_empty_and_populated() -> None:
    assert texts.render_entries_list([], "channel") == texts.M_NO_CHANNELS
    assert texts.render_entries_list([], "group") == texts.M_NO_GROUPS
    entries = [
        {"title": "Ch", "invite_link": "l", "type": "channel", "is_active": 1},
        {"title": "Grp", "invite_link": "l2", "type": "group", "is_active": 0},
    ]
    rendered = texts.render_entries_list(entries, "channel")
    assert "🔄" not in rendered  # no toggle glyph in list text
    assert "Ch" in rendered


def test_gate_callback_data_round_trips() -> None:
    from app.bot.callbacks import GateCB
    packed = GateCB(action="verify").pack()
    assert packed == "gate:verify"


# ---------------------------------------------------------------- notifications


def test_notification_event_label_arabic() -> None:
    assert texts.notification_event_label("user_joined") == "مستخدم جديد"
    assert texts.notification_event_label("job_failed") == "فشلت عملية نقل"
    assert texts.notification_event_label("broadcast_completed") == "اكتمل البث"
    assert texts.notification_event_label("unknown_type") == "unknown_type"


def test_notification_severity_label_arabic() -> None:
    assert texts.notification_severity_label("info") == "معلومات"
    assert texts.notification_severity_label("warning") == "تحذير"
    assert texts.notification_severity_label("error") == "خطأ"


def test_render_notifications_list_empty() -> None:
    rendered = texts.render_notifications_list([], 0)
    assert texts.M_NOTIFY_TITLE in rendered
    assert texts.M_NOTIFY_EMPTY in rendered


def test_render_notifications_list_with_items() -> None:
    notifs = [
        {"id": 1, "title": "joined", "event_type": "user_joined",
         "severity": "info", "body": "user 42", "read_at": None,
         "created_at": "2026-01-01T00:00:00Z"},
        {"id": 2, "title": "failed", "event_type": "job_failed",
         "severity": "error", "body": "boom", "read_at": "2026-01-01T01:00:00Z",
         "created_at": "2026-01-01T00:00:00Z"},
    ]
    rendered = texts.render_notifications_list(notifs, 2)
    assert texts.M_NOTIFY_TITLE in rendered
    assert "lDadY" not in rendered  # ensure no raw encoding artifact
    assert "joined" in rendered
    assert "failed" in rendered
    # Unread count badge
    assert "<code>2</code>" in rendered


def test_render_notification_card() -> None:
    notif = {
        "id": 1, "title": "test title", "event_type": "user_joined",
        "severity": "info", "body": "user details",
        "created_at": "2026-01-01T00:00:00Z", "read_at": None,
        "dismissed": 0,
    }
    card = texts.render_notification_card(notif)
    assert "test title" in card
    assert "مستخدم جديد" in card  # event label
    assert "user details" in card


def test_render_notify_settings_shows_all_types() -> None:
    admin_ids = [12345, 67890]
    enabled = ["user_joined", "job_failed", "job_completed"]
    rendered = texts.render_notify_settings(admin_ids, enabled)
    assert texts.M_NOTIFY_SETTINGS_TITLE in rendered
    assert "<code>2</code>" in rendered  # two admins
    # Each event type should appear
    assert "user_joined" in rendered
    assert "job_failed" in rendered
    assert "job_completed" in rendered
    # Enabled ones marked ✓, disabled marked ×
    assert "✓" in rendered


# ---------------------------------------------------------------- tickets


def test_ticket_status_labels_arabic() -> None:
    assert texts.ticket_status_label("open") == "مفتوحة"
    assert texts.ticket_status_label("in_progress") == "قيد المعالجة"
    assert texts.ticket_status_label("resolved") == "تم الحل"
    assert texts.ticket_status_label("closed") == "مغلقة"
    assert texts.ticket_status_label("unknown") == "unknown"  # fallback


def test_ticket_priority_labels_arabic() -> None:
    assert texts.ticket_priority_label("low") == "منخفضة"
    assert texts.ticket_priority_label("normal") == "عادية"
    assert texts.ticket_priority_label("high") == "عالية"
    assert texts.ticket_priority_label("unknown") == "unknown"


def test_ticket_status_glyphs() -> None:
    assert texts.ticket_status_glyph("open") == texts.GLYPH_PASS
    assert texts.ticket_status_glyph("in_progress") == texts.GLYPH_INFO
    assert texts.ticket_status_glyph("resolved") == texts.GLYPH_PASS
    assert texts.ticket_status_glyph("closed") == texts.GLYPH_NEUTRAL
    assert texts.ticket_status_glyph("unknown") == texts.GLYPH_UNKNOWN


def test_render_tickets_list_empty() -> None:
    rendered = texts.render_tickets_list([])
    assert texts.M_TICKETS_EMPTY in rendered
    assert "🎫" in rendered


def test_render_tickets_list_with_items() -> None:
    tickets = [
        {"id": 1, "subject": "Bug <x>", "priority": "high", "status": "open"},
        {"id": 2, "subject": "Feature", "priority": "low", "status": "in_progress"},
    ]
    rendered = texts.render_tickets_list(tickets)
    assert "#1" in rendered
    assert "#2" in rendered
    assert "Bug &lt;x&gt;" in rendered  # escaped
    assert "Feature" in rendered
    assert texts.ticket_status_label("open") in rendered
    assert texts.ticket_status_label("in_progress") in rendered
    assert texts.ticket_priority_label("high") in rendered
    assert texts.ticket_priority_label("low") in rendered


def test_render_ticket_detail_with_messages() -> None:
    ticket = {"id": 1, "subject": "Test <sub>", "priority": "normal", "status": "open"}
    messages = [
        {"sender_role": "user", "body": "Hello <admin>"},
        {"sender_role": "admin", "body": "Hi there <user>"},
    ]
    rendered = texts.render_ticket_detail(ticket, messages)
    assert "#1" in rendered
    assert "Test &lt;sub&gt;" in rendered
    assert "Hello &lt;admin&gt;" in rendered
    assert "Hi there &lt;user&gt;" in rendered
    assert texts.ticket_status_label("open") in rendered
    assert texts.ticket_priority_label("normal") in rendered


def test_render_ticket_detail_empty_thread() -> None:
    ticket = {"id": 1, "subject": "New", "priority": "high", "status": "open"}
    rendered = texts.render_ticket_detail(ticket, [])
    assert "#1" in rendered
    assert "لا توجد رسائل" in rendered


def test_render_ticket_card() -> None:
    ticket = {"id": 5, "subject": "Bug", "priority": "high", "status": "closed", "owner_id": 42}
    card = texts.render_ticket_card(ticket)
    assert "#5" in card
    assert "Bug" in card
    assert str(42) in card
    assert texts.ticket_status_label("closed") in card
    assert texts.ticket_priority_label("high") in card


def test_render_admin_tickets_list_empty() -> None:
    rendered = texts.render_admin_tickets_list([], status_filter=None)
    assert "لا توجد تذاكر" in rendered


def test_render_admin_tickets_list_with_items() -> None:
    tickets = [
        {"id": 1, "subject": "Ticket <one>", "priority": "low", "status": "open", "owner_id": 10},
    ]
    rendered = texts.render_admin_tickets_list(tickets, status_filter=None)
    assert "#1" in rendered
    assert "Ticket &lt;one&gt;" in rendered
    assert str(10) in rendered


def test_main_menu_has_tickets_button() -> None:
    from app.bot.keyboards import main_menu
    kb = main_menu()
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert texts.BUT_TICKETS in flat


def test_tickets_list_kb_structure() -> None:
    from app.bot.callbacks import TicketCB, MenuCB
    from app.bot.keyboards import tickets_list_kb
    tickets = [{"id": 1}, {"id": 2}]
    kb = tickets_list_kb(tickets)
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert TicketCB(action="view", ticket_id=1).pack() in flat
    assert TicketCB(action="view", ticket_id=2).pack() in flat
    assert TicketCB(action="create").pack() in flat
    assert MenuCB(action="main").pack() in flat


def test_ticket_detail_kb_has_back() -> None:
    from app.bot.callbacks import TicketCB
    from app.bot.keyboards import ticket_detail_kb
    kb = ticket_detail_kb(42)
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert TicketCB(action="back", ticket_id=42).pack() in flat


def test_ticket_confirm_create_kb() -> None:
    from app.bot.callbacks import TicketCB
    from app.bot.keyboards import ticket_confirm_create_kb
    kb = ticket_confirm_create_kb()
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert TicketCB(action="confirm_create").pack() in flat
    assert TicketCB(action="back").pack() in flat


def test_ticket_confirm_reply_kb() -> None:
    from app.bot.callbacks import TicketCB
    from app.bot.keyboards import ticket_confirm_reply_kb
    kb = ticket_confirm_reply_kb(7)
    flat = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert TicketCB(action="confirm_reply", ticket_id=7).pack() in flat
    assert TicketCB(action="back", ticket_id=7).pack() in flat
