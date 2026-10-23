"""Manual dates and private, optional backward timeline proposals."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from core import prompts
from core.db import Database, DeadlineRecord, TaskRecord, utc_iso
from core.llm import Provider, ProviderError, request_json
from core.rounds import RoundError, RoundService, validate_input
from core.tasks import date_end


PREVIEW_LIFETIME = timedelta(minutes=15)
MAX_PREVIEWS = 128


class Milestone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("title")
    @classmethod
    def planning_only(cls, value):
        try:
            return validate_input(value, 100)
        except RoundError:
            raise ValueError("Invalid milestone title.") from None

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        date.fromisoformat(value)
        return value


class TimelinePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    milestones: list[Milestone] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def distinct_milestones(self):
        if len({item.title.casefold() for item in self.milestones}) != len(self.milestones):
            raise ValueError("Duplicate milestones.")
        return self


@dataclass(frozen=True, repr=False)
class TimelinePreview:
    deadline: DeadlineRecord
    milestones: tuple[tuple[str, str], ...]
    late_tasks: tuple[str, ...]
    tasks: tuple[TaskRecord, ...]
    generation: int


class TimelineService:
    def __init__(self, db: Database, provider: Provider):
        self.db, self.provider = db, provider
        self._rounds = RoundService(db, provider)
        # Proposals are ephemeral: no generated text, chat, or preference notes
        # are saved as confirmed milestones. A restart expires these previews.
        self._previews: dict[str, tuple[str, str, datetime, TimelinePreview]] = {}
        self._generations: dict[str, int] = {}

    def invalidate(self, channel_id):
        key = str(channel_id)
        self._generations[key] = self._generations.get(key, 0) + 1
        self._previews = {token: receipt for token, receipt in self._previews.items() if receipt[0] != key}

    def require_active(self, channel_id):
        self._rounds.require_active(channel_id)

    def add_deadline(self, channel_id, title: str, date_text: str, now: datetime) -> DeadlineRecord:
        self.require_active(channel_id)
        title = validate_input(title, 200)
        due = date_end(date_text)
        if due < now:
            raise RoundError("date_past")
        return self.db.add_deadline(channel_id, title, utc_iso(due))

    async def build(self, channel_id, now: datetime) -> TimelinePreview:
        self.require_active(channel_id)
        generation = self._generations.get(str(channel_id), 0)
        deadline = self.db.nearest_deadline(channel_id, utc_iso(now))
        if deadline is None:
            raise RoundError("deadline")
        today = now.date()
        due_date = datetime.fromisoformat(deadline.due_at).date()
        last_day = due_date - timedelta(days=1)
        if last_day < today:
            raise RoundError("deadline_soon")
        tasks = self.db.channel_tasks(channel_id)

        class DatedTimeline(TimelinePlan):
            @model_validator(mode="after")
            def dates_in_range(self):
                if any(not today <= date.fromisoformat(item.date) <= last_day for item in self.milestones):
                    raise ValueError("Milestone outside the planning window or buffer.")
                self.milestones.sort(key=lambda item: item.date)
                return self

        class ActiveProvider:
            async def complete(guard, **kwargs):
                try:
                    self.require_active(channel_id)
                    if self._generations.get(str(channel_id), 0) != generation:
                        raise RoundError("timeline_stale")
                except RoundError:
                    raise ProviderError("Provider request failed.") from None
                return await self.provider.complete(**kwargs)

        result = await request_json(
            ActiveProvider(), system=prompts.BUILD_TIMELINE,
            user=json.dumps({
                "today": today.isoformat(), "assignment_deadline": due_date.isoformat(), "buffer_days": 1,
                "tasks": [{"title": task.title, "due_at": task.due_at, "status": task.status} for task in tasks],
            }, ensure_ascii=False), response_model=DatedTimeline,
        )
        self.require_active(channel_id)
        if self._generations.get(str(channel_id), 0) != generation:
            raise RoundError("timeline_stale")
        if result is None:
            raise RoundError("provider")
        if self.db.nearest_deadline(channel_id, utc_iso(now)) != deadline or self.db.channel_tasks(channel_id) != tasks:
            raise RoundError("timeline_stale")
        late_tasks = tuple(task.title for task in tasks if task.due_at is not None and task.due_at > deadline.due_at)
        return TimelinePreview(deadline, tuple((item.title, item.date) for item in result.milestones), late_tasks, tasks, generation)

    def save_preview(self, channel_id, user_id, preview: TimelinePreview, now: datetime) -> str:
        self.require_active(channel_id)
        self.expire(now)
        if len(self._previews) >= MAX_PREVIEWS:
            self._previews.pop(next(iter(self._previews)))
        token = uuid4().hex
        self._previews[token] = (str(channel_id), str(user_id), now + PREVIEW_LIFETIME, preview)
        try:
            self.get_preview(channel_id, user_id, token, now)
        except RoundError:
            self.discard_preview(token)
            raise
        return token

    def get_preview(self, channel_id, user_id, token: str, now: datetime) -> TimelinePreview:
        self.require_active(channel_id)
        self.expire(now)
        receipt = self._previews.get(token)
        if receipt is None or receipt[:2] != (str(channel_id), str(user_id)):
            raise RoundError("timeline_preview")
        preview = receipt[3]
        # A cached proposal cannot be posted with dates now in the past, a newer
        # nearer deadline, or silently changed task warnings.
        deadline = self.db.nearest_deadline(channel_id, utc_iso(now))
        late_tasks = tuple(task.title for task in self.db.channel_tasks(channel_id)
                           if task.due_at is not None and task.due_at > preview.deadline.due_at)
        if (deadline != preview.deadline or preview.generation != self._generations.get(str(channel_id), 0)
            or self.db.channel_tasks(channel_id) != preview.tasks
            or any(date.fromisoformat(day) < now.date() for _, day in preview.milestones) or late_tasks != preview.late_tasks):
            raise RoundError("timeline_stale")
        return preview

    def discard_preview(self, token: str):
        self._previews.pop(token, None)

    def expire(self, now: datetime):
        self._previews = {token: receipt for token, receipt in self._previews.items() if receipt[2] > now}

    def clear(self):
        self._previews.clear()
