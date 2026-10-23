"""Decision round rules and LLM synthesis, using plain data only."""

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core import prompts
from core.db import Database, DecisionEntry, ResponseRecord, RoundRecord, utc_iso
from core.llm import CheckedProvider, Provider, request_json


ANOTHER = "Another option"
NONE = "None of these"
HISTORY_PAGE_SIZE = 10
MAX_REOPEN_OPTIONS = 24  # Discord permits 25 buttons, including None of these.
MIN_DEADLINE_HOURS, MAX_DEADLINE_HOURS = 5 / 60, 30 * 24
DEADLINE_PRESETS = (("1 hour", "1h"), ("6 hours", "6h"), ("1 day", "1d"), ("3 days", "3d"), ("1 week", "1w"))
_DURATION_UNITS = {"m": 1 / 60, "h": 1, "d": 24, "w": 168}


class RoundError(Exception):
    """Only a fixed error code; never attach input or provider output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_duration(text: str) -> float:
    """Relative round length such as 90m, 36h, 2d, or "1 week", in hours."""
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)",
        text.strip().casefold(),
    )
    if match is None:
        raise RoundError("round_deadline")
    hours = float(match[1]) * _DURATION_UNITS[match[2][0]]
    if not MIN_DEADLINE_HOURS <= hours <= MAX_DEADLINE_HOURS:
        raise RoundError("round_deadline")
    return hours


def duration_suggestions(text: str) -> tuple[tuple[str, str], ...]:
    """(label, value) pairs for a partly typed relative length."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", text.strip().casefold())
    if match is None:
        typed = text.strip().casefold()
        return tuple(preset for preset in DEADLINE_PRESETS if typed in preset[0]) or DEADLINE_PRESETS
    number, unit = match[1], match[2]
    amount = float(number)
    shown = f"{amount:g}"
    suggestions = []
    for code, name, aliases in (
        ("h", "hour", ("hours", "hrs")), ("d", "day", ("days",)),
        ("w", "week", ("weeks", "wks")), ("m", "minute", ("minutes", "mins")),
    ):
        if not any(alias.startswith(unit) for alias in aliases):
            continue
        try:
            parse_duration(f"{number}{code}")
        except RoundError:
            continue
        suggestions.append((f"{shown} {name}{'' if amount == 1 else 's'}", f"{number}{code}"))
    return tuple(suggestions)


def validate_input(text: str, maximum: int) -> str:
    text = text.strip()
    if not text or len(text) > maximum:
        raise RoundError("input")
    if re.search(
        r"(?:assignment\s+prompt|submission|grades?)\s*:|"
        r"\b(?:write|draft|compose|generate|solve)\b.{0,70}\b(?:essay|report|assignment|deliverable|submission|homework)\b|"
        r"\byou(?:'re| are) (?:wrong|stupid|incompetent)\b", text, re.I | re.S,
    ):
        raise RoundError("boundary")
    return text


class ExtractedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=300)
    options: list[str]

    @field_validator("options")
    @classmethod
    def valid_options(cls, options: list[str]) -> list[str]:
        if len(options) not in {0, 2, 3, 4}:
            raise ValueError("Expected zero or two to four options.")
        if any(not option.strip() or len(option) > 80 or option.strip().casefold() in {ANOTHER.casefold(), NONE.casefold()} for option in options):
            raise ValueError("Invalid option label.")
        if len({option.strip().casefold() for option in options}) != len(options):
            raise ValueError("Duplicate options.")
        return [option.strip() for option in options]


class ParsedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_label: str | None = Field(max_length=80)
    reason: str = Field(min_length=1, max_length=600)

    @field_validator("reason")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty reason.")
        return value.strip()


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steelman: str = Field(min_length=1, max_length=700)
    trade_off: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=300)

    @field_validator("steelman", "trade_off", "question")
    @classmethod
    def neutral_wording(cls, value: str) -> str:
        value = value.strip()
        if not value.strip() or re.search(
            r"\brecommend\w*\b|\b(?:you|we|the team) should (?:pick|choose|select|use|go with|opt for)\b|"
            r"(?:^|[.!?]\s+)\s*(?:please\s+)?(?:pick|choose|select|use|go with|opt for)\b|"
            r"\b(?:is|would be)\s+(?:clearly\s+)?(?:the\s+)?(?:best|better|correct)\s+(?:choice|option)\b|"
            r"\b(?:best|correct) (?:choice|option) is\b|\byou (?:are|were) wrong\b|<@|@everyone|@here",
            value, re.I,
        ):
            raise ValueError("Non-neutral synthesis.")
        return value


@dataclass(frozen=True)
class PreparedQuestion:
    question: str
    options: tuple[str, ...]
    generation: int | None = None


@dataclass(frozen=True)
class DecisionPage:
    entries: tuple[DecisionEntry, ...]
    page: int
    total_pages: int


@dataclass(frozen=True)
class PresentedReason:
    response: ResponseRecord
    english: str


@dataclass(frozen=True)
class RoundResult:
    round: RoundRecord
    responses: tuple[ResponseRecord, ...]
    reasons: tuple[PresentedReason, ...]
    counts: tuple[tuple[str, int], ...]
    total_members: int
    minority_option: str | None
    synthesis: Synthesis
    deadline_missing: bool
    generation: int | None = None


class RoundService:
    def __init__(self, db: Database, provider: Provider, deadline_hours: float = 24):
        self.db, self.provider, self.deadline_hours = db, provider, deadline_hours
        self._generations: dict[str, int] = {}

    def generation(self, channel_id) -> int:
        return self._generations.get(str(channel_id), 0)

    def invalidate(self, channel_id):
        self._generations[str(channel_id)] = self.generation(channel_id) + 1

    def require_current(self, channel_id, generation):
        self.require_active(channel_id)
        if self.generation(channel_id) != generation:
            raise RoundError("stale")

    def checked_provider(self, channel_id, generation) -> Provider:
        def allowed():
            state = self.db.get_channel(channel_id)
            return state is not None and state.active and self.generation(channel_id) == generation
        return CheckedProvider(self.provider, allowed)

    def require_active(self, channel_id: str | int) -> None:
        state = self.db.get_channel(channel_id)
        if state is None or not state.enabled:
            raise RoundError("setup")
        if state.paused:
            raise RoundError("paused")

    def get(self, round_id: int, channel_id: str | int, user_id: str | int | None = None) -> RoundRecord:
        self.require_active(channel_id)
        record = self.db.get_round(round_id)
        if record is None or record.channel_id != str(channel_id) or record.status == "cancelled":
            raise RoundError("round")
        if user_id is not None and str(user_id) not in {m.user_id for m in self.db.round_members(round_id)}:
            raise RoundError("member")
        return record

    def collecting(self, round_id: int, channel_id: str | int, user_id: str | int, now: datetime) -> RoundRecord:
        record = self.get(round_id, channel_id, user_id)
        if record.status != "collecting" or now >= datetime.fromisoformat(record.deadline_at):
            raise RoundError("closed")
        return record

    async def prepare_question(self, channel_id: str | int, question: str) -> PreparedQuestion:
        self.require_active(channel_id)
        generation = self.generation(channel_id)
        question = validate_input(question, 300)
        extracted = await request_json(
            self.checked_provider(channel_id, generation), system=prompts.EXTRACT_OPTIONS, user=question, response_model=ExtractedOptions,
        )
        self.require_current(channel_id, generation)
        if extracted is None or any(option.casefold() not in question.casefold() for option in extracted.options):
            raise RoundError("provider")
        return PreparedQuestion(question, tuple(extracted.options), generation)

    def start(
        self, channel_id: str | int, creator_id: str | int, prepared: PreparedQuestion,
        member_ids: tuple[str, ...], now: datetime, *, reopened_from: int | None = None,
        deadline_hours: float | None = None,
    ) -> RoundRecord:
        self.require_active(channel_id)
        if prepared.generation is not None:
            self.require_current(channel_id, prepared.generation)
        if not member_ids or str(creator_id) not in member_ids:
            raise RoundError("member")
        if deadline_hours is not None and not MIN_DEADLINE_HOURS <= deadline_hours <= MAX_DEADLINE_HOURS:
            raise RoundError("round_deadline")
        return self.db.create_round(
            channel_id=str(channel_id), question=prepared.question, options=prepared.options,
            created_by=str(creator_id), created_at=utc_iso(now),
            deadline_at=utc_iso(now + timedelta(hours=self.deadline_hours if deadline_hours is None else deadline_hours)), member_ids=member_ids,
            reopened_from=reopened_from,
        )

    def history(self, channel_id: str | int, page: int = 0) -> DecisionPage:
        self.require_active(channel_id)
        total = self.db.decision_count(str(channel_id))
        total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        page = min(max(0, page), total_pages - 1)
        return DecisionPage(
            self.db.decision_history(str(channel_id), limit=HISTORY_PAGE_SIZE, offset=page * HISTORY_PAGE_SIZE),
            page, total_pages,
        )

    def closed_decision(self, round_id: int, channel_id: str | int) -> RoundRecord:
        record = self.get(round_id, channel_id)
        if record.status != "closed":
            raise RoundError("reopen")
        return record

    def reopen(
        self, round_id: int, channel_id: str | int, creator_id: str | int,
        member_ids: tuple[str, ...], now: datetime, *, new_option: str | None = None,
    ) -> RoundRecord:
        source = self.closed_decision(round_id, channel_id)
        choices = self.choices(source)
        if new_option is not None:
            option = validate_input(new_option, 80)
            if option.casefold() in {ANOTHER.casefold(), NONE.casefold()} or any(option.casefold() == choice.casefold() for choice in choices):
                raise RoundError("new_option")
            choices += (option,)
        if len(choices) > MAX_REOPEN_OPTIONS:
            raise RoundError("option_capacity")
        if self.db.active_reopening(source.id) is not None:
            raise RoundError("reopened")
        try:
            return self.start(
                channel_id, creator_id, PreparedQuestion(source.question, choices),
                member_ids, now, reopened_from=source.id,
            )
        except sqlite3.IntegrityError:
            if self.db.active_reopening(source.id) is not None:
                raise RoundError("reopened") from None
            raise

    def option(self, record: RoundRecord, selection: int) -> str:
        options = record.options or (ANOTHER,)
        if selection < 0 or selection >= len(options):
            raise RoundError("selection")
        return options[selection]

    async def save_response(
        self, *, round_id: int, channel_id: str | int, user_id: str | int,
        selection: int, reason: str, show_name: bool, answer: str | None = None,
        now: datetime | None = None,
    ) -> None:
        record = self.collecting(round_id, channel_id, user_id, now or now_utc())
        label = self.option(record, selection)
        reason = validate_input(reason, 300)
        if not record.options:
            if answer is None:
                raise RoundError("input")
            label = validate_input(answer, 80)
            if label.casefold() in {ANOTHER.casefold(), NONE.casefold()}:
                raise RoundError("input")
        elif answer is not None:
            # A free-text payload must never override a selected fixed option.
            raise RoundError("input")
        if not self.db.upsert_response(
            round_id=round_id, user_id=str(user_id), option_label=label, reason=reason,
            show_name=show_name, now=utc_iso(now or now_utc()),
        ):
            raise RoundError("closed")

    def progress(self, round_id: int) -> tuple[int, int]:
        members = self.db.round_members(round_id)
        return sum(member.replied for member in members), len(members)

    def ready(self, record: RoundRecord, now: datetime) -> bool:
        replied, total = self.progress(record.id)
        return record.status == "collecting" and (replied == total or now >= datetime.fromisoformat(record.deadline_at))

    def freeze_ready(self, round_id: int, now: datetime) -> RoundRecord:
        record = self.db.get_round(round_id)
        if record is None:
            raise RoundError("round")
        self.require_active(record.channel_id)
        if not self.ready(record, now):
            raise RoundError("not_ready")
        self.db.close_round(round_id, utc_iso(now))
        return self.db.get_round(round_id)

    def end_early(
        self, round_id: int, channel_id: str | int, user_id: str | int,
        now: datetime, *, can_manage: bool = False,
    ) -> RoundRecord:
        record = self.get(round_id, channel_id)
        if str(user_id) != record.created_by and not can_manage:
            raise RoundError("end_permission")
        if record.status != "collecting" or not self.db.close_round(round_id, utc_iso(now)):
            raise RoundError("closed")
        return self.db.get_round(round_id)

    def choices(self, record: RoundRecord) -> tuple[str, ...]:
        return record.options or self.db.response_choices(record.id)

    async def prepare_result(self, round_id: int, now: datetime) -> RoundResult:
        record = self.db.get_round(round_id)
        if record is None:
            raise RoundError("round")
        self.require_active(record.channel_id)
        generation = self.generation(record.channel_id)
        provider = self.checked_provider(record.channel_id, generation)
        if record.status != "closed" and not self.ready(record, now):
            raise RoundError("not_ready")
        responses = self.db.round_responses(round_id)
        choices = self.choices(record)
        counts = Counter(response.option_label for response in responses)
        represented = [option for option in choices if counts[option] > 0]
        minority = min(represented, key=lambda option: counts[option]) if len(represented) > 1 else None
        translations = await asyncio.gather(*(
            request_json(
                provider, system=prompts.PARSE_RESPONSE,
                user=json.dumps({"options": list(choices), "reply": response.reason}, ensure_ascii=False),
                response_model=ParsedResponse,
            ) for response in responses
        ))
        self.require_current(record.channel_id, generation)
        if any(translation is None for translation in translations):
            raise RoundError("provider")
        if responses:
            synthesis = await request_json(
                provider, system=prompts.STEELMAN_MINORITY,
                user=json.dumps({
                    "minority_option": minority,
                    "reasons": [{"option_label": response.option_label, "reason": response.reason} for response in responses],
                }, ensure_ascii=False), response_model=Synthesis,
            )
            self.require_current(record.channel_id, generation)
            if synthesis is None:
                raise RoundError("provider")
        else:
            synthesis = Synthesis(
                steelman="No dissent raised.",
                trade_off="No member reasons were shared, so there is no stated trade-off to synthesize.",
                question="What options would the team like to compare?",
            )
        replied, total = self.progress(round_id)
        return RoundResult(
            record, responses,
            tuple(PresentedReason(response, translation.reason) for response, translation in zip(responses, translations)),
            tuple((option, counts[option]) for option in choices), total, minority, synthesis,
            replied < total and (record.status == "closed" or now >= datetime.fromisoformat(record.deadline_at)),
            generation,
        )

    def close_prepared(self, result: RoundResult, now: datetime) -> RoundRecord:
        if result.generation is not None:
            self.require_current(result.round.channel_id, result.generation)
        record = self.get(result.round.id, result.round.channel_id)
        if record.outcome is not None:
            raise RoundError("outcome")
        if self.db.round_responses(record.id) != result.responses:
            raise RoundError("stale")
        if record.status == "collecting":
            if not self.ready(record, now):
                raise RoundError("not_ready")
            self.db.close_round(record.id, utc_iso(now))
        return self.db.get_round(record.id)

    def outcome(self, round_id: int, channel_id: str | int, user_id: str | int, choice: int, now: datetime) -> RoundRecord:
        record = self.get(round_id, channel_id)
        choices = self.choices(record) + (NONE,)
        if record.status != "closed" or record.outcome is not None:
            raise RoundError("outcome")
        if choice < 0 or choice >= len(choices):
            raise RoundError("selection")
        if not self.db.record_outcome(record.id, choices[choice], str(user_id), utc_iso(now)):
            raise RoundError("outcome")
        return self.db.get_round(record.id)

    def reminder_candidates(self, round_id: int, now: datetime) -> tuple[str, ...]:
        record = self.db.get_round(round_id)
        self.require_active(record.channel_id)
        created, deadline = datetime.fromisoformat(record.created_at), datetime.fromisoformat(record.deadline_at)
        if record.status != "collecting" or now < created + (deadline - created) / 2 or now >= deadline:
            return ()
        return tuple(member.user_id for member in self.db.round_members(round_id) if not member.replied and not member.reminded)

    def log(self, channel_id: str | int):
        self.require_active(channel_id)
        return self.db.decision_log(str(channel_id))
