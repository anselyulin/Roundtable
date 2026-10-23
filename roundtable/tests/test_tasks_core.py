from dataclasses import asdict
from datetime import timedelta
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from core import prompts
from core.db import Database, SCHEMA, utc_iso
from core.llm import FakeProvider
from core.rounds import PreparedQuestion, RoundError, RoundService
from core.tasks import TaskService, date_end
from test_rounds import T0


MEMBERS = ("102", "103", "104")


@pytest.fixture
def tasks(db):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    return TaskService(db, FakeProvider())


async def proposed(service, *, titles=("Compare market choices",), due=None, now=T0):
    added = tuple(service.add(200, 102, title, due, now) for title in titles)
    prepared = await service.prepare_assign(200, MEMBERS, now)
    batch = service.propose(200, prepared, MEMBERS, now)
    service.db.publish_task_batch(200, batch, 300)
    return added, batch


async def test_tasks_start_unowned_and_suggestions_need_explicit_confirmation(tasks):
    added, batch = await proposed(tasks, titles=("Compare markets", "Arrange review", "Check scope", "Schedule discussion"))
    assert all(task.status == "proposed" and task.owner_user_id is None and task.done_at is None for task in added)
    suggested = tasks.db.task_batch(200, batch)
    assert [task.owner_user_id for task in suggested] == ["102", "103", "104", "102"]
    assert all(task.status == "proposed" and task.done_at is None for task in suggested)
    with pytest.raises(RoundError, match="task_done"):
        tasks.done(200, added[0].id, T0)
    tasks.confirm(200, batch, added[0].id, "102", MEMBERS, T0)
    assert tasks.db.get_task(added[0].id).status == "confirmed"
    assert tasks.db.get_task(added[1].id).status == "proposed"
    tasks.confirm(200, batch, 0, None, MEMBERS, T0)
    assert all(task.status == "confirmed" for task in tasks.db.channel_tasks(200))
    with pytest.raises(RoundError, match="task_proposal"):
        tasks.confirm(200, batch, 0, None, MEMBERS, T0)


async def test_assign_payload_contains_only_tasks_counts_due_and_private_preferences(tasks):
    tasks.prefs(200, 102, "I prefer organizing team meetings")
    tasks.prefs(200, 103, "I prefer comparing choices")
    tasks.db.ensure_channel(201, 100)
    tasks.db.set_preference(201, 102, "OTHER_CHANNEL_SECRET")
    tasks.add(200, 102, "Compare markets", "2026-12-31", T0)
    original = tasks.provider.complete
    tasks.provider.complete = AsyncMock(wraps=original)
    prepared = await tasks.prepare_assign(200, MEMBERS, T0)
    args = tasks.provider.complete.call_args.kwargs
    assert args["system"] == prompts.PROPOSE_OWNERS
    payload = json.loads(args["user"])
    assert set(payload) == {"tasks", "members"}
    assert set(payload["tasks"][0]) == {"task_id", "title", "due_at"}
    assert set(payload["members"][0]) == {"user_id", "task_count", "preference_note"}
    assert payload["members"][0]["preference_note"] == "I prefer organizing team meetings"
    assert "OTHER_CHANNEL_SECRET" not in args["user"]
    assert "I prefer" not in repr(prepared)


@pytest.mark.parametrize("invalid", [
    "PRIVATE_INVALID_OUTPUT",
    '{"assignments":[{"task_id":999,"user_id":"102","reason":"Current counts"}]}',
    '{"assignments":[{"task_id":1,"user_id":"999","reason":"Current counts"}]}',
    '{"assignments":[]}',
    json.dumps({"assignments": [{"task_id": 1, "user_id": "102", "reason": "word " * 13}]}),
    json.dumps({"assignments": [{"task_id": 1, "user_id": "102", "reason": "Current counts"}] * 2}),
])
async def test_invalid_or_out_of_scope_assignments_retry_once_without_writes(tasks, invalid, caplog):
    tasks.add(200, 102, "Compare markets", None, T0)
    tasks.provider.complete = AsyncMock(return_value=invalid)
    with pytest.raises(RoundError, match="provider"):
        await tasks.prepare_assign(200, MEMBERS, T0)
    assert tasks.provider.complete.await_count == 2
    assert tasks.db.get_task(1).owner_user_id is None
    assert not caplog.text


async def test_invalid_assignment_can_recover_on_single_retry(tasks):
    tasks.add(200, 102, "Compare markets", None, T0)
    valid = json.dumps({"assignments": [{"task_id": 1, "user_id": "103", "reason": "Balances current counts"}]})
    tasks.provider.complete = AsyncMock(side_effect=["INVALID", valid])
    assert (await tasks.prepare_assign(200, MEMBERS, T0)).assignments == ((1, "103"),)
    assert tasks.provider.complete.await_count == 2


@pytest.mark.parametrize("change", ["task", "prefs", "members", "pause"])
async def test_stale_plan_cannot_overwrite_changed_state(tasks, change):
    tasks.add(200, 102, "Compare markets", None, T0)
    prepared = await tasks.prepare_assign(200, MEMBERS, T0)
    members = MEMBERS
    if change == "task":
        tasks.db.connection.execute("UPDATE tasks SET owner_user_id='104' WHERE id=1")
    elif change == "prefs":
        tasks.prefs(200, 102, "I prefer organizing")
    elif change == "members":
        members = MEMBERS[:2]
    else:
        tasks.db.set_paused(200, True)
    with pytest.raises(RoundError):
        tasks.propose(200, prepared, members, T0)
    assert tasks.db.get_task(1).proposal_batch is None


async def test_swap_is_only_a_proposal_and_does_not_extend_twenty_four_hour_expiry(tasks):
    added, batch = await proposed(tasks)
    tasks.swap(200, batch, added[0].id, "103", MEMBERS, T0 + timedelta(hours=23))
    task = tasks.db.get_task(added[0].id)
    assert task.status == "proposed" and task.owner_user_id == "103" and task.proposed_at == utc_iso(T0)
    with pytest.raises(RoundError, match="task_stale"):
        tasks.confirm(200, batch, task.id, "102", MEMBERS, T0)
    tasks.expire(200, T0 + timedelta(hours=24))
    assert tasks.db.get_task(task.id).owner_user_id is None
    with pytest.raises(RoundError, match="task_proposal"):
        tasks.confirm(200, batch, task.id, "103", MEMBERS, T0 + timedelta(hours=24))
    assert (await tasks.prepare_assign(200, MEMBERS, T0 + timedelta(hours=24))).tasks[0].id == task.id


async def test_confirmation_cannot_assign_hidden_members_and_other_channels_cannot_change_tasks(tasks):
    added, batch = await proposed(tasks)
    with pytest.raises(RoundError, match="task_stale"):
        tasks.confirm(200, batch, 0, None, MEMBERS[1:], T0)
    with pytest.raises(RoundError, match="task_members"):
        tasks.swap(200, batch, 1, "999", MEMBERS, T0)
    tasks.db.ensure_channel(201, 100)
    tasks.db.enable_channel(201)
    with pytest.raises(RoundError, match="task_proposal"):
        tasks.confirm(201, batch, 1, "102", MEMBERS, T0)
    with pytest.raises(RoundError, match="task"):
        tasks.done(201, added[0].id, T0)


@pytest.mark.parametrize("due,delta,counter", [
    (None, timedelta(days=500), "tasks_done_on_time"),
    ("2026-12-31", timedelta(microseconds=-1), "tasks_done_on_time"),
    ("2026-12-31", timedelta(0), "tasks_done_on_time"),
    ("2026-12-31", timedelta(microseconds=1), "tasks_done_late"),
])
async def test_done_timestamp_and_boundary_counters_are_exactly_once(tasks, due, delta, counter):
    added, batch = await proposed(tasks, due=due)
    tasks.confirm(200, batch, 0, None, MEMBERS, T0)
    completed = (date_end(due) if due else T0) + delta
    tasks.done(200, added[0].id, completed)
    assert tasks.db.get_task(1).done_at == utc_iso(completed)
    assert tasks.db.get_task(1).status == "done"
    with pytest.raises(RoundError, match="task_done"):
        tasks.done(200, 1, completed + timedelta(days=1))
    assert tasks.db.get_task(1).done_at == utc_iso(completed)
    counts = dict(tasks.db.connection.execute("SELECT name,count FROM metrics WHERE channel_id='200'"))
    assert counts == {counter: 1}


async def test_done_rolls_back_if_metric_write_fails(tasks):
    added, batch = await proposed(tasks)
    tasks.confirm(200, batch, 0, None, MEMBERS, T0)
    tasks.db.connection.execute("""CREATE TRIGGER reject_completion BEFORE INSERT ON metrics
        WHEN NEW.name='tasks_done_on_time' BEGIN SELECT RAISE(ABORT,'Test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        tasks.done(200, added[0].id, T0)
    assert tasks.db.get_task(1).status == "confirmed" and tasks.db.get_task(1).done_at is None


async def test_workload_is_compact_open_counts_with_no_ranking_and_paginated_board(tasks):
    added, batch = await proposed(tasks, titles=tuple(f"Planning task {i}" for i in range(10)))
    tasks.confirm(200, batch, 0, None, MEMBERS, T0)
    tasks.done(200, 1, T0)
    board = tasks.board(200, MEMBERS, T0)
    assert board.workload == (("102", 3), ("103", 3), ("104", 3))
    assert board.open_count == 9 and board.done_count == 1
    assert len(board.tasks) == 8 and board.total_pages == 2
    assert len(tasks.board(200, MEMBERS, T0, 99).tasks) == 2
    hidden = tasks.board(200, MEMBERS[:2], T0)
    assert hidden.unavailable_count == 3 and hidden.workload == (("102", 3), ("103", 3))


async def test_assignment_batches_are_bounded_and_other_batches_are_not_confirmed(tasks):
    for index in range(12):
        tasks.add(200, 102, f"Planning task {index}", None, T0)
    first = tasks.propose(200, await tasks.prepare_assign(200, MEMBERS, T0), MEMBERS, T0)
    tasks.db.publish_task_batch(200, first, 300)
    second = tasks.propose(200, await tasks.prepare_assign(200, MEMBERS, T0), MEMBERS, T0)
    tasks.db.publish_task_batch(200, second, 301)
    assert len(tasks.db.task_batch(200, first)) == 10 and len(tasks.db.task_batch(200, second)) == 2
    tasks.confirm(200, first, 0, None, MEMBERS, T0)
    assert all(task.status == "proposed" for task in tasks.db.task_batch(200, second))


async def test_prepared_assignments_cannot_be_saved_in_a_different_channel(tasks):
    tasks.add(200, 102, "Compare choices", None, T0)
    prepared = await tasks.prepare_assign(200, MEMBERS, T0)
    tasks.db.ensure_channel(201, 100)
    tasks.db.enable_channel(201)
    with pytest.raises(RoundError, match="task_stale"):
        tasks.propose(201, prepared, MEMBERS, T0)
    assert tasks.db.get_task(1).owner_user_id is None


@pytest.mark.parametrize("value", ["2026-2-03", "2026-02-30", "2026-12-31T12:00:00", "not a date"])
def test_dates_require_exact_valid_iso_day(tasks, value):
    with pytest.raises(RoundError, match="date"):
        tasks.add(200, 102, "Compare choices", value, T0)
    assert not tasks.db.channel_tasks(200)


@pytest.mark.parametrize("text", ["", "x" * 301, "Grade: 99", "Assignment prompt: supplied", "Write our report"])
def test_titles_and_preferences_reuse_existing_content_boundaries(tasks, text):
    with pytest.raises(RoundError):
        tasks.add(200, 102, text, None, T0)
    with pytest.raises(RoundError):
        tasks.prefs(200, 102, text)
    assert not tasks.db.channel_tasks(200) and tasks.db.preference(200, 102) == ""


async def test_task_state_and_proposal_expiry_survive_restart(tasks):
    added, batch = await proposed(tasks, titles=("Compare choices", "Arrange team review"))
    tasks.confirm(200, batch, added[0].id, "102", MEMBERS, T0)
    restored = Database(":memory:")
    restored.connection.deserialize(tasks.db.connection.serialize())
    try:
        service = TaskService(restored, FakeProvider())
        service.expire(200, T0 + timedelta(days=1))
        assert restored.get_task(1).status == "confirmed"
        assert restored.get_task(2).owner_user_id is None
        service.done(200, 1, T0)
        assert service.stats(200, T0).tasks_done_on_time == 1
    finally:
        restored.close()


def test_previous_task_schema_upgrades_without_losing_data(monkeypatch):
    connection = sqlite3.connect(":memory:")
    legacy = SCHEMA.replace("created_by TEXT NOT NULL,\n    proposal_batch TEXT,\n    proposal_message_id TEXT", "created_by TEXT NOT NULL")
    connection.executescript(legacy)
    connection.execute("INSERT INTO channels(channel_id,guild_id,enabled) VALUES('200','100',1)")
    connection.execute("INSERT INTO tasks(channel_id,title,status,proposed_at,created_by) VALUES('200','Existing task','proposed',?,'102')", (utc_iso(T0),))
    connection.commit()
    monkeypatch.setattr("core.db.sqlite3.connect", lambda path: connection)
    database = Database(":memory:")
    try:
        assert database.get_task(1).title == "Existing task"
        assert database.get_task(1).proposal_batch is None
        Database(":memory:")
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        database.close()


def test_stats_empty_are_numbers_or_none_and_never_fetch_content(tasks):
    assert all(value is None or type(value) in {int, float} for value in asdict(tasks.stats(200, T0)).values())
    assert tasks.stats(200, T0).accept_rate is None
    assert tasks.stats(200, T0).helpful_rate is None
    statements = []
    tasks.db.connection.set_trace_callback(statements.append)
    tasks.stats(200, T0)
    selects = " ".join(statements[1:]).lower()
    assert "question" not in selects and "title" not in selects and "reason" not in selects
    assert "user_id" not in selects and "preference_note" not in selects


def test_stats_round_ratios_median_rates_channel_isolation_and_live_overdue(tasks):
    rounds = RoundService(tasks.db, FakeProvider())
    for index, (members, replied, minutes) in enumerate(((MEMBERS[:2], 1, 10), (MEMBERS, 3, 30), (MEMBERS[:2], 0, None))):
        record = rounds.start(200, 102, PreparedQuestion("SECRET_QUESTION", ("A", "B")), members, T0)
        for member in members[:replied]:
            tasks.db.upsert_response(round_id=record.id, user_id=member, option_label="A", reason="SECRET_REASON", show_name=False, now=utc_iso(T0))
        if minutes is not None:
            tasks.db.close_round(record.id, utc_iso(T0 + timedelta(minutes=1)))
            tasks.db.record_outcome(record.id, "A", "102", utc_iso(T0 + timedelta(minutes=minutes)))
    cancelled = rounds.start(200, 102, PreparedQuestion("SECRET_CANCELLED", ()), MEMBERS, T0)
    tasks.db.cancel_round(cancelled.id)
    for name, count in (("prompts_shown", 4), ("prompts_accepted", 1), ("prompts_dismissed", 2), ("clarify_uses", 5), ("clarify_helpful", 3), ("clarify_unhelpful", 1), ("tasks_done_late", 2), ("tasks_done_on_time", 3)):
        for _ in range(count):
            tasks.db.increment_metric(200, name)
    tasks.add(200, 102, "SECRET_TASK", None, T0)
    tasks.db.connection.execute("UPDATE tasks SET status='confirmed',owner_user_id='102',due_at=?", (utc_iso(T0),))
    tasks.db.ensure_channel(201, 100)
    tasks.db.enable_channel(201)
    tasks.db.increment_metric(201, "prompts_shown")
    stats = tasks.stats(200, T0 + timedelta(microseconds=1))
    assert (stats.rounds_started, stats.rounds_closed) == (3, 2)
    assert stats.average_reply_share == pytest.approx((.5 + 1 + 0) / 3)
    assert stats.median_decision_seconds == pytest.approx(20 * 60, abs=.001)
    assert (stats.prompts_shown, stats.prompts_accepted, stats.prompts_dismissed, stats.accept_rate) == (4, 1, 2, .25)
    assert stats.clarify_uses == 5 and stats.helpful_rate == .75
    assert (stats.tasks_done_on_time, stats.tasks_done_late, stats.tasks_overdue) == (3, 2, 1)
    assert tasks.stats(200, T0).tasks_overdue == 0
    assert tasks.stats(201, T0).rounds_started == 0
    assert all(value is None or type(value) in {int, float} for value in asdict(stats).values())


@pytest.mark.parametrize("action", ["add", "prefs", "board", "assign", "done", "stats", "expire"])
async def test_pause_blocks_every_task_core_action(tasks, action):
    tasks.db.set_paused(200, True)
    tasks.provider.complete = AsyncMock()
    with pytest.raises(RoundError, match="paused"):
        if action == "assign":
            await tasks.prepare_assign(200, MEMBERS, T0)
        else:
            {"add": lambda: tasks.add(200, 102, "Compare choices", None, T0),
             "prefs": lambda: tasks.prefs(200, 102, "Organizing"),
             "board": lambda: tasks.board(200, MEMBERS, T0), "done": lambda: tasks.done(200, 1, T0),
             "stats": lambda: tasks.stats(200, T0), "expire": lambda: tasks.expire(200, T0)}[action]()
    tasks.provider.complete.assert_not_awaited()
