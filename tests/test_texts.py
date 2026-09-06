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
)

# Emoji / pictographic ranges + variation selectors + ZWJ sequences.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0000FE00-\U0000FE0F\U0000200D\U00002B00-\U00002BFF\U0001F900-\U0001F9FF]"
)

_ALLOWED_SYMBOLS = set("✓!?×›=•#—…|←")


def _iter_public_strings() -> list[str]:
    out = []
    for name in texts.__all__:
        value = getattr(texts, name)
        if isinstance(value, str):
            out.append(value)
    return out


def test_no_emoji_in_any_exported_string() -> None:
    for value in _iter_public_strings():
        assert not _EMOJI.search(value), f"emoji found in {value!r}"


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
    assert "دُعي: 10" in card
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
    assert "تم: 5/100" in card
    assert "42" in card
    assert "ملاحظة" in card
    assert texts.phase_label("waiting") in card


def test_render_final_summary_error_and_reasons() -> None:
    summary = texts.render_final_summary(
        9, JobStatus.FAILED, 0, 0, 0, "انتهت المدة", {"already_member": 7}
    )
    assert "انتهت العملية" in summary
    assert "انتهت المدة" in summary
    assert texts.skip_reason_label("already_member") in summary


def test_button_labels_are_short() -> None:
    for value in _iter_public_strings():
        if name := next(
            (n for n in texts.__all__ if n.startswith("BUT_") and getattr(texts, n) == value),
            None,
        ):
            assert len(value) <= 20, f"button too long: {name}"
