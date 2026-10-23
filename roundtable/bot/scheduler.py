"""Round checks, one-time reminders, and task-proposal expiry every sixty seconds."""

import discord
from discord.ext import tasks

from bot.formatting import safe
from core.rounds import RoundError, now_utc


class RoundScheduler:
    def __init__(self, client, controller):
        self.client, self.controller = client, controller
        self.db, self.service = controller.db, controller.service

    @tasks.loop(seconds=60)
    async def run(self):
        await self.tick()

    @run.before_loop
    async def before_run(self):
        await self.client.wait_until_ready()

    async def tick(self, now=None):
        now = now or now_utc()
        await self.client.detection_ui.reconcile()
        await self.client.tasks_ui.reconcile(now)
        ids = (set(self.db.active_round_ids()) | self.controller.pending_results.copy()
               | self.controller.pending_outcome_views.copy() | self.controller.pending_share_views.copy())
        for round_id in sorted(ids):
            try:
                record = self.db.get_round(round_id)
                self.service.require_active(record.channel_id)
                if round_id in self.controller.pending_share_views:
                    await self.controller.refresh_share_view(round_id)
                if round_id in self.controller.pending_outcome_views:
                    await self.controller.reconcile_outcome_view(round_id)
                elif self.service.ready(record, now) or round_id in self.controller.pending_results:
                    await self.controller.maybe_close(round_id, now=now)
                else:
                    await self.remind(round_id, now)
            except (RoundError, discord.HTTPException):
                continue
            except Exception:
                # Keep later rounds running. Never log exception arguments or
                # a traceback that could contain members' private responses.
                continue

    async def remind(self, round_id, now):
        record = self.db.get_round(round_id)
        if not self.service.reminder_candidates(round_id, now):
            return
        channel = await self.controller.resolve_channel(record)
        async with self.controller.controls.lock_for(int(record.channel_id)):
            candidates = self.service.reminder_candidates(round_id, now)
            failed = False
            for user_id in candidates:
                self.service.require_active(record.channel_id)
                # Mark before attempting; forbidden/failed DMs are never retried,
                # including after a process restart.
                self.db.mark_reminded(round_id, (user_id,))
                member = channel.guild.get_member(int(user_id))
                if member is None or member.bot or not channel.permissions_for(member).view_channel:
                    continue
                try:
                    await member.send(
                        f'Gentle reminder: the team is waiting on your view for "{safe(record.question)}". '
                        'Press "Share my view" in the channel. One line is enough.',
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    failed = True
            if failed:
                self.service.require_active(record.channel_id)
                missing = [
                    member for item in self.db.round_members(round_id)
                    if not item.replied and (member := channel.guild.get_member(int(item.user_id))) is not None
                    and not member.bot and channel.permissions_for(member).view_channel
                ]
                if missing:
                    mentions = ", ".join(f"<@{member.id}>" for member in missing)
                    await channel.send(
                        f'Roundtable (AI): Still waiting on {mentions} for "{safe(record.question)}".',
                        allowed_mentions=discord.AllowedMentions(
                            users=[discord.Object(id=member.id) for member in missing],
                            everyone=False, roles=False, replied_user=False,
                        ),
                    )

    async def stop(self):
        task = self.run.get_task()
        self.run.cancel()
        if task:
            import asyncio
            try:
                await task
            except asyncio.CancelledError:
                pass
