from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
import ast
import logging
import sqlite3

from discord import app_commands
import pytest

from bot.formatting import GENERIC_ERROR, PAUSED_NOTICE
from bot.main import RoundtableClient, configure_private_logging, main
from core.config import ConfigError
from core.llm import FakeProvider


async def test_client_has_exact_commands_required_intents_and_no_history_cache(db, settings):
    client = RoundtableClient(settings, db)
    try:
        assert isinstance(client.provider, FakeProvider)
        assert {command.name for command in client.tree.get_commands()} == {"rt-setup", "rt-pause", "rt-resume", "rt-help", "rt-decide", "rt-log", "re-open", "Ask Roundtable privately", "rt-tasks", "rt-deadline", "rt-timeline", "rt-stats"}
        assert all(command.guild_only for command in client.tree.get_commands())
        assert client.intents.guilds and client.intents.guild_messages
        assert client.intents.members and client.intents.message_content
        assert not client.intents.dm_messages
        assert client._connection._messages is None
        assert not client.persistent_views
    finally:
        await client.close()


@pytest.mark.parametrize("guild_id", [None, 123])
async def test_commands_sync_once_in_setup_hook(db, settings, guild_id):
    client = RoundtableClient(replace(settings, discord_guild_id=guild_id), db)
    try:
        client.tree.sync = AsyncMock()
        with patch.object(client.tree, "copy_global_to", wraps=client.tree.copy_global_to) as copy:
            await client.setup_hook()
        client.tree.sync.assert_awaited_once()
        if guild_id:
            assert client.tree.sync.call_args.kwargs["guild"].id == 123
            copy.assert_called_once()
            assert len(client.tree.get_commands(guild=client.tree.sync.call_args.kwargs["guild"])) == 12
        else:
            client.tree.sync.assert_awaited_once_with()
            copy.assert_not_called()
    finally:
        await client.close()


@pytest.mark.parametrize("state", ["unknown", "enabled", "paused", "detection_disabled"])
async def test_messages_never_inspected_in_command_only_mode(db, settings, state):
    class UnreadableMessage:
        def __getattribute__(self, name):
            raise AssertionError("Message fields must never be inspected in step 1.")

    if state != "unknown":
        db.ensure_channel(200, 100)
        db.enable_channel(200)
        if state == "paused":
            db.set_paused(200, True)
        if state == "detection_disabled":
            db.connection.execute("UPDATE channels SET detection_enabled = 0")
    client = RoundtableClient(replace(settings, detection_mode="command_only"), db)
    try:
        client.provider.complete = AsyncMock()
        await client.on_message(UnreadableMessage())
        client.provider.complete.assert_not_awaited()
        for table in ["rounds", "responses", "round_members", "metrics", "tasks", "deadlines", "member_prefs"]:
            assert db.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["text", "attachment", "embed", "bot", "dm", "system"])
async def test_no_message_text_in_db_file_or_logs(db, settings, interaction, caplog, tmp_path, kind):
    # Core operations use in-memory SQLite. Only a snapshot is written for the
    # explicit file-level privacy check required by the spec.
    from types import SimpleNamespace
    secret_text = "UNIQUE_PRIVATE_CHAT_SENTINEL_4f932"
    client = RoundtableClient(settings, db)
    try:
        await client.handlers.setup(interaction)
        message = SimpleNamespace(
            content=secret_text, attachments=[secret_text] if kind == "attachment" else [],
            embeds=[secret_text] if kind == "embed" else [],
            author=SimpleNamespace(bot=kind == "bot"), guild=None if kind == "dm" else interaction.guild,
            channel=interaction.channel, is_system=lambda: kind == "system",
        )
        with caplog.at_level(logging.DEBUG):
            await client.on_message(message)
            await client.handlers.pause(interaction)
            await client.on_message(message)
            await client.handlers.resume(interaction)
            await client.on_error("on_message", message)
        db.connection.commit()
        assert secret_text not in "\n".join(db.connection.iterdump())
        snapshot = tmp_path / "privacy-check.db"
        with sqlite3.connect(snapshot) as disk:
            db.connection.backup(disk)
        assert secret_text.encode() not in snapshot.read_bytes()
        assert secret_text not in caplog.text
        assert all(secret_text not in str(call) for call in interaction.channel.send.await_args_list)
    finally:
        await client.close()


@pytest.mark.parametrize("paused", [False, True])
async def test_unexpected_command_errors_do_not_log_exception_content(db, settings, interaction, caplog, paused):
    client = RoundtableClient(settings, db)
    try:
        if paused:
            db.ensure_channel(200, 100)
            db.enable_channel(200)
            db.set_paused(200, True)
        await client.tree.on_error(interaction, app_commands.AppCommandError("PRIVATE_ERROR_BODY"))
        assert "PRIVATE_ERROR_BODY" not in caplog.text
        expected = PAUSED_NOTICE if paused else GENERIC_ERROR
        assert interaction.followup.send.call_args.args[0] == expected
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        interaction.channel.send.assert_not_awaited()
    finally:
        await client.close()


def test_sdk_logging_is_disabled_even_at_debug(caplog):
    previous = logging.root.manager.disable
    try:
        with caplog.at_level(logging.DEBUG):
            configure_private_logging()
            for name in ["discord", "discord.http", "discord.gateway", "openai", "google.genai", "google.genai.models", "httpx", "httpcore"]:
                logging.getLogger(name).critical("PRIVATE_SDK_PAYLOAD")
        assert "PRIVATE_SDK_PAYLOAD" not in caplog.text
    finally:
        logging.disable(previous)


def test_main_reports_config_failure_without_secrets(capsys):
    previous = logging.root.manager.disable
    try:
        with patch("bot.main.load_settings", side_effect=ConfigError("Set DISCORD_TOKEN in .env.")):
            assert main() == 1
        assert capsys.readouterr().err == "Set DISCORD_TOKEN in .env.\n"
    finally:
        logging.disable(previous)


def test_main_never_prints_raw_startup_exception(db, settings, capsys):
    previous = logging.root.manager.disable
    try:
        with patch("bot.main.load_settings", return_value=settings), patch("bot.main.Database", return_value=db), patch("bot.main.RoundtableClient") as client:
            client.return_value.run.side_effect = RuntimeError("PRIVATE_GATEWAY_BODY")
            assert main() == 1
            client.return_value.run.assert_called_once_with(settings.discord_token, log_handler=None)
        assert "PRIVATE_GATEWAY_BODY" not in capsys.readouterr().err
    finally:
        logging.disable(previous)


def test_core_imports_no_platform_packages():
    for path in (Path(__file__).parents[1] / "core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith(("discord", "bot")) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("discord", "bot"))
