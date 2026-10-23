"""Environment configuration; validation errors never include supplied values."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping
import math
import os
import re

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Safe, value-free configuration error."""


@dataclass(frozen=True)
class Settings:
    discord_token: str = field(repr=False)
    discord_guild_id: int | None = None
    llm_provider: Literal["openai", "fake", "vertex"] = "openai"
    openai_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    detection_mode: Literal["auto", "command_only"] = "auto"
    round_deadline_hours: float = 24
    detection_confidence: float = 0.75
    detection_cooldown_min: float = 15
    vertex_api_key: str = field(default="", repr=False)
    google_cloud_project: str = ""
    google_cloud_location: str = "global"


def load_settings(
    env_file: str | Path = ".env", *, environ: Mapping[str, str] | None = None
) -> Settings:
    """Explicit mappings skip dotenv, making tests independent of local secrets."""
    if environ is None:
        load_dotenv(env_file, override=False)
        environ = os.environ

    def value(name: str, default: str = "") -> str:
        return environ.get(name, default).strip()

    def positive_number(name: str, default: str) -> float:
        try:
            number = float(value(name, default))
        except ValueError:
            raise ConfigError(f"{name} must be a finite positive number.") from None
        if not math.isfinite(number) or number <= 0:
            raise ConfigError(f"{name} must be a finite positive number.")
        return number

    token = value("DISCORD_TOKEN")
    if not token or token == "...":
        raise ConfigError("Set DISCORD_TOKEN in .env.")
    guild_value = value("DISCORD_GUILD_ID")
    guild_id = None
    if guild_value:
        if not guild_value.isdecimal() or int(guild_value) <= 0:
            raise ConfigError("DISCORD_GUILD_ID must be a positive Discord id or empty.")
        guild_id = int(guild_value)
    provider = value("LLM_PROVIDER", "openai")
    if provider not in {"openai", "fake", "vertex"}:
        raise ConfigError("LLM_PROVIDER must be openai, fake, or vertex.")
    mode = value("DETECTION_MODE", "auto")
    if mode not in {"auto", "command_only"}:
        raise ConfigError("DETECTION_MODE must be auto or command_only.")
    key, model = value("OPENAI_API_KEY"), value("LLM_MODEL")
    if provider == "openai" and (not key or key == "..." or not model or model == "..."):
        raise ConfigError("Set OPENAI_API_KEY and LLM_MODEL, or use LLM_PROVIDER=fake.")
    vertex_key = value("VERTEX_API_KEY")
    project = value("GOOGLE_CLOUD_PROJECT")
    location = value("GOOGLE_CLOUD_LOCATION", "global")
    if provider == "vertex":
        if not vertex_key or vertex_key == "..." or not project or project == "..." or not model or model == "...":
            raise ConfigError("Set VERTEX_API_KEY, GOOGLE_CLOUD_PROJECT, and LLM_MODEL, or use LLM_PROVIDER=fake.")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]+", project):
            raise ConfigError("GOOGLE_CLOUD_PROJECT must be a Google Cloud project id or number.")
        if location != "global":
            raise ConfigError("GOOGLE_CLOUD_LOCATION must be global.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
            raise ConfigError("LLM_MODEL must be a model id, without a URL or resource path.")
    confidence = positive_number("DETECTION_CONFIDENCE", "0.75")
    if confidence > 1:
        raise ConfigError("DETECTION_CONFIDENCE must be between 0 and 1.")
    return Settings(
        discord_token=token,
        discord_guild_id=guild_id,
        llm_provider=provider,
        openai_api_key=key,
        llm_model=model,
        detection_mode=mode,
        round_deadline_hours=positive_number("ROUND_DEADLINE_HOURS", "24"),
        detection_confidence=confidence,
        detection_cooldown_min=positive_number("DETECTION_COOLDOWN_MIN", "15"),
        vertex_api_key=vertex_key,
        google_cloud_project=project,
        google_cloud_location=location,
    )
