from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
import sqlite3

import pytest

from core.db import Database, SCHEMA, utc_iso
from core.rounds import PreparedQuestion, RoundError
from test_rounds import T0, answer, service, start


@pytest.fixture
async def decision(service):
    record = start(service)
    await answer(service, record, reason="ORIGINAL_PRIVATE_REASON")
    service.freeze_ready(record.id, T0 + timedelta(days=1))
    return service.outcome(record.id, 200, 102, 0, T0 + timedelta(days=1, minutes=1))


@pytest.mark.parametrize("new_option", [None, " Online customers "])
async def test_reopen_preserves_original_and_starts_fresh_with_current_members(service, decision, new_option):
    original_responses = service.db.round_responses(decision.id)
    service.provider.complete = AsyncMock()
    when = T0 + timedelta(days=2)
    reopened = service.reopen(decision.id, 200, 105, ("105", "106"), when, new_option=new_option)
    assert reopened.id != decision.id and reopened.reopened_from == decision.id
    assert reopened.question == decision.question
    assert reopened.options == decision.options + (("Online customers",) if new_option else ())
    assert reopened.status == "collecting" and reopened.outcome is None and reopened.outcome_at is None
    assert reopened.closed_at is None and reopened.owner_user_id is None
    assert reopened.created_at == utc_iso(when) and reopened.deadline_at == utc_iso(when + timedelta(days=1))
    assert reopened.created_by == "105" and service.progress(reopened.id) == (0, 2)
    assert [member.user_id for member in service.db.round_members(reopened.id)] == ["105", "106"]
    assert all(not member.reminded for member in service.db.round_members(reopened.id))
    assert not service.db.round_responses(reopened.id)
    assert service.db.get_round(decision.id) == decision
    assert service.db.round_responses(decision.id) == original_responses
    service.provider.complete.assert_not_awaited()


@pytest.mark.parametrize("option,error", [
    ("Campus", "new_option"), (" campus ", "new_option"),
    ("None of these", "new_option"), ("ANOTHER OPTION", "new_option"),
    ("", "input"), (" ", "input"), ("x" * 81, "input"),
    ("Write our report", "boundary"),
])
async def test_invalid_new_options_leave_decision_untouched(service, decision, option, error):
    with pytest.raises(RoundError, match=error):
        service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2), new_option=option)
    assert service.db.get_round(decision.id) == decision
    assert service.db.get_round(decision.id + 1) is None


@pytest.mark.parametrize("state", ["collecting", "cancelled", "foreign", "missing", "paused", "setup"])
async def test_reopen_requires_a_closed_decision_in_the_active_channel(service, decision, state):
    round_id, channel_id = decision.id, 200
    if state in {"collecting", "cancelled"}:
        record = start(service)
        round_id = record.id
        if state == "cancelled":
            service.db.cancel_round(round_id)
    elif state == "foreign":
        channel_id = 201
        service.db.ensure_channel(201, 100)
        service.db.enable_channel(201)
    elif state == "missing":
        round_id = 999
    elif state == "paused":
        service.db.set_paused(200, True)
    else:
        channel_id = 999
    with pytest.raises(RoundError):
        service.reopen(round_id, channel_id, 102, ("102",), T0 + timedelta(days=2))
    assert service.db.get_round(decision.id).outcome == "Campus"


async def test_repeat_clicks_and_database_race_cannot_create_duplicate_active_reopenings(service, decision, monkeypatch):
    child = service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2))
    with pytest.raises(RoundError, match="reopened"):
        service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2))
    monkeypatch.setattr(service.db, "active_reopening", MagicMock(side_effect=[None, child.id]))
    with pytest.raises(RoundError, match="reopened"):
        service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2))
    assert service.db.get_round(child.id + 1) is None
    assert service.progress(child.id) == (0, 1)


async def test_cancelled_reopening_can_be_retried_and_closed_child_can_be_reopened(service, decision):
    first = service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2))
    service.db.cancel_round(first.id)
    child = service.reopen(decision.id, 200, 102, ("102",), T0 + timedelta(days=2))
    service.freeze_ready(child.id, T0 + timedelta(days=3))
    grandchild = service.reopen(child.id, 200, 102, ("102",), T0 + timedelta(days=4), new_option="Online customers")
    assert grandchild.reopened_from == child.id
    assert grandchild.options == decision.options + ("Online customers",)


@pytest.mark.parametrize("prior_answers", [(), ("Weekly meetings",), ("Weekly meetings", "Async updates")])
async def test_open_ended_revote_uses_prior_answers_as_choices(service, prior_answers):
    source = service.start(200, 102, PreparedQuestion("How should we meet?", ()), ("102", "103", "104"), T0)
    for index, label in enumerate(prior_answers):
        await service.save_response(
            round_id=source.id, channel_id=200, user_id=102 + index, selection=0,
            answer=label, reason="A member's reason.", show_name=False, now=T0,
        )
    service.freeze_ready(source.id, T0 + timedelta(days=1))
    child = service.reopen(source.id, 200, 102, ("102",), T0 + timedelta(days=2))
    assert child.options == prior_answers
    assert not service.db.round_responses(child.id)


async def test_reopening_supports_five_or_more_choices_but_reserves_an_outcome_button(service):
    source = service.start(200, 102, PreparedQuestion("Which approach?", tuple(f"Option {i}" for i in range(24))), ("102",), T0)
    service.freeze_ready(source.id, T0 + timedelta(days=1))
    with pytest.raises(RoundError, match="option_capacity"):
        service.reopen(source.id, 200, 102, ("102",), T0 + timedelta(days=2), new_option="One more")
    child = service.reopen(source.id, 200, 102, ("102",), T0 + timedelta(days=2))
    assert len(child.options) == 24


def test_history_paginates_all_closed_decisions_and_clamps_pages(service):
    assert service.history(200).entries == ()
    for index in range(23):
        record = start(service)
        service.freeze_ready(record.id, T0 + timedelta(days=1, minutes=index))
    start(service)  # A collecting round must not appear.
    first, second, third = (service.history(200, page) for page in (0, 1, 2))
    assert [entry.id for entry in first.entries] == list(range(23, 13, -1))
    assert [entry.id for entry in second.entries] == list(range(13, 3, -1))
    assert [entry.id for entry in third.entries] == [3, 2, 1]
    assert all(page.total_pages == 3 for page in (first, second, third))
    assert service.history(200, 999) == third and service.history(200, -1) == first
    assert service.log(200) == first.entries
    assert set(vars(first.entries[0])) == {"id", "question", "outcome", "owner_user_id", "closed_at"}


def test_legacy_database_migrates_in_memory_without_losing_decisions(monkeypatch):
    connection = sqlite3.connect(":memory:")
    legacy_schema = SCHEMA.replace("outcome_at TEXT,\n    reopened_from INTEGER REFERENCES rounds(id)", "outcome_at TEXT")
    connection.executescript(legacy_schema)
    connection.execute("INSERT INTO channels (channel_id,guild_id,enabled) VALUES ('200','100',1)")
    connection.execute(
        """INSERT INTO rounds (id,channel_id,question,options_json,status,created_by,created_at,deadline_at,closed_at,outcome,outcome_at)
        VALUES (1,'200','Which meeting?','["Monday","Tuesday"]','closed','102',?,?,?,'Monday',?)""",
        (utc_iso(T0), utc_iso(T0), utc_iso(T0), utc_iso(T0)),
    )
    connection.commit()
    monkeypatch.setattr("core.db.sqlite3.connect", lambda path: connection)
    db = Database(":memory:")
    try:
        before = db.get_round(1)
        assert before.reopened_from is None and before.outcome == "Monday"
        Database(":memory:")  # Migration and index creation are idempotent.
        assert db.get_round(1) == before
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='one_active_reopening'").fetchone()
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        db.close()
