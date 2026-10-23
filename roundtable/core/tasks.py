"""Member-authored tasks, provisional owners, workload, and numeric pilot stats."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from hashlib import sha256
import re
from statistics import median
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from core import prompts
from core.db import Database, TaskRecord, utc_iso
from core.llm import Provider, ProviderError, request_json
from core.rounds import RoundError, RoundService, validate_input


TASK_PAGE_SIZE = 8
ASSIGNMENT_LIMIT = 10  # Two buttons/task and Confirm all fit one Discord View.
PROPOSAL_LIFETIME = timedelta(hours=24)


def proposal_fingerprint(tasks: tuple[TaskRecord, ...]) -> str:
    # A short numeric version for bulk consent, containing no title or note.
    state = [(task.id, task.owner_user_id, task.status) for task in tasks]
    return str(int.from_bytes(sha256(json.dumps(state).encode()).digest()[:8], "big"))


def date_end(value: str) -> datetime:
    """Date-only input means the last instant of that UTC day, inclusively."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return datetime.combine(date.fromisoformat(value), time.max, timezone.utc)
    except (ValueError, TypeError):
        raise RoundError("date") from None


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: int = Field(gt=0)
    user_id: str = Field(pattern=r"^[0-9]+$", min_length=1, max_length=20)
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]

    @field_validator("reason")
    @classmethod
    def bounded_reason(cls, value):
        if len(value.split()) > 12:
            raise ValueError("Assignment reason exceeds twelve words.")
        # Convert input-boundary errors to schema errors for the JSON retry path.
        try:
            validate_input(value, 160)
        except RoundError:
            raise ValueError("Invalid assignment reason.") from None
        return value


class OwnershipPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignments: list[Assignment] = Field(max_length=ASSIGNMENT_LIMIT)

    @model_validator(mode="after")
    def unique_tasks(self):
        if len({item.task_id for item in self.assignments}) != len(self.assignments):
            raise ValueError("Duplicate task assignment.")
        return self


@dataclass(frozen=True, repr=False)
class PreparedAssignments:
    tasks: tuple[TaskRecord, ...]
    members: tuple[str, ...]
    context: tuple[tuple[str, int, str], ...]
    assignments: tuple[tuple[int, str], ...]
    generation: int


@dataclass(frozen=True)
class TaskBoard:
    tasks: tuple[TaskRecord, ...]
    page: int
    total_pages: int
    open_count: int
    done_count: int
    workload: tuple[tuple[str, int], ...]
    unavailable_count: int


@dataclass(frozen=True)
class PilotStats:
    # Intentionally no channel/member ids or text fields.
    rounds_started: int
    rounds_closed: int
    average_reply_share: float | None
    median_decision_seconds: float | None
    prompts_shown: int
    prompts_accepted: int
    prompts_dismissed: int
    accept_rate: float | None
    clarify_uses: int
    helpful_rate: float | None
    tasks_done_on_time: int
    tasks_done_late: int
    tasks_overdue: int


class TaskService:
    def __init__(self, db: Database, provider: Provider):
        self.db, self.provider = db, provider
        self._rounds = RoundService(db, provider)
        self._generations: dict[str, int] = {}

    def invalidate(self, channel_id):
        key = str(channel_id)
        self._generations[key] = self._generations.get(key, 0) + 1

    def require_active(self, channel_id):
        self._rounds.require_active(channel_id)

    def add(self, channel_id, creator_id, title: str, due: str | None, now: datetime, *, round_id: int | None = None) -> TaskRecord:
        self.require_active(channel_id)
        title = validate_input(title, 200)
        due_at = utc_iso(date_end(due)) if due else None
        if round_id is not None:
            record = self._rounds.get(round_id, channel_id)
            if record.status != "closed" or record.outcome is None:
                raise RoundError("task_round")
        return self.db.add_task(channel_id, creator_id, title, due_at, utc_iso(now), round_id)

    def prefs(self, channel_id, user_id, note: str):
        self.require_active(channel_id)
        note = validate_input(note, 300)
        self.db.set_preference(channel_id, user_id, note)

    def expire(self, channel_id, now: datetime):
        self.require_active(channel_id)
        self.db.expire_task_proposals(channel_id, utc_iso(now - PROPOSAL_LIFETIME))

    def board(self, channel_id, members: tuple[str, ...], now: datetime, page: int = 0) -> TaskBoard:
        self.expire(channel_id, now)
        tasks = self.db.channel_tasks(channel_id)
        total_pages = max(1, (len(tasks) + TASK_PAGE_SIZE - 1) // TASK_PAGE_SIZE)
        page = min(max(0, page), total_pages - 1)
        counts = {member: 0 for member in dict.fromkeys(members)}
        unavailable = 0
        for task in tasks:
            if task.status == "confirmed":
                if task.owner_user_id in counts:
                    counts[task.owner_user_id] += 1
                else:
                    unavailable += 1
        return TaskBoard(tasks[page * TASK_PAGE_SIZE:(page + 1) * TASK_PAGE_SIZE], page, total_pages,
                         sum(task.status != "done" for task in tasks), sum(task.status == "done" for task in tasks), tuple(counts.items()), unavailable)

    def _context(self, channel_id, members: tuple[str, ...], now: datetime) -> tuple[tuple[str, int, str], ...]:
        self.expire(channel_id, now)
        tasks = self.db.channel_tasks(channel_id)
        # Pending suggestions reserve a count too, preventing overlapping batches
        # from making identical empty-workload assumptions. No performance scores.
        return tuple((member, sum(task.status != "done" and task.owner_user_id == member for task in tasks),
                      self.db.preference(channel_id, member)) for member in dict.fromkeys(members))

    async def prepare_assign(self, channel_id, members: tuple[str, ...], now: datetime) -> PreparedAssignments:
        generation = self._generations.get(str(channel_id), 0)
        context = self._context(channel_id, members, now)
        if not context:
            raise RoundError("task_members")
        tasks = tuple(task for task in self.db.channel_tasks(channel_id) if task.status == "proposed" and task.owner_user_id is None)[:ASSIGNMENT_LIMIT]
        if not tasks:
            raise RoundError("task_unowned")
        task_ids, member_ids = {task.id for task in tasks}, set(members)

        class CurrentOwnershipPlan(OwnershipPlan):
            @model_validator(mode="after")
            def exact_scope(self):
                if {item.task_id for item in self.assignments} != task_ids or any(item.user_id not in member_ids for item in self.assignments):
                    raise ValueError("Assignment outside supplied tasks or members.")
                return self

        class ActiveProvider:
            async def complete(guard, **kwargs):
                try:
                    self.require_active(channel_id)
                    if self._generations.get(str(channel_id), 0) != generation:
                        raise RoundError("task_stale")
                except RoundError:
                    raise ProviderError("Provider request failed.") from None
                return await self.provider.complete(**kwargs)

        result = await request_json(
            ActiveProvider(), system=prompts.PROPOSE_OWNERS,
            user=json.dumps({
                "tasks": [{"task_id": task.id, "title": task.title, "due_at": task.due_at} for task in tasks],
                "members": [{"user_id": member, "task_count": count, "preference_note": note} for member, count, note in context],
            }, ensure_ascii=False), response_model=CurrentOwnershipPlan,
        )
        self.require_active(channel_id)
        if self._generations.get(str(channel_id), 0) != generation:
            raise RoundError("task_stale")
        if result is None:
            raise RoundError("provider")
        # Explanations are validated but never published/stored; preferences must
        # not leak through a model's explanation of its suggestion.
        return PreparedAssignments(tasks, members, context, tuple((item.task_id, item.user_id) for item in result.assignments), generation)

    def propose(self, channel_id, prepared: PreparedAssignments, members: tuple[str, ...], now: datetime) -> str:
        self.require_active(channel_id)
        if (any(task.channel_id != str(channel_id) for task in prepared.tasks)
            or prepared.generation != self._generations.get(str(channel_id), 0)
            or set(members) != set(prepared.members) or self._context(channel_id, prepared.members, now) != prepared.context):
            raise RoundError("task_stale")
        batch = uuid4().hex[:16]
        try:
            self.db.save_task_proposals(prepared.tasks, prepared.assignments, batch, utc_iso(now))
        except ValueError:
            raise RoundError("task_stale") from None
        return batch

    def pending(self, channel_id, batch: str, now: datetime, task_id: int = 0) -> tuple[TaskRecord, ...]:
        self.expire(channel_id, now)
        tasks = tuple(task for task in self.db.task_batch(channel_id, batch)
                      if task.status == "proposed" and task.owner_user_id is not None and task.proposal_message_id is not None
                      and (task_id == 0 or task.id == task_id))
        if not tasks:
            raise RoundError("task_proposal")
        return tasks

    def confirm(self, channel_id, batch: str, task_id: int, expected_owner: str | None, members: tuple[str, ...], now: datetime):
        tasks = self.pending(channel_id, batch, now, task_id)
        if any(task.owner_user_id not in members or expected_owner is not None and task.owner_user_id != expected_owner for task in tasks):
            raise RoundError("task_stale")
        self.db.confirm_tasks(channel_id, batch, tuple(task.id for task in tasks))

    def swap(self, channel_id, batch: str, task_id: int, owner_id: str, members: tuple[str, ...], now: datetime):
        self.pending(channel_id, batch, now, task_id)
        if owner_id not in members:
            raise RoundError("task_members")
        self.db.swap_task_owner(task_id, channel_id, batch, owner_id)

    def done(self, channel_id, task_id: int, now: datetime):
        self.require_active(channel_id)
        task = self.db.get_task(task_id)
        if task is None or task.channel_id != str(channel_id):
            raise RoundError("task")
        if task.status != "confirmed" or not self.db.complete_task(task_id, channel_id, utc_iso(now)):
            raise RoundError("task_done")

    def stats(self, channel_id, now: datetime) -> PilotStats:
        self.require_active(channel_id)
        numbers = self.db.pilot_numbers(channel_id, utc_iso(now))
        counts = numbers["counters"]
        shown, accepted, dismissed = (counts.get(name, 0) for name in ("prompts_shown", "prompts_accepted", "prompts_dismissed"))
        ratings = counts.get("clarify_helpful", 0) + counts.get("clarify_unhelpful", 0)
        durations = numbers["durations"]
        return PilotStats(numbers["started"], numbers["closed"], numbers["share"], median(durations) if durations else None,
                          shown, accepted, dismissed, accepted / shown if shown else None,
                          counts.get("clarify_uses", 0), counts.get("clarify_helpful", 0) / ratings if ratings else None,
                          counts.get("tasks_done_on_time", 0), counts.get("tasks_done_late", 0), numbers["overdue"])
