from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

from core import prompts
from core.detection import DetectionResult as Detection
from core.llm import FakeProvider, OpenAIProvider, ProviderError, make_provider, request_json


@pytest.mark.parametrize("text,expected", [
    ("1. AB: WHICH MARKET should we pick?", True),
    ("1. AB: Thanks for the meeting.", False),
])
async def test_fake_detection_is_deterministic_and_valid(text, expected):
    fake = FakeProvider()
    result = await request_json(fake, system=prompts.DETECT_DECISION, user=text, response_model=Detection)
    assert result.is_decision == expected
    assert len(result.options) == (2 if expected else 0)
    assert await fake.complete(system=prompts.DETECT_DECISION, user=text) == await fake.complete(system=prompts.DETECT_DECISION, user=text)
    assert vars(fake) == {}  # No request history, even in the demo provider.


@pytest.mark.parametrize("template,keys", [
    (prompts.EXTRACT_OPTIONS, {"question", "options"}),
    (prompts.PARSE_RESPONSE, {"option_label", "reason"}),
    (prompts.SUMMARIZE_REASONS, {"summary", "trade_off", "question"}),
    (prompts.CLARIFY_MESSAGE, {"meaning", "terms", "other_readings", "follow_up_question"}),
    (prompts.PROPOSE_OWNERS, {"assignments"}),
    (prompts.BUILD_TIMELINE, {"milestones"}),
])
async def test_other_fake_fixtures_are_json_and_repeatable(template, keys):
    fake = FakeProvider()
    first = await fake.complete(system=template, user="fixture")
    assert set(json.loads(first)) == keys
    assert first == await fake.complete(system=template, user="fixture")


async def test_bad_json_or_schema_retries_exactly_once(caplog):
    fake = FakeProvider()
    valid = await fake.complete(system=prompts.DETECT_DECISION, user="which market")
    for invalid in ["invalid-json", '{"is_decision": "yes"}', valid.replace("0.95", "1.5")]:
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=[invalid, valid])
        result = await request_json(provider, system=prompts.DETECT_DECISION, user="private-input", response_model=Detection)
        assert result.is_decision
        assert provider.complete.await_count == 2
    assert "private-input" not in caplog.text
    assert "invalid-json" not in caplog.text


async def test_second_invalid_response_fails_quietly(caplog):
    provider = MagicMock()
    provider.complete = AsyncMock(return_value="private-invalid-output")
    assert await request_json(provider, system=prompts.DETECT_DECISION, user="private-input", response_model=Detection) is None
    assert provider.complete.await_count == 2
    assert not caplog.text


async def test_provider_failure_is_quiet_and_not_retried(caplog):
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=ProviderError("Provider request failed."))
    assert await request_json(provider, system=prompts.DETECT_DECISION, user="private-input", response_model=Detection) is None
    assert provider.complete.await_count == 1
    assert not caplog.text


async def test_unknown_fake_operation_does_not_echo_input():
    with pytest.raises(ProviderError) as failure:
        await FakeProvider().complete(system="private-system", user="private-input")
    assert "private" not in str(failure.value)


def test_factory_uses_configured_fake(settings):
    assert isinstance(make_provider(settings), FakeProvider)


async def test_openai_adapter_uses_json_low_temperature_and_no_storage():
    canned = await FakeProvider().complete(system=prompts.DETECT_DECISION, user="which market")
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=canned))]
    ))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    with patch("openai.AsyncOpenAI", return_value=context) as constructor:
        result = await OpenAIProvider(api_key="test-key", model="test-model").complete(
            system=prompts.DETECT_DECISION, user="which market"
        )
    assert json.loads(result)["is_decision"]
    constructor.assert_called_once_with(api_key="test-key", max_retries=0, timeout=30)
    args = client.chat.completions.create.call_args.kwargs
    assert args["response_format"] == {"type": "json_object"}
    assert args["temperature"] == 0
    assert args["store"] is False


async def test_openai_error_never_exposes_response_body(caplog):
    with patch("openai.AsyncOpenAI", side_effect=RuntimeError("private-body")):
        with pytest.raises(ProviderError) as failure:
            await OpenAIProvider(api_key="test-key", model="test-model").complete(system="template", user="private-input")
    assert "private" not in str(failure.value)
    assert failure.value.__suppress_context__
    assert not caplog.text
