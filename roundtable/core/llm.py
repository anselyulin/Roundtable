"""Stateless JSON provider boundary. Never cache or log inputs or outputs."""

from typing import Protocol, TypeVar
from collections.abc import Callable
from datetime import date, timedelta
import json
import re

from pydantic import BaseModel, ValidationError

from core import prompts
from core.config import Settings


class ProviderError(Exception):
    """A value-free provider failure; never carry a request or response body."""


class Provider(Protocol):
    async def complete(self, *, system: str, user: str) -> str: ...


class CheckedProvider:
    """Recheck permission before each structured-output attempt, including retries."""

    def __init__(self, provider: Provider, allowed: Callable[[], bool]):
        self.provider, self.allowed = provider, allowed

    async def complete(self, *, system: str, user: str) -> str:
        if not self.allowed():
            raise ProviderError("Provider request failed.")
        return await self.provider.complete(system=system, user=user)


class FakeProvider:
    """Canned fixtures only, not a substitute for actual language understanding."""

    async def complete(self, *, system: str, user: str) -> str:
        if system == prompts.DETECT_DECISION:
            decision = "which market" in user.casefold()
            result = {
                "is_decision": decision,
                "confidence": 0.95 if decision else 0.0,
                "question": "Which market should we choose?" if decision else "",
                "options": ["Campus", "Local businesses"] if decision else [],
            }
        elif system == prompts.EXTRACT_OPTIONS:
            # A documented, deterministic demo grammar: "Question: A or B" / A | B.
            candidate = user.rsplit(":", 1)[-1].strip().rstrip("?.")
            options = re.split(r"\s+(?:or|vs\.?|versus)\s+|\s*\|\s*", candidate, flags=re.I)
            options = [option.strip() for option in options]
            result = {"question": user, "options": options if 2 <= len(options) <= 4 else []}
        elif system == prompts.PARSE_RESPONSE:
            try:
                payload = json.loads(user)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("reply"), str):
                reply, options = payload["reply"], payload.get("options", [])
                label = next((option for option in options if option.casefold() in reply.casefold()), None)
                if not options:
                    label = reply.split(" — ", 1)[0].split(":", 1)[0].strip()[:80]
                result = {"option_label": label, "reason": reply}
            else:
                result = {"option_label": None, "reason": user}
        elif system == prompts.SUMMARIZE_REASONS:
            try:
                payload = json.loads(user)
            except (ValueError, TypeError):
                payload = {}
            reasons = payload.get("reasons", []) if isinstance(payload, dict) else []
            distinct = list(dict.fromkeys(reason["reason"][:100] for reason in reasons))
            stated = "; ".join(dict.fromkeys(f'“{reason["reason"][:100]}” ({reason["option_label"]})' for reason in reasons[:5]))
            result = {
                "summary": f"Members gave these reasons: {stated}." if stated else "No views were shared, so there is nothing to summarize.",
                "trade_off": f'The stated trade-off is between “{distinct[0]}” and “{distinct[1]}”.' if len(distinct) > 1 else "The team can weigh the priority in its stated reason; no competing priority was raised.",
                "question": "Which of the priorities you raised matters most for this decision?",
            }
        elif system == prompts.CLARIFY_MESSAGE:
            result = {
                "meaning": "Your teammate is asking the team to compare the available choices before deciding.",
                "terms": [],
                "other_readings": ["It could also mean they want more discussion before a decision."],
                "follow_up_question": "Which choices would you like us to compare?",
            }
        elif system == prompts.PROPOSE_OWNERS:
            try:
                payload = json.loads(user)
            except (ValueError, TypeError):
                payload = {}
            members = payload.get("members", []) if isinstance(payload, dict) else []
            counts = {member["user_id"]: member["task_count"] for member in members}
            assignments = []
            for task in payload.get("tasks", []) if isinstance(payload, dict) else []:
                if not counts:
                    break
                owner = min(counts, key=counts.get)
                assignments.append({"task_id": task["task_id"], "user_id": owner, "reason": "Balances current open task counts."})
                counts[owner] += 1
            result = {"assignments": assignments}
        elif system == prompts.BUILD_TIMELINE:
            # Deterministic planning dates relative to supplied dates, no clock
            # or network dependence. Preserve the original provider-test fixture.
            try:
                payload = json.loads(user)
                today = date.fromisoformat(payload["today"])
                final = date.fromisoformat(payload["assignment_deadline"]) - timedelta(days=1)
                middle = today + (final - today) // 2
            except (ValueError, TypeError, KeyError):
                today, middle, final = date(2099, 1, 1), date(2099, 1, 3), date(2099, 1, 5)
            result = {"milestones": [
                {"title": "Agree on the plan", "date": today.isoformat()},
                {"title": "Review progress together", "date": middle.isoformat()},
                {"title": "Team review and buffer", "date": final.isoformat()},
            ]}
        else:
            raise ProviderError("Unsupported provider operation.")
        return json.dumps(result, ensure_ascii=False)


class OpenAIProvider:
    """Lazy adapter: initialization makes no LLM requests."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def complete(self, *, system: str, user: str) -> str:
        from openai import AsyncOpenAI

        try:
            async with AsyncOpenAI(
                api_key=self._api_key, max_retries=0, timeout=30
            ) as client:
                response = await client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": prompts.SAFETY_RULES},
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0,
                    store=False,
                )
                return response.choices[0].message.content or ""
        except Exception:
            # SDK exceptions can include request/response content. Never propagate it.
            raise ProviderError("Provider request failed.") from None


class VertexAIProvider:
    """Project-scoped Vertex requests authenticated by a Cloud authorization key."""

    def __init__(self, *, api_key: str, model: str, project: str, location: str = "global") -> None:
        self._api_key = api_key
        self._model = model
        self._project = project
        self._location = location

    async def complete(self, *, system: str, user: str) -> str:
        try:
            from google import genai
            from google.genai import types
            from google.genai.client import DebugConfig

            async with genai.Client(
                api_key=self._api_key,
                vertexai=True,
                # Never let SDK recording/replay environment flags write inputs
                # or responses to disk. This adapter always uses a live transport.
                debug_config=DebugConfig(client_mode=None, replays_directory=None, replay_id=None),
                http_options=types.HttpOptions(
                    api_version="v1",
                    base_url="https://aiplatform.googleapis.com/",
                    timeout=30_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ).aio as client:
                response = await client.models.generate_content(
                    # The SDK rejects project/location together with api_key.
                    # A full model resource scopes key-auth requests to our
                    # project, without ADC or downloaded credential files.
                    model=f"projects/{self._project}/locations/{self._location}/publishers/google/models/{self._model}",
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=prompts.SAFETY_RULES + "\n\n" + system,
                        response_mime_type="application/json",
                        temperature=0,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                return response.text or ""
        except Exception:
            # SDK errors may contain keys, prompts, or generated text. The shared
            # validator/caller must see only the existing value-free failure.
            raise ProviderError("Provider request failed.") from None


Result = TypeVar("Result", bound=BaseModel)


async def request_json(
    provider: Provider, *, system: str, user: str, response_model: type[Result]
) -> Result | None:
    """Retry malformed JSON/schema once; return a quiet failure without raw errors."""
    for _ in range(2):
        try:
            raw = await provider.complete(system=system, user=user)
        except ProviderError:
            return None
        try:
            return response_model.model_validate_json(raw, strict=True)
        except ValidationError:
            continue
    return None


def make_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "fake":
        return FakeProvider()
    if settings.llm_provider == "vertex":
        return VertexAIProvider(
            api_key=settings.vertex_api_key, model=settings.llm_model,
            project=settings.google_cloud_project, location=settings.google_cloud_location,
        )
    return OpenAIProvider(api_key=settings.openai_api_key, model=settings.llm_model)
