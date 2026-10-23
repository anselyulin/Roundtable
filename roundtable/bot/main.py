"""Gateway entry point: run from roundtable/ with python -m bot.main."""

import logging
import sys

import discord

from bot.commands import ChannelCommands, ClarifyController, DetectionController, RoundController, RoundtableTree, TaskController, register_clarify_command, register_commands, register_round_commands, register_task_commands
from bot.scheduler import RoundScheduler
from bot.views import ClarifyFeedbackButton, MODAL_PATTERN, REOPEN_MODAL_PATTERN, TASK_MODAL_PATTERN
from core.clarify import ClarifyService
from core.config import ConfigError, Settings, load_settings
from core.db import Database
from core.detection import DetectionService
from core.llm import make_provider
from core.rounds import RoundService
from core.tasks import TaskService
from core.timeline import TimelineService


def configure_private_logging() -> None:
    """Disable all stdlib logging, including SDK debug logs and exception payloads."""
    logging.disable(sys.maxsize)


class RoundtableClient(discord.Client):
    def __init__(self, settings: Settings, db: Database) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True
        intents.message_content = True
        super().__init__(
            intents=intents, max_messages=None,
            allowed_mentions=discord.AllowedMentions.none(),
            enable_debug_events=False,
        )
        self.settings = settings
        self.db = db
        self.provider = make_provider(settings)
        self.handlers = ChannelCommands(db)
        self.rounds = RoundService(db, self.provider, settings.round_deadline_hours)
        self.rounds_ui = RoundController(self, self.handlers, self.rounds)
        self.clarify = ClarifyService(db, self.provider)
        self.clarify_ui = ClarifyController(self, self.handlers, self.clarify)
        self.detection = DetectionService(db, self.provider, settings)
        self.detection_ui = DetectionController(self, self.handlers, self.detection)
        self.tasks = TaskService(db, self.provider)
        self.timeline = TimelineService(db, self.provider)
        self.tasks_ui = TaskController(self, self.handlers, self.tasks, self.timeline)
        self.handlers.on_pause = self.clear_channel_work
        self.scheduler = RoundScheduler(self, self.rounds_ui)
        self.tree = RoundtableTree(self)
        register_commands(self.tree, self.handlers)
        register_round_commands(self.tree, self.rounds_ui)
        register_clarify_command(self.tree, self.clarify_ui)
        register_task_commands(self.tree, self.tasks_ui)

    def clear_channel_work(self, channel_id):
        self.detection_ui.clear_channel(channel_id)
        self.rounds.invalidate(channel_id)
        self.clarify.invalidate(channel_id)
        self.tasks.invalidate(channel_id)
        self.timeline.invalidate(channel_id)

    async def setup_hook(self) -> None:
        self.db.cancel_unpublished_rounds()
        self.db.cancel_unpublished_prompts()
        self.db.cancel_unpublished_task_batches()
        self.rounds_ui.register_views()
        self.add_dynamic_items(ClarifyFeedbackButton)
        self.detection_ui.register_views()
        self.tasks_ui.register_views()
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.scheduler.run.start()

    async def close(self):
        await self.scheduler.stop()
        self.clarify.clear_feedback()
        self.detection_ui.close()
        self.timeline.clear()
        await super().close()

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.modal_submit:
            return
        task = TASK_MODAL_PATTERN.fullmatch(interaction.data.get("custom_id", ""))
        if task is not None:
            try:
                await self.tasks_ui.submit_task(interaction, int(task["channel_id"]), int(task["user_id"]), int(task["round_id"]))
            except Exception:
                await self.rounds_ui.fail(interaction)
            return
        reopen = REOPEN_MODAL_PATTERN.fullmatch(interaction.data.get("custom_id", ""))
        if reopen is not None and reopen["action"] == "submit":
            try:
                await self.rounds_ui.perform_reopen(
                    interaction, int(reopen["channel_id"]), int(reopen["user_id"]),
                    int(reopen["round_id"]), modal=True,
                )
            except Exception:
                await self.rounds_ui.fail(interaction)
            return
        match = MODAL_PATTERN.fullmatch(interaction.data.get("custom_id", ""))
        if match is None or match["action"] != "submit":
            return
        try:
            await self.rounds_ui.submit_modal(
                interaction, int(match["round_id"]), int(match["user_id"]),
                int(match["selection"]), match["named"] == "1",
            )
        except Exception:
            await self.rounds_ui.fail(interaction)

    async def on_message(self, message: discord.Message) -> None:
        if self.settings.detection_mode == "command_only":
            return
        await self.detection_ui.on_message(message)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        # The default implementation logs event arguments and tracebacks.
        # Keep even unexpected failures silent rather than exposing message text.
        return


def main() -> int:
    configure_private_logging()
    try:
        settings = load_settings()
    except ConfigError as error:
        print(str(error), file=sys.stderr)  # Only value-free, application-owned text.
        return 1
    db = Database()
    try:
        client = RoundtableClient(settings, db)
        client.run(settings.discord_token, log_handler=None)
    except Exception:
        print("Roundtable could not start or connect. Check configuration and Discord permissions.", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
