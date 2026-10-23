"""Discord transport for commands, explicit clarify, and bounded automatic intake."""

import asyncio

import discord
from discord import app_commands

from bot import formatting
from bot.formatting import (
    GENERIC_ERROR, HELP_TEXT, PAUSED_NOTICE, RESUMED_NOTICE,
    SETUP_REQUIRED, TRANSPARENCY_NOTICE,
)
from core.db import Database
from core.clarify import ClarifyService
from core.detection import DetectionService
from core.rounds import RoundError, RoundService, now_utc
from core.tasks import TaskService, proposal_fingerprint
from core.timeline import TimelineService


class ChannelCommands:
    def __init__(self, db: Database) -> None:
        self.db = db
        # Serialize control actions so no setup post can race a completed pause.
        self._locks: dict[int, asyncio.Lock] = {}
        self.on_pause = None

    def valid_context(self, interaction: discord.Interaction) -> bool:
        guild, channel, member = interaction.guild, interaction.channel, interaction.user
        return bool(
            guild is not None
            and isinstance(channel, (discord.TextChannel, discord.Thread))
            and isinstance(member, discord.Member)
            and member.guild.id == guild.id
            and channel.guild.id == guild.id
            and channel.permissions_for(member).view_channel
        )

    async def _begin(self, interaction: discord.Interaction, *, thinking: bool = True) -> bool:
        if interaction.user.bot:
            return False
        await interaction.response.defer(ephemeral=True, thinking=thinking)
        if not self.valid_context(interaction):
            await interaction.followup.send("Use this command in a server text channel you can view.", ephemeral=True)
            return False
        return True

    def _lock(self, interaction: discord.Interaction) -> asyncio.Lock:
        return self.lock_for(interaction.channel.id)

    def lock_for(self, channel_id: int) -> asyncio.Lock:
        return self._locks.setdefault(channel_id, asyncio.Lock())

    async def _reply(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def setup(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        async with self._lock(interaction):
            channel, guild = interaction.channel, interaction.guild
            state = self.db.get_channel(channel.id)
            if state and state.paused:
                await self._reply(interaction, PAUSED_NOTICE)
                return
            if (
                interaction.user.id != guild.owner_id
                and not channel.permissions_for(interaction.user).manage_channels
            ):
                await self._reply(interaction, "You need Manage Channels permission or must be the server owner to run /rt-setup.")
                return
            state = self.db.ensure_channel(channel.id, guild.id)
            try:
                notice = None
                if state.notice_message_id:
                    try:
                        notice = await channel.get_partial_message(int(state.notice_message_id)).edit(
                            content=TRANSPARENCY_NOTICE,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.NotFound:
                        pass
                if notice is None:
                    notice = await channel.send(
                        TRANSPARENCY_NOTICE, allowed_mentions=discord.AllowedMentions.none()
                    )
                    # Remember even an unpinned notice so permission fixes don't duplicate it.
                    self.db.set_notice(channel.id, notice.id)
                await notice.pin(reason="Roundtable transparency notice")
            except discord.HTTPException:
                await self._reply(interaction, "I couldn't post and pin the notice. Check my channel permissions, including Pin Messages, then run /rt-setup again.")
                return
            self.db.enable_channel(channel.id)
            await self._reply(interaction, "Roundtable is enabled here. The transparency notice is pinned.")

    async def pause(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        async with self._lock(interaction):
            channel = interaction.channel
            if not self.db.set_paused(channel.id, True):
                await self._reply(interaction, SETUP_REQUIRED)
                return
            if self.on_pause is not None:
                self.on_pause(channel.id)
            # A normal channel post avoids identifying the caller via Discord's
            # public interaction attribution. Remove the ephemeral thinking reply.
            try:
                await channel.send(PAUSED_NOTICE, allowed_mentions=discord.AllowedMentions.none())
                await interaction.delete_original_response()
            except discord.HTTPException:
                await self._reply(interaction, PAUSED_NOTICE)

    async def resume(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        async with self._lock(interaction):
            if not self.db.set_paused(interaction.channel.id, False):
                await self._reply(interaction, SETUP_REQUIRED)
                return
            await self._reply(interaction, RESUMED_NOTICE)

    async def help(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        async with self._lock(interaction):
            state = self.db.get_channel(interaction.channel.id)
            await self._reply(interaction, PAUSED_NOTICE if state and state.paused else HELP_TEXT)


class RoundtableTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        # Do not pass exceptions to Discord's default logger: they can carry content.
        if interaction.user.bot:
            return
        db = self.client.db
        state = db.get_channel(interaction.channel_id) if interaction.channel_id else None
        text = PAUSED_NOTICE if state and state.paused else GENERIC_ERROR
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


def register_commands(tree: app_commands.CommandTree, handlers: ChannelCommands) -> None:
    for name, description, callback in (
        ("rt-setup", "Enable Roundtable and pin its transparency notice.", handlers.setup),
        ("rt-pause", "Pause Roundtable in this channel.", handlers.pause),
        ("rt-resume", "Resume Roundtable in this channel.", handlers.resume),
        ("rt-help", "Show Roundtable's commands privately.", handlers.help),
    ):
        command = app_commands.Command(name=name, description=description, callback=callback)
        command.guild_only = True
        tree.add_command(command)


ERROR_TEXT = {
    "setup": SETUP_REQUIRED,
    "paused": PAUSED_NOTICE,
    "round": "This round isn't available in this channel.",
    "member": "Only a member included in this round can share or view their submission.",
    "closed": "This round is closed or its deadline has passed.",
    "selection": "Select an option before adding your reason.",
    "input": "Please enter a nonempty question or reason (up to 300 characters). For an open-ended round, also enter your own answer (up to 80 characters).",
    "boundary": "I can't read assignment prompts, submissions, or grades, or write your deliverable. Please keep that work with your team and ask me about a team decision instead.",
    "outcome": "This round isn't awaiting an outcome. Check /rt-log for the saved decision.",
    "reopen": "Only closed decisions can be reopened. Choose one from /re-open.",
    "reopen_member": "These controls belong to the member and channel that opened /re-open. Run /re-open here to browse decisions.",
    "reopened": "This decision already has an active reopened round. Use Share my view on that round.",
    "new_option": "Enter a different option. It must not duplicate an existing option or use Another option or None of these.",
    "option_capacity": "A reopened round can have at most 24 options.",
    "clarify_input": "Choose a message with text in this channel (up to 4,000 characters). I ignore attachments and embeds.",
    "clarify_feedback": "Feedback was already recorded or has expired. You can ask for a new private clarification.",
    "clarify_member": "Only the person who asked can rate this clarification, in its original channel.",
    "end_permission": "Only the round starter, server owner, or a member with Manage Channels can end this round early.",
    "detection_prompt": "This decision prompt is no longer available or was already handled. Use /rt-decide to start a round.",
    "date": "Use a valid date in YYYY-MM-DD format. Dates use the end of that UTC day.",
    "date_past": "Choose an assignment deadline on or after today (UTC).",
    "task": "This task isn't available in this channel. Check /rt-tasks list.",
    "task_done": "Only a confirmed, unfinished task can be marked done. Check /rt-tasks list.",
    "task_round": "Log this decision's outcome before adding linked tasks.",
    "task_members": "Choose a current non-bot member who can view this channel.",
    "task_unowned": "There are no unowned tasks to assign. Add one with /rt-tasks add.",
    "task_stale": "Tasks, members, or preferences changed. Check /rt-tasks list and try again.",
    "task_proposal": "These owner suggestions expired or were already handled. Use /rt-tasks assign for unowned tasks.",
    "task_private": "These private controls belong to the member and channel that opened them.",
    "task_input": "Enter your own task title (up to 200 characters) or preference note (up to 300 characters).",
    "deadline": "Add an upcoming assignment deadline with /rt-deadline add first.",
    "deadline_soon": "That deadline is too close for a timeline with a one-day buffer. Please plan the remaining work together.",
    "timeline_preview": "This private preview expired or was already posted. Run /rt-timeline for a fresh proposal.",
    "timeline_stale": "The dates or tasks changed. Run /rt-timeline for a fresh proposal.",
}


class RoundController:
    """Discord transport for core rounds; all channel sends share the pause lock."""

    def __init__(self, client, controls: ChannelCommands, service: RoundService):
        self.client, self.controls, self.service = client, controls, service
        self.db = service.db
        self._working: set[int] = set()
        # Reconcile unlogged closed results once after every restart. Re-editing
        # their known bot message avoids an outbox table or duplicate result posts.
        self.pending_results = set(self.db.unlogged_closed_round_ids())
        self.pending_outcome_views = set(self.db.logged_round_ids())
        self.pending_share_views = {
            round_id for round_id in self.db.restorable_round_ids()
            if self.db.get_round(round_id).status == "collecting"
        }

    async def fail(self, interaction, error: RoundError | None = None):
        if interaction.user.bot:
            return
        state = self.db.get_channel(interaction.channel_id) if interaction.channel_id else None
        text = PAUSED_NOTICE if state and state.paused else ERROR_TEXT.get(error.code if error else "", GENERIC_ERROR)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            await self.controls._reply(interaction, text)
        except discord.HTTPException:
            pass

    def check(self, interaction, round_id, owner_id=None, *, collecting=True, public=False):
        if owner_id is not None and interaction.user.id != owner_id:
            raise RoundError("member")
        if collecting:
            record = self.service.collecting(round_id, interaction.channel.id, interaction.user.id, now_utc())
        else:
            record = self.service.get(round_id, interaction.channel.id)
        if public and (interaction.message is None or str(interaction.message.id) != record.public_message_id):
            raise RoundError("round")
        return record

    async def visible_members(self, channel, guild):
        if not guild.chunked:
            await guild.chunk(cache=True)
        thread_ids = None
        if isinstance(channel, discord.Thread) and channel.type == discord.ChannelType.private_thread:
            thread_ids = {member.id for member in await channel.fetch_members()}
        return tuple(
            str(member.id) for member in guild.members
            if not member.bot and channel.permissions_for(member).view_channel
            and (thread_ids is None or member.id in thread_ids or channel.permissions_for(member).manage_threads)
        )

    async def decide(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 300]):
        if not await self.controls._begin(interaction):
            return
        try:
            prepared = await self.service.prepare_question(interaction.channel.id, question)
            self.service.require_active(interaction.channel.id)
            member_ids = await self.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                record = self.service.start(interaction.channel.id, interaction.user.id, prepared, member_ids, now_utc())
                if not await self.post_round(interaction, record, len(set(member_ids))):
                    return
                await self.controls._reply(interaction, "Round started. Use Share my view in the channel; your submission is private until close.")
        except RoundError as error:
            await self.fail(interaction, error)

    async def post_round(self, interaction, record, total: int) -> bool:
        # Caller holds the channel pause lock for both ordinary and reopened rounds.
        from bot.views import ShareView
        try:
            message = await interaction.channel.send(
                formatting.starting_text(record), embed=formatting.progress_embed(0, total),
                view=ShareView(self, record.id), allowed_mentions=discord.AllowedMentions.none(),
            )
            self.db.set_round_message(record.id, message.id)
        except discord.HTTPException:
            self.db.cancel_round(record.id)
            await self.fail(interaction)
            return False
        return True

    async def end_round(self, interaction, round_id: int):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.check(interaction, round_id, collecting=False, public=True)
                can_manage = (
                    interaction.user.id == interaction.guild.owner_id
                    or interaction.channel.permissions_for(interaction.user).manage_channels
                )
                self.service.end_early(round_id, interaction.channel.id, interaction.user.id, now_utc(), can_manage=can_manage)
                self.pending_results.add(round_id)
                replied, total = self.service.progress(round_id)
                await self.controls._reply(interaction, f"Round closed. Preparing the result from {replied} of {total} views.")
            await self.maybe_close(round_id, interaction=interaction)
        except RoundError as error:
            await self.fail(interaction, error)

    async def refresh_share_view(self, round_id: int):
        record = self.db.get_round(round_id)
        if record is None or record.status != "collecting" or not record.public_message_id:
            self.pending_share_views.discard(round_id)
            return
        channel = await self.resolve_channel(record)
        async with self.controls.lock_for(int(record.channel_id)):
            self.service.require_active(record.channel_id)
            record = self.db.get_round(round_id)
            if record.status == "collecting":
                from bot.views import ShareView
                try:
                    await channel.get_partial_message(int(record.public_message_id)).edit(
                        content=formatting.starting_text(record), view=ShareView(self, round_id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.NotFound:
                    # Existing close-time recovery replaces deleted starters.
                    pass
            self.pending_share_views.discard(round_id)

    def check_reopen_controls(self, interaction, channel_id: int, user_id: int):
        if interaction.channel_id != channel_id or interaction.user.id != user_id:
            raise RoundError("reopen_member")
        self.service.require_active(channel_id)

    async def reopen(self, interaction: discord.Interaction):
        await self.browse_reopen(interaction, interaction.channel_id, interaction.user.id, 0, edit=False)

    async def browse_reopen(self, interaction, channel_id: int, user_id: int, page: int, *, edit: bool = True):
        if not await self.controls._begin(interaction, thinking=not edit):
            return
        try:
            async with self.controls._lock(interaction):
                self.check_reopen_controls(interaction, channel_id, user_id)
                history = self.service.history(channel_id, page)
                names = self.names(interaction.guild, [entry.owner_user_id for entry in history.entries if entry.owner_user_id])
                embed = formatting.decision_log(history.entries, names)
                embed.set_footer(text=f"Page {history.page + 1} of {history.total_pages}")
                from bot.views import ReopenHistoryView
                payload = dict(
                    content="Browse previous decisions, then choose one to reopen.", embed=embed,
                    view=ReopenHistoryView(channel_id, user_id, history),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if edit:
                    await interaction.edit_original_response(**payload)
                else:
                    await interaction.followup.send(**payload, ephemeral=True)
        except RoundError as error:
            await self.fail(interaction, error)

    async def choose_reopen(self, interaction, channel_id: int, user_id: int, page: int):
        if not await self.controls._begin(interaction, thinking=False):
            return
        try:
            async with self.controls._lock(interaction):
                self.check_reopen_controls(interaction, channel_id, user_id)
                values = interaction.data.get("values", [])
                if len(values) != 1 or not isinstance(values[0], str) or not values[0].isdigit():
                    raise RoundError("reopen")
                round_id = int(values[0])
                history = self.service.history(channel_id, page)
                if round_id not in {entry.id for entry in history.entries}:
                    raise RoundError("reopen")
                record = self.service.closed_decision(round_id, channel_id)
                from bot.views import ReopenDecisionView
                await interaction.edit_original_response(
                    content="Vote again with the previous options, or add one new option and reopen.",
                    embed=formatting.reopen_details(record, self.service.choices(record)),
                    view=ReopenDecisionView(channel_id, user_id, history.page, record.id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except RoundError as error:
            await self.fail(interaction, error)

    async def open_reopen_option(self, interaction, channel_id: int, user_id: int, page: int, round_id: int):
        if interaction.user.bot:
            return
        try:
            if not self.controls.valid_context(interaction):
                raise RoundError("reopen_member")
            self.check_reopen_controls(interaction, channel_id, user_id)
            self.service.closed_decision(round_id, channel_id)
            from bot.views import ReopenOptionModal
            # send_modal acknowledges immediately, before any network/model work.
            await interaction.response.send_modal(ReopenOptionModal(channel_id, user_id, page, round_id))
        except RoundError as error:
            await self.fail(interaction, error)

    async def perform_reopen(self, interaction, channel_id: int, user_id: int, round_id: int, *, modal: bool = False):
        if not await self.controls._begin(interaction):
            return
        try:
            self.check_reopen_controls(interaction, channel_id, user_id)
            new_option = None
            if modal:
                fields = [field for row in interaction.data.get("components", []) for field in row.get("components", [])]
                if len(fields) != 1 or fields[0].get("custom_id") != "rt:new_option" or not isinstance(fields[0].get("value"), str):
                    raise RoundError("input")
                new_option = fields[0]["value"]
            members = await self.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                record = self.service.reopen(round_id, channel_id, user_id, members, now_utc(), new_option=new_option)
                if not await self.post_round(interaction, record, len(set(members))):
                    return
                await self.controls._reply(interaction, f"Decision reopened as round {record.id}. Use Share my view in the channel to submit again.")
        except RoundError as error:
            await self.fail(interaction, error)

    async def log(self, interaction: discord.Interaction):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                entries = self.service.log(interaction.channel.id)
                names = self.names(interaction.guild, [entry.owner_user_id for entry in entries if entry.owner_user_id])
                await interaction.followup.send(embed=formatting.decision_log(entries, names), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except RoundError as error:
            await self.fail(interaction, error)

    @staticmethod
    def names(guild, ids):
        return {user_id: member.display_name[:80] for user_id in ids if (member := guild.get_member(int(user_id))) is not None}

    async def share(self, interaction, round_id: int):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                record = self.check(interaction, round_id, public=True)
                own = next((response for response in self.db.round_responses(round_id) if response.user_id == str(interaction.user.id)), None)
                selection = record.options.index(own.option_label) if own and record.options else (-1 if record.options else 0)
                from bot.views import PrivateShareView
                await interaction.followup.send(
                    formatting.share_instructions(record), ephemeral=True,
                    view=PrivateShareView(record, interaction.user.id, selection, own.show_name if own else False),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except RoundError as error:
            await self.fail(interaction, error)

    async def select_draft(self, interaction, round_id, user_id, named):
        if not await self.controls._begin(interaction, thinking=False):
            return
        try:
            async with self.controls._lock(interaction):
                record = self.check(interaction, round_id, user_id)
                values = interaction.data.get("values", [])
                if len(values) != 1 or not isinstance(values[0], str) or not values[0].isdigit():
                    raise RoundError("selection")
                selection = int(values[0])
                self.service.option(record, selection)
                await self.edit_draft(interaction, record, selection, named)
        except RoundError as error:
            await self.fail(interaction, error)

    async def change_draft(self, interaction, round_id, user_id, selection, named):
        if not await self.controls._begin(interaction, thinking=False):
            return
        try:
            async with self.controls._lock(interaction):
                record = self.check(interaction, round_id, user_id)
                if selection != -1:
                    self.service.option(record, selection)
                await self.edit_draft(interaction, record, selection, named)
        except RoundError as error:
            await self.fail(interaction, error)

    async def edit_draft(self, interaction, record, selection, named):
        from bot.views import PrivateShareView
        await interaction.edit_original_response(
            content=formatting.share_instructions(record),
            view=PrivateShareView(record, interaction.user.id, selection, named),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def open_reason(self, interaction, round_id, user_id, selection, named):
        if interaction.user.bot:
            return
        try:
            if not self.controls.valid_context(interaction):
                raise RoundError("member")
            record = self.check(interaction, round_id, user_id)
            self.service.option(record, selection)
            own = next((response for response in self.db.round_responses(round_id) if response.user_id == str(user_id)), None)
            from bot.views import ReasonModal
            # send_modal is itself the immediate acknowledgement. Discord forbids
            # opening a modal after deferring; no network/LLM work precedes it.
            await interaction.response.send_modal(ReasonModal(
                round_id, user_id, selection, named, own.reason if own else "",
                answer=(own.option_label if own else "") if not record.options else None,
            ))
        except RoundError as error:
            await self.fail(interaction, error)

    async def submit_modal(self, interaction, round_id, user_id, selection, named):
        if not await self.controls._begin(interaction):
            return
        try:
            record = self.check(interaction, round_id, user_id)
            self.service.option(record, selection)
            # Read only this member's submitted text input, after all guards.
            fields = [field for row in interaction.data.get("components", []) for field in row.get("components", [])]
            expected = {"rt:reason"} if record.options else {"rt:answer", "rt:reason"}
            values = {field.get("custom_id"): field.get("value") for field in fields}
            if len(fields) != len(expected) or set(values) != expected or any(not isinstance(value, str) for value in values.values()):
                raise RoundError("input")
            await self.service.save_response(
                round_id=round_id, channel_id=interaction.channel.id, user_id=user_id,
                selection=selection, reason=values["rt:reason"], show_name=named,
                answer=values.get("rt:answer"),
            )
            async with self.controls._lock(interaction):
                self.service.require_active(interaction.channel.id)
                current = self.db.get_round(round_id)
                if current.status == "collecting":
                    try:
                        await interaction.channel.get_partial_message(int(current.public_message_id)).edit(
                            embed=formatting.progress_embed(*self.service.progress(round_id))
                        )
                    except discord.HTTPException:
                        pass
                await self.controls._reply(interaction, "Saved. You can change your answer until the round closes.")
            await self.maybe_close(round_id, interaction=interaction)
        except RoundError as error:
            await self.fail(interaction, error)

    async def resolve_channel(self, record):
        self.service.require_active(record.channel_id)
        channel = self.client.get_channel(int(record.channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(record.channel_id))
        state = self.db.get_channel(record.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or str(channel.guild.id) != state.guild_id:
            raise RoundError("round")
        return channel

    async def maybe_close(self, round_id: int, *, interaction=None, now=None):
        if round_id in self._working:
            return
        record = self.db.get_round(round_id)
        if record is None or record.outcome is not None:
            self.pending_results.discard(round_id)
            return
        if not self.service.ready(record, now or now_utc()) and round_id not in self.pending_results:
            return
        self._working.add(round_id)
        try:
            async with self.controls.lock_for(int(record.channel_id)):
                self.service.require_active(record.channel_id)
                if self.service.ready(record, now or now_utc()):
                    # Freeze submissions at the deadline/all-replied boundary,
                    # even when the provider or Discord is temporarily unavailable.
                    self.service.freeze_ready(round_id, now or now_utc())
                    self.pending_results.add(round_id)
            result = await self.service.prepare_result(round_id, now or now_utc())
            channel = await self.resolve_channel(record)
            async with self.controls.lock_for(int(record.channel_id)):
                self.service.require_active(record.channel_id)
                payload = formatting.result_message(result, self.names(channel.guild, [r.user_id for r in result.responses if r.show_name]))
                record = self.service.close_prepared(result, now or now_utc())
                self.pending_results.add(round_id)
                from bot.views import OutcomeView
                view = OutcomeView(self, record, self.service.choices(record))
                try:
                    await channel.get_partial_message(int(record.public_message_id)).edit(
                        **payload, view=view, allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.NotFound:
                    message = await channel.send(**payload, view=view, allowed_mentions=discord.AllowedMentions.none())
                    self.db.set_round_message(round_id, message.id)
                self.pending_results.discard(round_id)
        except RoundError as error:
            if interaction and error.code not in {"stale", "not_ready", "outcome"}:
                await self.fail(interaction, error)
        except discord.HTTPException:
            if interaction:
                await self.fail(interaction)
        finally:
            self._working.discard(round_id)

    async def log_outcome(self, interaction, round_id: int, choice: int):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.check(interaction, round_id, collecting=False, public=True)
                record = self.service.outcome(round_id, interaction.channel.id, interaction.user.id, choice, now_utc())
                self.pending_results.discard(round_id)
                from bot.views import TaskFlowView, OutcomeView
                self.pending_outcome_views.add(round_id)
                try:
                    await interaction.channel.get_partial_message(int(record.public_message_id)).edit(
                        view=OutcomeView(self, record, self.service.choices(record))
                    )
                    self.pending_outcome_views.discard(round_id)
                except discord.HTTPException:
                    pass
                await interaction.channel.send(
                    f"Roundtable (AI): Outcome logged: {formatting.safe(record.outcome)}. Want to turn this into tasks? Add the tasks in your own words; owners are only final after confirmation.",
                    view=TaskFlowView(interaction.channel.id, record.id), allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.controls._reply(interaction, "Outcome saved. See /rt-log for the decision.")
        except RoundError as error:
            await self.fail(interaction, error)

    async def reconcile_outcome_view(self, round_id):
        record = self.db.get_round(round_id)
        if record is None or record.outcome is None:
            self.pending_outcome_views.discard(round_id)
            return
        channel = await self.resolve_channel(record)
        async with self.controls.lock_for(int(record.channel_id)):
            self.service.require_active(record.channel_id)
            from bot.views import OutcomeView
            try:
                await channel.get_partial_message(int(record.public_message_id)).edit(
                    view=OutcomeView(self, record, self.service.choices(record))
                )
            except discord.NotFound:
                # A deleted, already-logged result remains accessible in /rt-log.
                pass
            self.pending_outcome_views.discard(round_id)

    def register_views(self):
        from bot.views import OutcomeView, PrivateButton, PrivateSelect, ReopenButton, ReopenSelect, ShareView
        self.client.add_dynamic_items(PrivateButton, PrivateSelect, ReopenButton, ReopenSelect)
        for round_id in self.db.restorable_round_ids():
            record = self.db.get_round(round_id)
            # A crash may have frozen a round before its starter was edited.
            # Keep the old share id routable so it can give a private closed reply.
            self.client.add_view(ShareView(self, round_id), message_id=int(record.public_message_id))
            if record.status == "closed":
                self.client.add_view(OutcomeView(self, record, self.service.choices(record)), message_id=int(record.public_message_id))


def register_round_commands(tree, controller: RoundController):
    for name, description, callback in (
        ("rt-decide", "Start a private-first decision round.", controller.decide),
        ("rt-log", "Show this channel's last 10 closed decisions privately.", controller.log),
        ("re-open", "Browse past decisions and reopen one with an optional new option.", controller.reopen),
    ):
        command = app_commands.Command(name=name, description=description, callback=callback)
        command.guild_only = True
        tree.add_command(command)


class ClarifyController:
    def __init__(self, client, controls: ChannelCommands, service: ClarifyService):
        self.client, self.controls, self.service = client, controls, service

    async def ask(self, interaction: discord.Interaction, message: discord.Message):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.service.require_active(interaction.channel.id)
                if message.guild is None or message.guild.id != interaction.guild.id or message.channel.id != interaction.channel.id:
                    raise RoundError("clarify_input")
                # Deliberately never access attachments, embeds, authors,
                # referenced messages, history, files, or surrounding chat.
                text = message.content
                generation = self.service.generation(interaction.channel.id)
            result = await self.service.explain(interaction.channel.id, text)
            async with self.controls._lock(interaction):
                self.service.require_current(interaction.channel.id, generation)
                from bot.views import ClarifyFeedbackView
                token = self.service.new_feedback(interaction.channel.id)
                try:
                    await interaction.followup.send(
                        embed=formatting.clarification_embed(result), ephemeral=True,
                        view=ClarifyFeedbackView(interaction.channel.id, interaction.user.id, token),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.service.record_use(interaction.channel.id)
                except Exception:
                    self.service.discard_feedback(token)
                    raise
        except RoundError as error:
            await self.client.rounds_ui.fail(interaction, RoundError("clarify_input") if error.code == "input" else error)
        except Exception:
            await self.client.rounds_ui.fail(interaction)

    async def feedback(self, interaction, channel_id: int, user_id: int, token: str, helpful: bool):
        if not await self.controls._begin(interaction, thinking=False):
            return
        try:
            async with self.controls._lock(interaction):
                if interaction.channel_id != channel_id or interaction.user.id != user_id:
                    raise RoundError("clarify_member")
                self.service.record_feedback(channel_id, token, helpful=helpful)
                from bot.views import ClarifyFeedbackView
                await interaction.edit_original_response(view=ClarifyFeedbackView(channel_id, user_id, token, disabled=True))
        except RoundError as error:
            await self.client.rounds_ui.fail(interaction, error)
        except Exception:
            await self.client.rounds_ui.fail(interaction)


def register_clarify_command(tree: app_commands.CommandTree, controller: ClarifyController) -> None:
    command = app_commands.ContextMenu(name="Ask Roundtable privately", callback=controller.ask)
    command.guild_only = True
    tree.add_command(command)


class DetectionController:
    """Automatic intake and persistent prompt controls. No history fetches."""

    def __init__(self, client, controls: ChannelCommands, service: DetectionService):
        self.client, self.controls, self.service = client, controls, service
        self.db = service.db
        self._expiry: asyncio.TimerHandle | None = None
        self.closed = False
        self.pending_edits = {
            prompt_id for prompt_id in self.db.published_prompt_ids()
            if self.db.get_decision_prompt(prompt_id).status != "pending"
        }

    def schedule_expiry(self):
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        deadline = self.service.next_expiry()
        if deadline is not None and not self.closed:
            delay = max(0.001, (deadline - self.service.clock()).total_seconds())
            self._expiry = asyncio.get_running_loop().call_later(delay, self.expire_windows)

    def expire_windows(self):
        self.service.expire()
        self.schedule_expiry()

    def clear_channel(self, channel_id: int):
        self.service.clear_channel(channel_id)
        self.schedule_expiry()

    def close(self):
        self.closed = True
        self.service.clear()
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None

    async def on_message(self, message: discord.Message):
        try:
            if self.closed or self.service.mode != "auto":
                return
            channel = message.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not self.service.can_read(channel.id):
                return
            state = self.db.get_channel(channel.id)
            if message.guild is None or str(message.guild.id) != state.guild_id or channel.guild.id != message.guild.id:
                return
            if message.author.bot or message.is_system() or message.edited_at is not None:
                return
            # Check presence only. Never inspect attachment/embed objects, files,
            # references, display names, or surrounding messages.
            if message.attachments or message.embeds:
                return
            sent_at = message.created_at
            if not self.service.is_recent(sent_at):
                return
            check = self.service.observe(channel.id, message.author.id, message.content, sent_at)
            self.schedule_expiry()
            if check is None:
                return
            candidate = await self.service.evaluate(check)
            if candidate is None:
                return
            async with self.controls.lock_for(channel.id):
                if self.closed:
                    return
                draft = self.service.reserve_prompt(check, candidate)
                from bot.views import DetectionPromptView
                try:
                    posted = await channel.send(
                        formatting.detection_prompt(draft), view=DetectionPromptView(self, draft),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.service.mark_shown(draft.id, posted.id)
                except Exception:
                    self.db.cancel_unpublished_prompts(draft.id)
                    return
            self.schedule_expiry()
        except Exception:
            # Automatic detection fails silently. Even SDK/Discord exceptions may
            # carry raw message text or response content, so never log them.
            return

    def check_prompt(self, interaction, prompt_id: int):
        prompt = self.service.pending_prompt(prompt_id, interaction.channel.id)
        if interaction.message is None or str(interaction.message.id) != prompt.public_message_id:
            raise RoundError("detection_prompt")
        return prompt

    async def respond(self, interaction, prompt_id: int, accepted: bool):
        if not await self.controls._begin(interaction):
            return
        try:
            self.check_prompt(interaction, prompt_id)
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild) if accepted else ()
            async with self.controls._lock(interaction):
                self.check_prompt(interaction, prompt_id)
                if accepted:
                    record = self.service.accept(prompt_id, interaction.channel.id, interaction.user.id, members)
                    self.pending_edits.add(prompt_id)
                    posted = await self.client.rounds_ui.post_round(interaction, record, len(set(members)))
                    await self.edit_prompt(interaction.channel, prompt_id)
                    if posted:
                        await self.controls._reply(interaction, "Round started. Use Share my view in the channel; your submission is private until close.")
                else:
                    self.service.dismiss(prompt_id, interaction.channel.id)
                    self.pending_edits.add(prompt_id)
                    await self.edit_prompt(interaction.channel, prompt_id)
                    await self.controls._reply(interaction, "Okay, I'll stay quiet for at least 60 minutes.")
            self.schedule_expiry()
        except RoundError as error:
            await self.client.rounds_ui.fail(interaction, error)
        except Exception:
            await self.client.rounds_ui.fail(interaction)

    async def edit_prompt(self, channel, prompt_id: int):
        # Caller holds the shared channel lock and has rechecked pause.
        prompt = self.db.get_decision_prompt(prompt_id)
        from bot.views import DetectionPromptView
        record = self.db.get_round(prompt.round_id) if prompt.round_id is not None else None
        try:
            await channel.get_partial_message(int(prompt.public_message_id)).edit(
                content=formatting.detection_prompt(prompt, record), view=DetectionPromptView(self, prompt),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
        self.pending_edits.discard(prompt_id)

    async def reconcile(self):
        self.expire_windows()
        for prompt_id in self.pending_edits.copy():
            try:
                prompt = self.db.get_decision_prompt(prompt_id)
                channel = await self.client.rounds_ui.resolve_channel(prompt)
                async with self.controls.lock_for(channel.id):
                    self.client.rounds.require_active(channel.id)
                    await self.edit_prompt(channel, prompt_id)
            except Exception:
                continue

    def register_views(self):
        from bot.views import DetectionPromptView
        for prompt_id in self.db.published_prompt_ids():
            prompt = self.db.get_decision_prompt(prompt_id)
            self.client.add_view(DetectionPromptView(self, prompt), message_id=int(prompt.public_message_id))


class TaskController:
    """Task/deadline commands and optional publishing; no chat-history access."""

    def __init__(self, client, controls: ChannelCommands, service: TaskService, timeline: TimelineService):
        self.client, self.controls, self.service, self.timeline = client, controls, service, timeline
        self.db = service.db
        self.pending_edits = set(self.db.published_task_batches())
        self._expired_views = set()

    async def fail(self, interaction, error=None):
        if error is not None and error.code == "input":
            error = RoundError("task_input")
        await self.client.rounds_ui.fail(interaction, error)

    def check_private(self, interaction, channel_id, user_id):
        if interaction.channel_id != channel_id or interaction.user.id != user_id:
            raise RoundError("task_private")
        self.service.require_active(channel_id)

    async def add(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], due: str | None = None):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                task = self.service.add(interaction.channel.id, interaction.user.id, title, due, now_utc())
                await self.controls._reply(interaction, f"Task {task.id} added, unowned. Use /rt-tasks assign for suggestions; owners need confirmation.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def prefs(self, interaction: discord.Interaction, note: app_commands.Range[str, 1, 300]):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.service.prefs(interaction.channel.id, interaction.user.id, note)
                await self.controls._reply(interaction, "Your preference note is saved privately and used only for owner suggestions.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def list_tasks(self, interaction: discord.Interaction):
        await self.show_board(interaction)

    async def show_board(self, interaction, page=0, *, channel_id=None, user_id=None, edit=False):
        if not await self.controls._begin(interaction, thinking=not edit):
            return
        try:
            self.check_private(interaction, channel_id or interaction.channel_id, user_id or interaction.user.id)
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                self.check_private(interaction, channel_id or interaction.channel_id, user_id or interaction.user.id)
                board = self.service.board(interaction.channel.id, members, now_utc(), page)
                names = self.client.rounds_ui.names(interaction.guild, members)
                from bot.views import TaskBoardView
                payload = dict(embed=formatting.task_board(board, names), view=TaskBoardView(interaction.channel.id, interaction.user.id, board), allowed_mentions=discord.AllowedMentions.none())
                if edit:
                    await interaction.edit_original_response(**payload)
                else:
                    await interaction.followup.send(**payload, ephemeral=True)
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def assign(self, interaction: discord.Interaction):
        if not await self.controls._begin(interaction):
            return
        try:
            self.service.require_active(interaction.channel.id)
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild)
            prepared = await self.service.prepare_assign(interaction.channel.id, members, now_utc())
            self.service.require_active(interaction.channel.id)
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                self.service.require_active(interaction.channel.id)
                batch = self.service.propose(interaction.channel.id, prepared, members, now_utc())
                from bot.views import TaskProposalView
                try:
                    tasks = self.db.task_batch(interaction.channel.id, batch)
                    names = self.client.rounds_ui.names(interaction.guild, members)
                    message = await interaction.channel.send(
                        "Roundtable (AI): Here are proposed task owners. The team confirms; nothing is final yet.",
                        embed=formatting.task_proposals(tasks, names), view=TaskProposalView(interaction.channel.id, batch, tasks, now_utc()),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    self.db.publish_task_batch(interaction.channel.id, batch, message.id)
                except Exception:
                    self.db.cancel_task_batch(interaction.channel.id, batch)
                    raise
                await self.controls._reply(interaction, f"Proposed owners for {len(tasks)} tasks. Confirm or swap the suggestions in the channel.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    def check_batch(self, interaction, channel_id, batch, task_id=0, *, public=True):
        if interaction.channel_id != channel_id:
            raise RoundError("task_proposal")
        tasks = self.service.pending(channel_id, batch, now_utc(), task_id)
        message_id = tasks[0].proposal_message_id
        if public and (interaction.message is None or str(interaction.message.id) != message_id):
            raise RoundError("task_proposal")
        return tasks, (str(channel_id), batch, message_id)

    async def proposal_action(self, interaction, channel_id, batch, task_id, owner_id, action):
        if not await self.controls._begin(interaction):
            return
        try:
            self.check_batch(interaction, channel_id, batch, task_id)
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                _, key = self.check_batch(interaction, channel_id, batch, task_id)
                if action == "swap":
                    from bot.views import TaskSwapView
                    await interaction.followup.send("Choose a member. This only changes the proposal; confirmation is still required.", ephemeral=True,
                                                    view=TaskSwapView(channel_id, batch, task_id, interaction.user.id), allowed_mentions=discord.AllowedMentions.none())
                else:
                    if action == "all" and owner_id != proposal_fingerprint(self.db.task_batch(channel_id, batch)):
                        raise RoundError("task_stale")
                    self.service.confirm(channel_id, batch, task_id, None if action == "all" else owner_id, members, now_utc())
                    self.pending_edits.add(key)
                    await self.refresh_batch(interaction.channel, key)
                    await self.controls._reply(interaction, "Task owner confirmed." if task_id else "All remaining owners in this proposal confirmed.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def swap_selected(self, interaction, channel_id, batch, task_id, user_id):
        if not await self.controls._begin(interaction):
            return
        try:
            self.check_private(interaction, channel_id, user_id)
            self.check_batch(interaction, channel_id, batch, task_id, public=False)
            values = interaction.data.get("values", [])
            if len(values) != 1 or not isinstance(values[0], str) or not values[0].isdecimal():
                raise RoundError("task_members")
            owner = values[0]
            members = await self.client.rounds_ui.visible_members(interaction.channel, interaction.guild)
            async with self.controls._lock(interaction):
                self.check_private(interaction, channel_id, user_id)
                _, key = self.check_batch(interaction, channel_id, batch, task_id, public=False)
                self.service.swap(channel_id, batch, task_id, owner, members, now_utc())
                self.pending_edits.add(key)
                await self.refresh_batch(interaction.channel, key)
                await interaction.edit_original_response(content="Proposed owner changed. Confirm the updated suggestion in the channel.", view=None, allowed_mentions=discord.AllowedMentions.none())
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def done(self, interaction: discord.Interaction, task_id: app_commands.Range[int, 1]):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.service.done(interaction.channel.id, task_id, now_utc())
                task = self.db.get_task(task_id)
                if task.proposal_batch and task.proposal_message_id:
                    key = (task.channel_id, task.proposal_batch, task.proposal_message_id)
                    self.pending_edits.add(key)
                    await self.refresh_batch(interaction.channel, key)
                await self.controls._reply(interaction, f"Task {task_id} marked done.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def open_task(self, interaction, channel_id, round_id):
        if interaction.user.bot:
            return
        try:
            if not self.controls.valid_context(interaction) or interaction.channel_id != channel_id:
                raise RoundError("task_private")
            record = self.client.rounds.get(round_id, channel_id)
            if record.outcome is None:
                raise RoundError("task_round")
            from bot.views import TaskModal
            await interaction.response.send_modal(TaskModal(channel_id, interaction.user.id, round_id))
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def submit_task(self, interaction, channel_id, user_id, round_id):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.check_private(interaction, channel_id, user_id)
                fields = [field for row in interaction.data.get("components", []) for field in row.get("components", [])]
                values = {field.get("custom_id"): field.get("value") for field in fields}
                if len(fields) != 2 or set(values) != {"rt:task_title", "rt:task_due"} or any(not isinstance(value, str) for value in values.values()):
                    raise RoundError("task_input")
                task = self.service.add(channel_id, user_id, values["rt:task_title"], values["rt:task_due"] or None, now_utc(), round_id=round_id)
                await self.controls._reply(interaction, f"Task {task.id} added for this decision, unowned. Use /rt-tasks assign for suggestions.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def deadline_add(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], date: str):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                self.timeline.add_deadline(interaction.channel.id, title, date, now_utc())
                await self.controls._reply(interaction, "Assignment deadline saved. Use /rt-timeline for a private proposal.")
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def build_timeline(self, interaction: discord.Interaction):
        if not await self.controls._begin(interaction):
            return
        try:
            preview = await self.timeline.build(interaction.channel.id, now_utc())
            async with self.controls._lock(interaction):
                self.timeline.require_active(interaction.channel.id)
                embed = formatting.timeline_embed(preview)
                token = self.timeline.save_preview(interaction.channel.id, interaction.user.id, preview, now_utc())
                from bot.views import TimelinePreviewView
                try:
                    await interaction.followup.send(embed=embed, ephemeral=True, view=TimelinePreviewView(interaction.channel.id, interaction.user.id, token), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    self.timeline.discard_preview(token)
                    raise
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def post_timeline(self, interaction, channel_id, user_id, token):
        if not await self.controls._begin(interaction, thinking=False):
            return
        try:
            async with self.controls._lock(interaction):
                self.check_private(interaction, channel_id, user_id)
                preview = self.timeline.get_preview(channel_id, user_id, token, now_utc())
                await interaction.channel.send("Roundtable (AI): Proposed backward timeline. These are suggestions for the team to review.", embed=formatting.timeline_embed(preview), allowed_mentions=discord.AllowedMentions.none())
                self.timeline.discard_preview(token)
                from bot.views import TimelinePreviewView
                await interaction.edit_original_response(content="Timeline proposal posted.", view=TimelinePreviewView(channel_id, user_id, token, disabled=True), allowed_mentions=discord.AllowedMentions.none())
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def stats(self, interaction: discord.Interaction):
        if not await self.controls._begin(interaction):
            return
        try:
            async with self.controls._lock(interaction):
                stats = self.service.stats(interaction.channel.id, now_utc())
                await interaction.followup.send(embed=formatting.pilot_stats(stats), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except RoundError as error:
            await self.fail(interaction, error)
        except Exception:
            await self.fail(interaction)

    async def refresh_batch(self, channel, key):
        channel_id, batch, message_id = key
        self.service.require_active(channel_id)
        tasks = self.db.task_batch(channel_id, batch)
        # Only names of members still able to view this channel are rendered.
        owners = [task.owner_user_id for task in tasks if task.owner_user_id and (member := channel.guild.get_member(int(task.owner_user_id))) is not None and channel.permissions_for(member).view_channel]
        names = self.client.rounds_ui.names(channel.guild, owners)
        from bot.views import TaskProposalView
        try:
            await channel.get_partial_message(int(message_id)).edit(
                content="Roundtable (AI): Task owner proposals; only confirmed owners are final.",
                embed=formatting.task_proposals(tasks, names), view=TaskProposalView(int(channel_id), batch, tasks, now_utc()), allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
        self.pending_edits.discard(key)

    async def reconcile(self, now):
        self.timeline.expire(now)
        batches = self.db.published_task_batches()
        for channel_id in dict.fromkeys(key[0] for key in batches):
            try:
                async with self.controls.lock_for(int(channel_id)):
                    self.service.expire(channel_id, now)
                    for key in batches:
                        if key[0] == channel_id and key not in self._expired_views and any(task.status == "proposed" and task.owner_user_id is None for task in self.db.task_batch(channel_id, key[1])):
                            self.pending_edits.add(key)
                            self._expired_views.add(key)
            except RoundError:
                continue
        for key in self.pending_edits.copy():
            try:
                channel = await self.client.rounds_ui.resolve_channel(self.db.get_channel(key[0]))
                async with self.controls.lock_for(channel.id):
                    self.service.require_active(channel.id)
                    await self.refresh_batch(channel, key)
            except Exception:
                continue

    def register_views(self):
        from bot.views import TaskButton, TaskSwapSelect, TaskPageButton, TaskFlowButton, TimelinePostButton
        self.client.add_dynamic_items(TaskButton, TaskSwapSelect, TaskPageButton, TaskFlowButton, TimelinePostButton)


def register_task_commands(tree, controller: TaskController):
    tasks = app_commands.Group(name="rt-tasks", description="Member-written tasks and confirmed owner suggestions.", guild_only=True)
    for name, description, callback in (
        ("add", "Add an unowned task; due date is optional YYYY-MM-DD (UTC).", controller.add),
        ("list", "Show the task board and compact workload counts privately.", controller.list_tasks),
        ("assign", "Suggest owners; the team must confirm them.", controller.assign),
        ("prefs", "Save your own private preference note for owner suggestions.", controller.prefs),
        ("done", "Mark a confirmed task done.", controller.done),
    ):
        tasks.add_command(app_commands.Command(name=name, description=description, callback=callback))
    tree.add_command(tasks)
    deadlines = app_commands.Group(name="rt-deadline", description="Manual assignment deadlines.", guild_only=True)
    deadlines.add_command(app_commands.Command(name="add", description="Add an assignment deadline: YYYY-MM-DD (UTC).", callback=controller.deadline_add))
    tree.add_command(deadlines)
    for name, description, callback in (
        ("rt-timeline", "Preview a proposed backward timeline privately.", controller.build_timeline),
        ("rt-stats", "Show only aggregate pilot counts and rates for this channel.", controller.stats),
    ):
        command = app_commands.Command(name=name, description=description, callback=callback)
        command.guild_only = True
        tree.add_command(command)
