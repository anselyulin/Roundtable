"""Bounded, transient decision detection. Raw chat is never persisted or logged."""

from collections import deque
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import sqlite3

from pydantic import Field

from core import prompts
from core.config import Settings
from core.db import Database, DecisionPrompt, RoundRecord, utc_iso
from core.llm import Provider, ProviderError, request_json
from core.rounds import ExtractedOptions, PreparedQuestion, RoundError, now_utc, validate_input


WINDOW_AGE = timedelta(minutes=10)
CHECK_INTERVAL = timedelta(seconds=45)


class DetectionResult(ExtractedOptions):
    question: str = Field(max_length=300)
    is_decision: bool
    confidence: float = Field(ge=0, le=1)


@dataclass(repr=False)
class RecentText:
    author_id: str
    text: str
    sent_at: datetime
    sequence: int


@dataclass(repr=False)
class Window:
    messages: deque[RecentText] = field(default_factory=lambda: deque(maxlen=15))
    sequence: int = 0
    last_check_at: datetime | None = None
    checking: bool = False


@dataclass(repr=False)
class DetectionCheck:
    channel_id: str
    window: Window
    texts: tuple[str, ...]
    payload: str
    expires_at: datetime


class DetectionService:
    def __init__(self, db: Database, provider: Provider, settings: Settings, *, clock: Callable[[], datetime] = now_utc):
        self.db, self.provider, self.clock = db, provider, clock
        self.mode = settings.detection_mode
        self.confidence = settings.detection_confidence
        self.cooldown = timedelta(minutes=settings.detection_cooldown_min)
        self.deadline_hours = settings.round_deadline_hours
        self.windows: dict[str, Window] = {}

    def clear_channel(self, channel_id: str | int) -> None:
        # Removing the object also invalidates any in-flight check, even if the
        # channel is resumed before the provider returns.
        window = self.windows.pop(str(channel_id), None)
        if window is not None:
            window.messages.clear()

    def clear(self) -> None:
        for channel_id in list(self.windows):
            self.clear_channel(channel_id)

    def can_read(self, channel_id: str | int) -> bool:
        state = self.db.get_channel(channel_id)
        allowed = self.mode == "auto" and state is not None and state.active and state.detection_enabled
        if not allowed:
            self.clear_channel(channel_id)
        return bool(allowed)

    def can_offer(self, channel_id: str | int, now: datetime) -> bool:
        if not self.can_read(channel_id) or self.db.has_pending_prompt(channel_id):
            return False
        state = self.db.get_channel(channel_id)
        # The normal posting cooldown is authoritative. Ignore legacy extended
        # dismissal timers so upgrading also releases old 60-minute quiet periods.
        return not state.last_prompt_at or now >= datetime.fromisoformat(state.last_prompt_at) + self.cooldown

    def expire(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        for channel_id, window in list(self.windows.items()):
            if not self.can_read(channel_id):
                continue
            window.messages = deque((item for item in window.messages if item.sent_at + WINDOW_AGE > now), maxlen=15)
            if not window.messages:
                self.clear_channel(channel_id)

    def next_expiry(self) -> datetime | None:
        return min((item.sent_at + WINDOW_AGE for window in self.windows.values() for item in window.messages), default=None)

    def is_recent(self, sent_at: datetime) -> bool:
        utc_iso(sent_at)
        now = self.clock()
        return sent_at <= now < sent_at + WINDOW_AGE

    def observe(self, channel_id: str | int, author_id: str | int, text: str, sent_at: datetime) -> DetectionCheck | None:
        now = self.clock()
        self.expire(now)
        if not self.can_read(channel_id):
            return None
        if not self.is_recent(sent_at):
            return None
        try:
            text = validate_input(text, 4000)
        except RoundError:
            self.clear_channel(channel_id)
            return None
        channel_id = str(channel_id)
        window = self.windows.setdefault(channel_id, Window())
        window.sequence += 1
        window.messages.append(RecentText(str(author_id), text, sent_at, window.sequence))
        if (
            window.checking or not self.can_offer(channel_id, now)
            # Require enough context for the first check. After that, recheck
            # each new message once the rate limit elapses; requiring four new
            # messages after every negative result misses decisions that emerge
            # gradually in ordinary, slower conversations.
            or window.last_check_at is None and len(window.messages) < 4
            or len({item.author_id for item in window.messages}) < 2
            or window.last_check_at is not None and now - window.last_check_at < CHECK_INTERVAL
        ):
            return None
        window.checking, window.last_check_at = True, now
        # Anonymous initials are local to this window; never send Discord ids or
        # display names to the model. Only the numbered recent text is supplied.
        initials = {author: chr(65 + index) for index, author in enumerate(dict.fromkeys(item.author_id for item in window.messages))}
        return DetectionCheck(
            channel_id, window, tuple(item.text for item in window.messages),
            "\n".join(f"{index}. {initials[item.author_id]}: {item.text}" for index, item in enumerate(window.messages, 1)),
            min(item.sent_at + WINDOW_AGE for item in window.messages),
        )

    def valid_check(self, check: DetectionCheck) -> bool:
        self.expire()
        return self.clock() < check.expires_at and self.windows.get(check.channel_id) is check.window and self.can_offer(check.channel_id, self.clock())

    async def evaluate(self, check: DetectionCheck) -> PreparedQuestion | None:
        class CurrentWindowProvider:
            async def complete(guard, *, system: str, user: str) -> str:
                # Shared JSON retry must not initiate another call after pause,
                # disable, expiry, or invalidation of the source window.
                if not self.valid_check(check):
                    raise ProviderError("Provider request failed.")
                return await self.provider.complete(system=system, user=user)

        try:
            if not self.valid_check(check):
                return None
            result = await asyncio.wait_for(
                request_json(CurrentWindowProvider(), system=prompts.DETECT_DECISION, user=check.payload, response_model=DetectionResult),
                timeout=(check.expires_at - self.clock()).total_seconds(),
            )
            if not self.valid_check(check) or result is None or not result.is_decision or result.confidence < self.confidence or len(result.options) < 2:
                return None
            question = validate_input(result.question, 300)
            source = "\n".join(check.texts).casefold()
            if any(option.casefold() not in source for option in result.options):
                return None
            for option in result.options:
                validate_input(option, 80)
            return PreparedQuestion(question, tuple(result.options))
        except (RoundError, TimeoutError):
            return None
        finally:
            check.window.checking = False
            check.texts, check.payload = (), ""

    def reserve_prompt(self, check: DetectionCheck, candidate: PreparedQuestion) -> DecisionPrompt:
        if not self.valid_check(check):
            raise RoundError("detection_prompt")
        try:
            return self.db.create_decision_prompt(check.channel_id, candidate.question, candidate.options, utc_iso(self.clock()))
        except sqlite3.IntegrityError:
            raise RoundError("detection_prompt") from None

    def mark_shown(self, prompt_id: int, message_id: str | int) -> None:
        now = self.clock()
        if not self.db.mark_prompt_shown(prompt_id, message_id, utc_iso(now), utc_iso(now + self.cooldown)):
            raise RoundError("detection_prompt")
        self.clear_channel(self.db.get_decision_prompt(prompt_id).channel_id)

    def pending_prompt(self, prompt_id: int, channel_id: str | int) -> DecisionPrompt:
        state = self.db.get_channel(channel_id)
        if state is None or not state.enabled:
            raise RoundError("setup")
        if state.paused:
            raise RoundError("paused")
        prompt = self.db.get_decision_prompt(prompt_id)
        if prompt is None or prompt.channel_id != str(channel_id) or prompt.status != "pending" or not prompt.public_message_id:
            raise RoundError("detection_prompt")
        return prompt

    def dismiss(self, prompt_id: int, channel_id: str | int) -> None:
        self.pending_prompt(prompt_id, channel_id)
        now = self.clock()
        if not self.db.dismiss_decision_prompt(prompt_id, utc_iso(now)):
            raise RoundError("detection_prompt")
        self.clear_channel(channel_id)

    def accept(self, prompt_id: int, channel_id: str | int, creator_id: str | int, members: tuple[str, ...]) -> RoundRecord:
        self.pending_prompt(prompt_id, channel_id)
        if not members or str(creator_id) not in members:
            raise RoundError("member")
        now = self.clock()
        record = self.db.accept_decision_prompt(
            prompt_id, creator_id, members, utc_iso(now), utc_iso(now + timedelta(hours=self.deadline_hours)),
        )
        if record is None:
            raise RoundError("detection_prompt")
        self.clear_channel(channel_id)
        return record
