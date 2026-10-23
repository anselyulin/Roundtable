from datetime import timedelta
import json
from unittest.mock import AsyncMock

import pytest

from core import prompts
from core.llm import FakeProvider
from core.rounds import RoundError
from core.tasks import TaskService
from core.timeline import TimelineService
from test_rounds import T0


@pytest.fixture
def timeline(db):
    db.ensure_channel(200, 100)
    db.enable_channel(200)
    return TimelineService(db, FakeProvider())


def deadline(service, days=10):
    return service.add_deadline(200, "Team checkpoint", (T0 + timedelta(days=days)).date().isoformat(), T0)


async def test_nearest_assignment_builds_three_milestones_with_buffer_and_flags_late_tasks(timeline):
    near = deadline(timeline)
    deadline(timeline, 20)
    tasks = TaskService(timeline.db, FakeProvider())
    tasks.add(200, 102, "Compare choices", (T0 + timedelta(days=11)).date().isoformat(), T0)
    tasks.add(200, 102, "Arrange review", (T0 + timedelta(days=10)).date().isoformat(), T0)
    timeline.provider.complete = AsyncMock(wraps=FakeProvider().complete)
    preview = await timeline.build(200, T0)
    assert preview.deadline == near and len(preview.milestones) == 3
    assert preview.milestones[-1][1] == (T0 + timedelta(days=9)).date().isoformat()
    assert preview.late_tasks == ("Compare choices",)
    args = timeline.provider.complete.call_args.kwargs
    assert args["system"] == prompts.BUILD_TIMELINE
    assert set(json.loads(args["user"])) == {"today", "assignment_deadline", "buffer_days", "tasks"}
    assert all(set(task) == {"title", "due_at", "status"} for task in json.loads(args["user"])["tasks"])
    assert timeline.db.connection.execute("SELECT count(*) FROM deadlines WHERE kind='milestone'").fetchone()[0] == 0


@pytest.mark.parametrize("invalid", [
    "PRIVATE_MODEL_RESPONSE",
    '{"milestones":[]}',
    json.dumps({"milestones": [{"title": f"Planning {i}", "date": "2000-01-01"} for i in range(3)]}),
    json.dumps({"milestones": [{"title": f"Planning {i}", "date": "2099-01-01"} for i in range(3)]}),
    json.dumps({"milestones": [{"title": "Same title", "date": "2026-01-01"} for _ in range(3)]}),
    json.dumps({"milestones": [{"title": f"Planning {i}", "date": "2026-02-30"} for i in range(3)]}),
])
async def test_invalid_timeline_retries_once_and_stores_no_output(timeline, invalid, caplog):
    deadline(timeline)
    timeline.provider.complete = AsyncMock(return_value=invalid)
    with pytest.raises(RoundError, match="provider"):
        await timeline.build(200, T0)
    assert timeline.provider.complete.await_count == 2
    assert "PRIVATE_MODEL_RESPONSE" not in "\n".join(timeline.db.connection.iterdump()) + caplog.text


async def test_deadline_day_is_rejected_as_a_milestone_for_one_day_buffer(timeline):
    due = deadline(timeline)
    payload = {"milestones": [{"title": f"Planning {i}", "date": due.due_at[:10]} for i in range(3)]}
    timeline.provider.complete = AsyncMock(return_value=json.dumps(payload))
    with pytest.raises(RoundError, match="provider"):
        await timeline.build(200, T0)
    assert timeline.provider.complete.await_count == 2


async def test_one_day_deadline_allows_three_distinct_milestones_today(timeline):
    deadline(timeline, 1)
    result = await timeline.build(200, T0)
    assert len(result.milestones) == 3 and all(day == T0.date().isoformat() for _, day in result.milestones)


async def test_due_date_mismatch_is_flagged_even_for_a_completed_task(timeline):
    deadline(timeline)
    task = TaskService(timeline.db, FakeProvider()).add(200, 102, "Compare choices", (T0 + timedelta(days=11)).date().isoformat(), T0)
    timeline.db.connection.execute("UPDATE tasks SET status='done' WHERE id=?", (task.id,))
    assert (await timeline.build(200, T0)).late_tasks == ("Compare choices",)


async def test_no_deadline_or_same_day_deadline_fails_before_model(timeline):
    timeline.provider.complete = AsyncMock()
    with pytest.raises(RoundError, match="deadline"):
        await timeline.build(200, T0)
    deadline(timeline, 0)
    with pytest.raises(RoundError, match="deadline_soon"):
        await timeline.build(200, T0)
    timeline.provider.complete.assert_not_awaited()


@pytest.mark.parametrize("text", ["2026-02-30", "2026-1-2", "2026-01-01T00:00:00", "not a date"])
def test_bad_deadline_date_is_not_saved(timeline, text):
    with pytest.raises(RoundError, match="date"):
        timeline.add_deadline(200, "Team checkpoint", text, T0)
    assert timeline.db.connection.execute("SELECT count(*) FROM deadlines").fetchone()[0] == 0


def test_past_deadline_is_refused_and_forbidden_content_is_not_saved(timeline):
    with pytest.raises(RoundError, match="date_past"):
        deadline(timeline, -1)
    for text in ("Assignment prompt: supplied text", "Grade: 99", "Write our report"):
        with pytest.raises(RoundError, match="boundary"):
            timeline.add_deadline(200, text, "2099-01-01", T0)


async def test_private_preview_is_member_and_channel_bound_single_use_and_expires(timeline):
    deadline(timeline)
    preview = await timeline.build(200, T0)
    token = timeline.save_preview(200, 102, preview, T0)
    assert timeline.get_preview(200, 102, token, T0) == preview
    with pytest.raises(RoundError, match="timeline_preview"):
        timeline.get_preview(200, 103, token, T0)
    timeline.db.ensure_channel(201, 100)
    timeline.db.enable_channel(201)
    with pytest.raises(RoundError, match="timeline_preview"):
        timeline.get_preview(201, 102, token, T0)
    timeline.discard_preview(token)
    with pytest.raises(RoundError, match="timeline_preview"):
        timeline.get_preview(200, 102, token, T0)
    token = timeline.save_preview(200, 102, preview, T0)
    with pytest.raises(RoundError, match="timeline_preview"):
        timeline.get_preview(200, 102, token, T0 + timedelta(minutes=15))
    token = timeline.save_preview(200, 102, preview, T0)
    restarted = TimelineService(timeline.db, FakeProvider())
    with pytest.raises(RoundError, match="timeline_preview"):
        restarted.get_preview(200, 102, token, T0)


async def test_new_nearer_deadline_or_new_late_task_invalidates_cached_preview(timeline):
    deadline(timeline)
    preview = await timeline.build(200, T0)
    token = timeline.save_preview(200, 102, preview, T0)
    deadline(timeline, 5)
    with pytest.raises(RoundError, match="timeline_stale"):
        timeline.get_preview(200, 102, token, T0)


async def test_task_changes_during_model_call_are_not_published(timeline):
    deadline(timeline)
    original = timeline.provider.complete

    async def changed(**kwargs):
        TaskService(timeline.db, FakeProvider()).add(200, 102, "Arrange review", None, T0)
        return await original(**kwargs)

    timeline.provider.complete = changed
    with pytest.raises(RoundError, match="timeline_stale"):
        await timeline.build(200, T0)


async def test_pause_blocks_deadline_timeline_and_posting(timeline):
    deadline(timeline)
    preview = await timeline.build(200, T0)
    token = timeline.save_preview(200, 102, preview, T0)
    timeline.db.set_paused(200, True)
    timeline.provider.complete = AsyncMock()
    with pytest.raises(RoundError, match="paused"):
        deadline(timeline)
    with pytest.raises(RoundError, match="paused"):
        await timeline.build(200, T0)
    with pytest.raises(RoundError, match="paused"):
        timeline.get_preview(200, 102, token, T0)
    timeline.provider.complete.assert_not_awaited()
