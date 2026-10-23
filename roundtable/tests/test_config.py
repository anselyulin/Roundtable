from dataclasses import replace
import os

import pytest

from core.config import ConfigError, load_settings


BASE = {"DISCORD_TOKEN": "private-token", "LLM_PROVIDER": "fake"}


def test_defaults_and_no_secret_repr(settings):
    assert settings.discord_guild_id is None
    assert settings.detection_mode == "auto"
    assert settings.round_deadline_hours == 24
    assert settings.detection_confidence == 0.75
    assert settings.detection_cooldown_min == 15
    assert "test-token" not in repr(settings)
    assert "private-key" not in repr(replace(settings, openai_api_key="private-key"))
    assert "vertex-private-key" not in repr(replace(settings, vertex_api_key="vertex-private-key"))


def test_explicit_demo_and_dev_settings():
    settings = load_settings(environ={
        **BASE, "ROUND_DEADLINE_HOURS": "0.05", "DISCORD_GUILD_ID": "123",
        "DETECTION_MODE": "command_only",
    })
    assert settings.round_deadline_hours == 0.05
    assert settings.discord_guild_id == 123
    assert settings.detection_mode == "command_only"


def test_openai_is_default_and_requires_key_and_model():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_settings(environ={"DISCORD_TOKEN": "private-token"})
    settings = load_settings(environ={
        "DISCORD_TOKEN": "private-token", "OPENAI_API_KEY": "private-key", "LLM_MODEL": "test-model",
    })
    assert settings.llm_provider == "openai"


@pytest.mark.parametrize("name,value", [
    ("DISCORD_TOKEN", ""), ("DISCORD_TOKEN", "..."),
    ("DISCORD_GUILD_ID", "invalid-private-value"), ("DISCORD_GUILD_ID", "-1"),
    ("LLM_PROVIDER", "invalid-private-value"), ("DETECTION_MODE", "invalid-private-value"),
    ("ROUND_DEADLINE_HOURS", "0"), ("ROUND_DEADLINE_HOURS", "nan"),
    ("ROUND_DEADLINE_HOURS", "inf"), ("ROUND_DEADLINE_HOURS", "invalid-private-value"),
    ("DETECTION_CONFIDENCE", "1.1"), ("DETECTION_CONFIDENCE", "-0.1"),
    ("DETECTION_COOLDOWN_MIN", "-1"),
])
def test_invalid_settings_are_safely_rejected(name, value):
    with pytest.raises(ConfigError) as failure:
        load_settings(environ={**BASE, name: value})
    assert "invalid-private-value" not in str(failure.value)
    assert "private-token" not in str(failure.value)


def test_dotenv_loaded_without_overriding_process_env(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in (
        "DISCORD_TOKEN", "DISCORD_GUILD_ID", "LLM_PROVIDER", "OPENAI_API_KEY", "VERTEX_API_KEY", "LLM_MODEL",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
        "ROUND_DEADLINE_HOURS", "DETECTION_MODE", "DETECTION_CONFIDENCE", "DETECTION_COOLDOWN_MIN",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text("DISCORD_TOKEN=dotenv-test-token\nLLM_PROVIDER=fake\nROUND_DEADLINE_HOURS=24\n")
    monkeypatch.setenv("ROUND_DEADLINE_HOURS", "0.05")
    settings = load_settings(path)
    assert settings.discord_token == "dotenv-test-token"
    assert settings.round_deadline_hours == 0.05


def test_vertex_requires_only_its_own_key_and_shared_model():
    settings = load_settings(environ={
        "DISCORD_TOKEN": "test-token", "LLM_PROVIDER": "vertex",
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "VERTEX_API_KEY": " vertex-test-key ", "LLM_MODEL": " test-vertex-model ",
        "OPENAI_API_KEY": "...",
    })
    assert settings.llm_provider == "vertex"
    assert settings.vertex_api_key == "vertex-test-key"
    assert settings.llm_model == "test-vertex-model"
    assert "vertex-test-key" not in repr(settings)


@pytest.mark.parametrize("key,model", [
    ("", "test-model"), ("...", "test-model"), ("   ", "test-model"),
    ("vertex-private-key", ""), ("vertex-private-key", "..."), ("vertex-private-key", "   "),
])
def test_invalid_vertex_configuration_is_value_free(key, model):
    with pytest.raises(ConfigError, match="VERTEX_API_KEY, GOOGLE_CLOUD_PROJECT, and LLM_MODEL") as failure:
        load_settings(environ={
            "DISCORD_TOKEN": "test-token", "LLM_PROVIDER": "vertex",
        "GOOGLE_CLOUD_PROJECT": "test-project",
            "VERTEX_API_KEY": key, "LLM_MODEL": model,
        })
    assert "vertex-private-key" not in str(failure.value)


def test_vertex_config_loaded_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    path = tmp_path / ".env"
    path.write_text("DISCORD_TOKEN=test-token\nLLM_PROVIDER=vertex\nGOOGLE_CLOUD_PROJECT=test-project\nVERTEX_API_KEY=dotenv-vertex-test-key\nLLM_MODEL=test-vertex-model\n")
    settings = load_settings(path)
    assert settings.llm_provider == "vertex" and settings.vertex_api_key == "dotenv-vertex-test-key"
    assert settings.llm_model == "test-vertex-model"


@pytest.mark.parametrize("name,value", [
    ("GOOGLE_CLOUD_PROJECT", ""), ("GOOGLE_CLOUD_PROJECT", "..."),
    ("GOOGLE_CLOUD_PROJECT", "bad/project"),
    ("GOOGLE_CLOUD_LOCATION", ""), ("GOOGLE_CLOUD_LOCATION", "us-central1"),
    ("LLM_MODEL", "projects/other-project/locations/global/publishers/google/models/test"),
    ("LLM_MODEL", "model?key=private-key"),
    ("VERTEX_API_KEY", ""),
])
def test_vertex_requires_explicit_cloud_configuration_without_developer_fallback(name, value):
    env = {
        **BASE, "LLM_PROVIDER": "vertex", "GOOGLE_CLOUD_PROJECT": "test-project",
        "VERTEX_API_KEY": "synthetic-vertex-key", "LLM_MODEL": "test-model",
        "GEMINI_API_KEY": "must-not-use", "GOOGLE_API_KEY": "must-not-use",
        name: value,
    }
    with pytest.raises(ConfigError) as failure:
        load_settings(environ=env)
    assert "synthetic-vertex-key" not in str(failure.value)
    assert "private-key" not in str(failure.value)


@pytest.mark.parametrize("project", ["test-project", "123456789012"])
def test_vertex_defaults_to_global_and_accepts_project_ids_or_numbers(project):
    settings = load_settings(environ={
        **BASE, "LLM_PROVIDER": "vertex", "GOOGLE_CLOUD_PROJECT": project,
        "VERTEX_API_KEY": "synthetic-vertex-key", "LLM_MODEL": "test-model",
    })
    assert settings.google_cloud_project == project and settings.google_cloud_location == "global"


def test_old_developer_provider_name_is_rejected():
    with pytest.raises(ConfigError, match="openai, fake, or vertex"):
        load_settings(environ={**BASE, "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "must-not-use"})
