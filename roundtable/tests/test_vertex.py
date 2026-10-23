"""Vertex SDK boundary tests. Every Client is mocked; no API keys or credits."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.formatting import GENERIC_ERROR
from core import prompts
from core.config import load_settings
from core.clarify import Clarification
from core.llm import FakeProvider, VertexAIProvider, OpenAIProvider, ProviderError, make_provider, request_json
from core.rounds import ExtractedOptions, ParsedResponse, Synthesis
from core.tasks import OwnershipPlan
from core.timeline import TimelinePlan
from test_llm import Detection
from test_round_discord import context


@pytest.fixture(autouse=True)
def vertex_sdk(monkeypatch):
    from google import genai
    import socket

    monkeypatch.setattr(socket.socket, "connect", MagicMock(side_effect=AssertionError("Network disabled")))

    generate = AsyncMock(return_value=SimpleNamespace(text="{}"))
    client = MagicMock()
    client.models.generate_content = generate
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)
    wrapper = MagicMock()
    wrapper.aio = manager
    constructor = MagicMock(return_value=wrapper)
    monkeypatch.setattr(genai, "Client", constructor)
    return SimpleNamespace(constructor=constructor, manager=manager, generate=generate)


def test_factory_selects_vertex_lazily_and_preserves_other_providers(vertex_sdk, settings):
    configured = load_settings(environ={
        "DISCORD_TOKEN": "test-token", "LLM_PROVIDER": "vertex",
        "VERTEX_API_KEY": "synthetic-vertex-key", "LLM_MODEL": "test-vertex-model",
        "GOOGLE_CLOUD_PROJECT": "test-project",
    })
    provider = make_provider(configured)
    assert isinstance(provider, VertexAIProvider)
    assert "synthetic-vertex-key" not in repr(provider)
    assert isinstance(make_provider(settings), FakeProvider)
    assert isinstance(make_provider(replace(settings, llm_provider="openai")), OpenAIProvider)
    vertex_sdk.constructor.assert_not_called()


@pytest.mark.parametrize("system,user,response_model", [
    (prompts.DETECT_DECISION, "Which market should we choose?", Detection),
    (prompts.EXTRACT_OPTIONS, "Which market: Campus or Local businesses?", ExtractedOptions),
    (prompts.PARSE_RESPONSE, json.dumps({"options": ["Campus"], "reply": "Campus is nearby."}), ParsedResponse),
    (prompts.SUMMARIZE_REASONS, json.dumps({"minority_option": "Campus", "reasons": [
        {"option_label": "Campus", "reason": "Campus is nearby."},
        {"option_label": "Local businesses", "reason": "We have local contacts."},
    ]}), Synthesis),
    (prompts.CLARIFY_MESSAGE, "Can we compare those choices?", Clarification),
    (prompts.PROPOSE_OWNERS, json.dumps({"tasks": [{"task_id": 1, "title": "Compare choices", "due_at": None}], "members": [{"user_id": "102", "task_count": 0, "preference_note": "Organizing"}]}), OwnershipPlan),
    (prompts.BUILD_TIMELINE, "fixture", TimelinePlan),
])
async def test_all_prompt_operations_use_json_and_shared_pydantic_validation(
    vertex_sdk, system, user, response_model, caplog, capsys,
):
    fake = FakeProvider()
    raw = await fake.complete(system=system, user=user)
    vertex_sdk.generate.return_value = SimpleNamespace(text=raw)
    result = await request_json(
        VertexAIProvider(api_key="synthetic-vertex-key", model="test-vertex-model", project="test-project"),
        system=system, user=user, response_model=response_model,
    )
    expected = await request_json(fake, system=system, user=user, response_model=response_model)
    assert isinstance(result, response_model) and result == expected
    args = vertex_sdk.generate.call_args.kwargs
    assert args["model"] == "projects/test-project/locations/global/publishers/google/models/test-vertex-model" and args["contents"] == user
    config = args["config"]
    assert config.system_instruction == prompts.SAFETY_RULES + "\n\n" + system
    assert config.response_mime_type == "application/json" and config.temperature == 0
    assert config.tools is None and config.automatic_function_calling.disable
    assert config.cached_content is None
    options = vertex_sdk.constructor.call_args.kwargs
    assert options["api_key"] == "synthetic-vertex-key" and options["vertexai"] is True
    assert "project" not in options and "location" not in options and "credentials" not in options
    assert options["http_options"].api_version == "v1"
    assert options["http_options"].base_url == "https://aiplatform.googleapis.com/"
    assert options["http_options"].timeout == 30_000
    assert options["http_options"].retry_options.attempts == 1
    vertex_sdk.manager.__aexit__.assert_awaited_once()
    assert "synthetic-vertex-key" not in caplog.text + str(capsys.readouterr())


@pytest.mark.parametrize("invalid", [
    "PRIVATE_INVALID_VERTEX_OUTPUT", "{}",
    '{"question":"Which market?","options":["only one"]}',
    '{"question":"Which market?","options":"wrong type"}',
])
async def test_invalid_json_or_schema_retried_exactly_once(vertex_sdk, invalid, caplog, capsys):
    valid = '{"question":"Which market?","options":[]}'
    vertex_sdk.generate.side_effect = [SimpleNamespace(text=invalid), SimpleNamespace(text=valid)]
    result = await request_json(
        VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project"),
        system=prompts.EXTRACT_OPTIONS, user="PRIVATE_INPUT_SENTINEL", response_model=ExtractedOptions,
    )
    assert result.options == []
    assert vertex_sdk.generate.await_count == 2
    assert vertex_sdk.constructor.call_count == 2 and vertex_sdk.manager.__aexit__.await_count == 2
    captured = caplog.text + str(capsys.readouterr())
    assert "PRIVATE_INPUT_SENTINEL" not in captured and "PRIVATE_INVALID_VERTEX_OUTPUT" not in captured


@pytest.mark.parametrize("raw", [None, "", "PRIVATE_INVALID_VERTEX_OUTPUT"])
async def test_two_invalid_or_empty_outputs_fail_quietly(vertex_sdk, raw, caplog, capsys):
    vertex_sdk.generate.return_value = SimpleNamespace(text=raw)
    assert await request_json(
        VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project"),
        system=prompts.EXTRACT_OPTIONS, user="PRIVATE_INPUT_SENTINEL", response_model=ExtractedOptions,
    ) is None
    assert vertex_sdk.generate.await_count == 2
    captured = caplog.text + str(capsys.readouterr())
    assert "PRIVATE_INPUT_SENTINEL" not in captured and "PRIVATE_INVALID_VERTEX_OUTPUT" not in captured


@pytest.mark.parametrize("phase", ["init", "enter", "generate", "read", "close"])
async def test_sdk_errors_never_expose_payloads_or_keys(vertex_sdk, phase, caplog, capsys):
    error = RuntimeError("PRIVATE_PROMPT PRIVATE_RESPONSE PRIVATE_REASON synthetic-vertex-key")
    if phase == "init":
        vertex_sdk.constructor.side_effect = error
    elif phase == "enter":
        vertex_sdk.manager.__aenter__.side_effect = error
    elif phase == "generate":
        vertex_sdk.generate.side_effect = error
    elif phase == "read":
        class UnreadableResponse:
            @property
            def text(self):
                raise error
        vertex_sdk.generate.return_value = UnreadableResponse()
    else:
        vertex_sdk.manager.__aexit__.side_effect = error
    with pytest.raises(ProviderError) as failure:
        await VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project").complete(system="PRIVATE_PROMPT", user="PRIVATE_REASON")
    assert str(failure.value) == "Provider request failed." and failure.value.__suppress_context__
    captured = caplog.text + str(capsys.readouterr()) + str(failure.value)
    for sentinel in ["PRIVATE_PROMPT", "PRIVATE_RESPONSE", "PRIVATE_REASON", "synthetic-vertex-key"]:
        assert sentinel not in captured


async def test_transport_failure_is_not_retried(vertex_sdk):
    vertex_sdk.generate.side_effect = TimeoutError("PRIVATE_RESPONSE")
    assert await request_json(
        VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project"),
        system=prompts.EXTRACT_OPTIONS, user="PRIVATE_INPUT", response_model=ExtractedOptions,
    ) is None
    assert vertex_sdk.generate.await_count == 1
    vertex_sdk.manager.__aexit__.assert_awaited_once()


async def test_sdk_recording_flags_cannot_enable_disk_recording(vertex_sdk, monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_CLIENT_MODE", "record")
    monkeypatch.setenv("GOOGLE_GENAI_REPLAYS_DIRECTORY", "/must-not-record")
    monkeypatch.setenv("GOOGLE_GENAI_REPLAY_ID", "must-not-record")
    await VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project").complete(system="template", user="private-reason")
    config = vertex_sdk.constructor.call_args.kwargs["debug_config"]
    assert config.client_mode is None and config.replays_directory is None and config.replay_id is None


async def test_existing_round_command_preserves_safe_ephemeral_error(vertex_sdk, context):
    ctx = context
    ctx.service.provider = VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project")
    vertex_sdk.generate.return_value = SimpleNamespace(text="PRIVATE_INVALID_RESPONSE")
    interaction = ctx.make()
    await ctx.ui.decide(interaction, "Which market: Campus or Local businesses?")
    assert vertex_sdk.generate.await_count == 2
    assert interaction.followup.send.call_args.args[0] == GENERIC_ERROR
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    ctx.channel.send.assert_not_awaited()
    assert ctx.db.get_round(1) is None


async def test_existing_round_command_accepts_vertex_extraction(vertex_sdk, context):
    ctx = context
    ctx.service.provider = VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project")
    vertex_sdk.generate.return_value = SimpleNamespace(text=json.dumps({
        "question": "Which market: Campus or Local businesses?", "options": ["Campus", "Local businesses"],
    }))
    interaction = ctx.make()
    await ctx.ui.decide(interaction, "Which market: Campus or Local businesses?")
    assert ctx.db.get_round(1).options == ("Campus", "Local businesses")
    assert interaction.followup.send.call_args.kwargs["ephemeral"]
    assert ctx.channel.send.call_args.args[0].startswith("Roundtable (AI):")


async def test_real_sdk_builds_project_scoped_cloud_request_without_adc(monkeypatch, vertex_sdk, caplog, capsys):
    """Use the real serializer/transport builder, replacing all HTTP with a mock."""
    import google.auth
    import httpx
    import socket
    from google.genai.client import Client
    from google.genai._api_client import BaseApiClient

    monkeypatch.setenv("GEMINI_API_KEY", "must-never-use-developer-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "must-never-use-ambient-key")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "wrong-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/must-not-read-service-account.json")
    adc = MagicMock(side_effect=AssertionError("ADC must not be used"))
    monkeypatch.setattr(google.auth, "default", adc)
    monkeypatch.setattr(BaseApiClient, "_use_aiohttp", lambda self: False)
    monkeypatch.setattr(socket.socket, "connect", MagicMock(side_effect=AssertionError("Network disabled")))
    requests = []

    async def send(_client, request, **kwargs):
        requests.append(request)
        return httpx.Response(200, request=request, json={
            "candidates": [{"content": {"role": "model", "parts": [{
                "text": '{"question":"Which market?","options":[]}',
            }]}, "finishReason": "STOP"}],
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    # The module fixture mocks the public constructor; select the real SDK here.
    monkeypatch.setattr("google.genai.Client", Client)
    provider = VertexAIProvider(api_key="synthetic-vertex-key", model="test-model", project="test-project")
    result = await request_json(provider, system=prompts.EXTRACT_OPTIONS, user="PRIVATE_INPUT", response_model=ExtractedOptions)
    assert result is not None
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/publishers/google/models/test-model:generateContent"
    assert request.headers["x-goog-api-key"] == "synthetic-vertex-key"
    assert "authorization" not in request.headers
    assert not request.url.query
    body = json.loads(request.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["contents"][0]["parts"][0]["text"] == "PRIVATE_INPUT"
    adc.assert_not_called()
    assert "PRIVATE_INPUT" not in caplog.text + str(capsys.readouterr())
