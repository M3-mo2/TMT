"""Callback-data factory roundtrips (pack/unpack, defaults, ownership ids)."""

from __future__ import annotations

import pytest

from app.bot.callbacks import AccountCB, JobCB, MenuCB, TransferCB, TicketCB


@pytest.mark.parametrize(
    "factory,kwargs",
    [
        (MenuCB, {"action": "main"}),
        (MenuCB, {"action": "accounts"}),
        (MenuCB, {"action": "transfers"}),
        (MenuCB, {"action": "help"}),
        (MenuCB, {"action": "tickets"}),
        (AccountCB, {"action": "list"}),
        (AccountCB, {"action": "view", "account_id": 12}),
        (AccountCB, {"action": "delete", "account_id": 34}),
        (AccountCB, {"action": "cancel_login"}),
        (TransferCB, {"action": "new"}),
        (TransferCB, {"action": "pick_account", "account_id": 7}),
        (TransferCB, {"action": "confirm", "account_id": 7}),
        (TransferCB, {"action": "refresh", "account_id": 7}),
        (TransferCB, {"action": "cancel_wizard"}),
        (JobCB, {"action": "list"}),
        (JobCB, {"action": "view", "job_id": 99}),
        (JobCB, {"action": "cancel", "job_id": 99}),
        (TicketCB, {"action": "view", "ticket_id": 1}),
        (TicketCB, {"action": "create"}),
        (TicketCB, {"action": "reply", "ticket_id": 5}),
        (TicketCB, {"action": "confirm_create"}),
        (TicketCB, {"action": "confirm_reply", "ticket_id": 3}),
        (TicketCB, {"action": "back", "ticket_id": 7}),
    ],
)
def test_roundtrip(factory, kwargs) -> None:
    cb = factory(**kwargs)
    unpacked = factory.unpack(cb.pack())
    assert unpacked == cb


def test_defaults_are_zero() -> None:
    assert AccountCB(action="add").account_id == 0
    assert JobCB(action="list").job_id == 0
    assert TicketCB(action="list").ticket_id == 0


def test_prefixes_are_distinct() -> None:
    assert MenuCB(action="main").pack().startswith("menu:")
    assert AccountCB(action="list").pack().startswith("acct:")
    assert TransferCB(action="new").pack().startswith("xfer:")
    assert JobCB(action="list").pack().startswith("job:")
    assert TicketCB(action="view").pack().startswith("tkt:")


def test_packed_length_within_telegram_limit() -> None:
    for cb in (
        AccountCB(action="delete_confirm", account_id=10**12),
        TransferCB(action="pick_account", account_id=10**12),
        JobCB(action="cancel", job_id=10**12),
        TicketCB(action="confirm_reply", ticket_id=10**12),
    ):
        assert len(cb.pack()) <= 64
