"""Selected-text clarification and anonymous counters. No content persistence."""

from collections.abc import Callable
import time
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core import prompts
from core.db import Database
from core.llm import CheckedProvider, Provider, request_json
from core.rounds import RoundError, validate_input


FEEDBACK_TTL = 900
MAX_PENDING_FEEDBACK = 1024


class ClarifiedTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=180)]


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meaning: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    terms: list[ClarifiedTerm] = Field(max_length=5)
    other_readings: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = Field(max_length=3)
    follow_up_question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ClarifyService:
    def __init__(self, db: Database, provider: Provider, *, clock: Callable[[], float] = time.monotonic):
        self.db, self.provider, self.clock = db, provider, clock
        # Only random receipt -> (channel id, expiry). Never messages, answers,
        # identities, or usage records. Restarts intentionally expire receipts.
        self._feedback: dict[str, tuple[str, float]] = {}
        self._generations: dict[str, int] = {}

    def generation(self, channel_id) -> int:
        return self._generations.get(str(channel_id), 0)

    def invalidate(self, channel_id):
        self._generations[str(channel_id)] = self.generation(channel_id) + 1

    def require_current(self, channel_id, generation):
        self.require_active(channel_id)
        if self.generation(channel_id) != generation:
            raise RoundError("stale")

    def require_active(self, channel_id: str | int) -> None:
        state = self.db.get_channel(channel_id)
        if state is None or not state.enabled:
            raise RoundError("setup")
        if state.paused:
            raise RoundError("paused")

    async def explain(self, channel_id: str | int, text: str) -> Clarification:
        self.require_active(channel_id)
        generation = self.generation(channel_id)
        text = validate_input(text, 4000)

        def allowed():
            state = self.db.get_channel(channel_id)
            return state is not None and state.active and self.generation(channel_id) == generation

        result = await request_json(
            CheckedProvider(self.provider, allowed), system=prompts.CLARIFY_MESSAGE, user=text, response_model=Clarification,
        )
        self.require_current(channel_id, generation)
        if result is None:
            raise RoundError("provider")
        return result

    def new_feedback(self, channel_id: str | int) -> str:
        self.require_active(channel_id)
        now = self.clock()
        self._feedback = {key: value for key, value in self._feedback.items() if value[1] > now}
        if len(self._feedback) >= MAX_PENDING_FEEDBACK:
            self._feedback.pop(next(iter(self._feedback)))
        token = uuid4().hex
        self._feedback[token] = (str(channel_id), now + FEEDBACK_TTL)
        return token

    def discard_feedback(self, token: str) -> None:
        self._feedback.pop(token, None)

    def clear_feedback(self) -> None:
        self._feedback.clear()

    def record_use(self, channel_id: str | int) -> None:
        self.require_active(channel_id)
        self.db.increment_metric(channel_id, "clarify_uses")

    def record_feedback(self, channel_id: str | int, token: str, *, helpful: bool) -> None:
        self.require_active(channel_id)
        receipt = self._feedback.get(token)
        if receipt is None or receipt[0] != str(channel_id) or receipt[1] <= self.clock():
            raise RoundError("clarify_feedback")
        self.db.increment_metric(channel_id, "clarify_helpful" if helpful else "clarify_unhelpful")
        self.discard_feedback(token)
