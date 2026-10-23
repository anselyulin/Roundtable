# Roundtable demo: hear the quiet member before deciding

This is invented demo dialogue, not collected team chat. The main scene takes
about four minutes after setup. Use three consenting human participants: **Ana**,
**Ben**, and **Chen**. These are role labels; nobody needs to rename an account.
Restrict the test channel to these three people and the bot so the round has
three eligible members. Chen stays quiet in the public conversation initially.

## Before recording

Follow the README's quick setup with a test-server `DISCORD_GUILD_ID`. Stop the
old bot process before starting another one. Keep the existing ignored `.env`
and its Discord token/provider settings. With the virtual environment activated,
override only this process from `roundtable/`:

```sh
DETECTION_MODE=auto ROUND_DEADLINE_HOURS=0.05 \
DETECTION_CONFIDENCE=0.75 DETECTION_COOLDOWN_MIN=15 python -m bot.main
```

The bot starts with a three-minute deadline and uses the provider configured in
`.env`. With `LLM_PROVIDER=vertex`, the demo uses Vertex for automatic detection
and the other model-backed features. The command deliberately leaves
`LLM_PROVIDER` unchanged. For an offline demo, set it to `fake` explicitly; that
provider recognizes explicit choices written as “A or B”, “A vs B”, or “A | B”
in a message that asks the team to choose, compare, or decide, and returns canned
responses for other model-backed features.

Use a **fresh test channel** for each recording. Run `/rt-setup` there, verify the
notice is pinned, and run `/rt-help` to show the pitch privately. If paused, run
`/rt-resume` and wait for its private confirmation before sending the dialogue.
Never reset the database or delete decisions to prepare another take.

## Main scene

### 1. Two people talk past each other (about 20 seconds)

Send these four lines as **four separate new messages**, alternating authors.
Chen sends nothing yet. The initial disagreement is about reach versus access.
The marked tables are also consumed by the offline acceptance rehearsal.

<!-- demo-chat:start -->
| Role | Chat text |
| --- | --- |
| Ana | Which market should we choose: Campus or Local businesses? |
| Ben | Local businesses have a larger market. |
| Ana | Campus contacts can help us find interviewees. |
| Ben | I mean long-term reach; let's compare Local businesses and Campus. |
<!-- demo-chat:end -->

Expected: **one** AI-labeled offer naming Campus and Local businesses, with
**Yes, run a round** / **No thanks**. No round starts automatically. Ana clicks
**Yes, run a round**. The new round shows **0 of 3 have shared a view** and its
three-minute deadline. White dots turn green as views are submitted; the dots
represent counts only, without identifying members or their choices. The clock
starts at acceptance, not at the first chat.

If detection does not appear, use the fallback in the troubleshooting section
below; do not spend the recording waiting on a real model.

### 2. Collect private views; show counts only (round +10 to +30 seconds)

Ana and Ben each click **Share my view**, choose Campus, leave **Show my name:
Off**, click **Add my reason**, and submit the corresponding reason below.
Ben's private priority is timing, even though his first public point was reach.
Keep Chen's final row for step 3.

<!-- demo-views:start -->
| Role | Option | Private reason |
| --- | --- | --- |
| Ana | Campus | Campus contacts make it easier to reach interviewees. |
| Ben | Campus | Campus interviews can fit our short schedule. |
| Chen | Local businesses | I already have two local shop owners willing to talk this week. |
<!-- demo-views:end -->

Expected: each form/confirmation is visible only to its user. Public progress
changes from 1 of 3 to 2 of 3. No reason, vote, or missing member's name is shown
by the progress footer. Reopening Share my view shows only that user's answer.
For a privacy shot, have Chen verify the public channel contains no reasons.

### 3. Invite the quiet view (round +90 to +150 seconds)

Wait for the halfway reminder. It becomes eligible at +90 seconds and is sent
on the next 60-second scheduler tick (normally between +90 and +150 seconds).
The reminder is a DM, never a submission form. If Chen blocks the bot's DM,
one AI-labeled channel fallback mentions Chen only. For a short edit, skip the
wait and have Chen submit before the deadline; use the deadline branch below
when checking reminder timing explicitly.

Chen now clicks the **channel's Share my view** button, chooses Local businesses,
leaves the name toggle Off, and submits the final private reason from the table.
Do this before +180 seconds. The round closes immediately when the third view
arrives; nobody needs to click End round early for this scene.

### 4. The team changes the decision (about 30 seconds)

Expected: the starter becomes one AI-labeled result with:

- **2 lean toward Campus, 1 leans toward Local businesses.**
- Three anonymous reasons, including Chen's concrete access to shop owners.
- A minority case quoting that stated reason, a key trade-off, a next question,
  and **The call is yours. Pick the outcome to log it.**
- Campus, Local businesses, and None of these outcome buttons.

The bot does **not** call Chen's option best or recommend it. A human makes the
turn: Ana says, “Those confirmed local contacts make Local businesses workable.
Let's choose that.” Ana then clicks **Local businesses**, despite its minority
vote count. The result buttons disable, and a public outcome confirmation offers
**Turn into tasks**. This demonstrates that the team makes the final call.

Finish the main scene with `/rt-log`. It privately shows the question, saved
Local businesses outcome, owner, and date, with no reasons or voting record.

## Optional acceptance checks after the scene

These are separate checks, not required for the four-minute recording.

1. **Clarify privately:** Ben selects the fourth chat message, then right-clicks
   → Apps → **Ask Roundtable privately**. Show only Ben's screen; Ana and Chen
   should see no announcement. Rate Helpful once. The canned answer describes
   comparing choices. A second rating must not add another count.
2. **Tasks/workload:** click Turn into tasks and enter `Arrange team review`,
   leaving due blank. Add `/rt-tasks add title:Compare interview availability`.
   Optionally set `/rt-tasks prefs note:I prefer organizing team meetings`.
   Run `/rt-tasks assign`; verify owners are only proposed. Swap one using the
   private user selector, then confirm the updated proposals. `/rt-tasks list`
   should privately show two open tasks and compact counts per member. Mark one
   with `/rt-tasks done task_id:<the displayed id>` and check open/done totals.
   Without a due date it counts on time. Use an invented task due yesterday to
   separately verify late completion; never change a real task for the demo.
3. **Deadline/timeline:** choose UTC dates seven and ten days ahead. Run
   `/rt-deadline add title:Team checkpoint date:<seven days ahead>` and add a task
   due ten days ahead. `/rt-timeline` should privately show three milestones
   (FakeProvider), a one-day buffer, and the task's due-date warning. Only the
   requester presses **Post to channel**. Verify an AI-labeled proposal appears
   once. It creates no confirmed milestones and changes no task dates.
4. **Stats:** `/rt-stats` is private. After exactly this scene and one Helpful
   rating, expect one round started/closed, 100% reply share, one shown/accepted
   prompt and zero dismissed, 100% accept rate, one clarify use and 100% helpful
   rate. Completion counters reflect optional done actions. The median duration
   varies. Confirm there are **no names, user IDs, questions, reasons, task
   titles, preference notes, or per-member rows** in this output.
5. **Pause/resume:** any participant runs `/rt-pause`; show the anonymous pause
   notice. Send fresh chat and try a command: there should be no detection,
   reminder, result, or task/timeline post; user actions return only the private
   paused line. `/rt-resume` confirms privately. Send four new messages after
   resuming in a fresh enabled channel to check detection without cooldown.
6. **Restart controls:** use `/rt-decide` to start a second round. Submit Ana's
   view, stop the bot with Ctrl-C, restart from the same directory, and have Ben
   use the **old** Share my view button and submit. Existing answers remain.
   Also leave an owner proposal unconfirmed across a restart and confirm it
   afterward. Private timeline previews and clarify ratings deliberately expire
   on restart; regenerate them rather than treating their expiry as a defect.

## Deadline and missing-view branch

Start a separate `/rt-decide question:Which market: Campus or Local businesses?`
round with the same three people, then submit only Ana's view. Wait past +180
seconds. At the next scheduler tick (less than 60 seconds later under normal
operation) the result should say **Proceeding with 1 of 3 views.** Late forms
must be rejected even before that tick. Confirm the halfway reminder is sent
once to each missing member, or falls back once if a DM fails. Alternatively,
the starter can press **End round early**; expect the same missing-view count
plus **Ended early.** These branches do not need an LLM or a 24-hour wait.

## Troubleshooting and a reliable fallback

- **Worked once, then stopped:** offers have a 15-minute cooldown. No thanks
  extends it to at least 60 minutes. An unanswered old offer also blocks another.
  Pause/resume and a restart do not reset these states. Use a fresh enabled
  channel for a new take, or wait and send new messages. Do not delete history.
- **Paused:** run `/rt-resume`; its confirmation is private. Chat sent while
  paused was ignored and must be sent again after resume.
- **Wrong mode/process:** verify `DETECTION_MODE=auto` and restart the bot after
  changing settings. Shell/IDE environment overrides `.env`. Run one bot process.
- **Repeated low-confidence checks:** after a check, both four new messages and
  45 seconds are required. Messages with attachments/embeds, bots, edits, or a
  single author cannot trigger an offer.
- **Recording fallback:** run
  `/rt-decide question:Which market: Campus or Local businesses?` in an enabled,
  active channel. Continue from the private-views scene. This bypasses detection
  and its cooldown, but does not bypass pause. Label the take “command fallback”;
  it does not verify automatic-detection acceptance criterion 1.
- **Date helpers:** run the following locally for valid UTC demo dates; substitute
  the printed dates into commands, not the angle-bracket placeholders:

  ```sh
  python - <<'PY'
  from datetime import datetime, timedelta, timezone
  today = datetime.now(timezone.utc).date()
  for label, offset in (("yesterday", -1), ("deadline", 7), ("later task", 10)):
      print(label, (today + timedelta(days=offset)).isoformat())
  PY
  ```

After recording, stop the bot with Ctrl-C. A normal `python -m bot.main` uses
your previous `.env` settings again (usually `ROUND_DEADLINE_HOURS=24`). If you
chose to edit `.env` instead, restore those settings before restarting. Keep keys local.
Do not film `.env`, token settings, real team messages, or private notes.
