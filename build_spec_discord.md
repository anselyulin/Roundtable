# Roundtable (Discord): MVP Build Spec

Hand this file to the coding agent as the full brief. Build exactly what is described. Where something is marked DECISION, use the stated default and do not ask.

## 1. What you are building

Roundtable is a Discord bot that joins a team's text channel and helps a student project team think together. It stays mostly quiet. When it detects that the team is making a decision, it offers to run a private-first decision round. Members can also privately ask it to explain a teammate's message. The team, not the AI, always makes the final call.

Audience: USC Marshall MSBA project teams of 3-5 people, many of them mixing native and second-language English speakers. Each team uses its own small Discord server or one private channel.

## 2. Hard rules (apply to every feature)

1. Never read or accept documents, files, assignment prompts, submissions, or grades. Ignore attachments and embeds entirely.
2. Never write any part of the team's deliverable. If asked, refuse politely and redirect to the team.
3. Do not store chat history. Keep only a small in-memory rolling window of recent messages per channel (last 15 messages, text only, max 10 minutes old) for detection. Never write message text to disk or logs.
4. Persist only: decisions, round responses, tasks, deadlines, settings, and aggregate metric counters (no content, no user ids).
5. Private features (clarify, view submission) reveal results only to the person who used them. Use ephemeral interaction responses. Never tell the channel who used clarify.
6. Challenge ideas, never people. Public wording is neutral and about process ("One risk nobody has raised yet is X", never "You are wrong").
7. Label every AI-generated public message with the prefix "Roundtable (AI):".
8. Any member can run `/rt-pause` to pause the bot in that channel. While paused it reads nothing and posts nothing except a one-line confirmation.
9. The bot operates only in channels where a member has run `/rt-setup`. In any other channel it ignores all messages.

## 3. Tech stack (DECISION)

- Python 3.11+
- `discord.py` 2.x using `discord.app_commands`, `discord.ui` (Views, Buttons, Selects, Modals), and a gateway connection (no public URL needed).
- SQLite via the standard library (`sqlite3`), file at `./roundtable.db`.
- LLM calls behind a small provider interface in `llm.py` with two implementations:
  - `OpenAIProvider` (default, model name from env `LLM_MODEL`, key from `OPENAI_API_KEY`)
  - `FakeProvider` (deterministic canned outputs, used when `LLM_PROVIDER=fake`, for tests and the demo fallback)
- All structured LLM outputs are requested as JSON and validated with `pydantic`. On invalid JSON, retry once, then fail quietly (the bot says nothing for detection, and says "I couldn't do that, please try again" for user-invoked features).
- Config from `.env`. Provide `.env.example`.
- Dependencies in `requirements.txt`: `discord.py`, `openai`, `pydantic`, `python-dotenv`, `pytest`, `pytest-asyncio`.
- Architecture rule: keep all logic in platform-independent `core/` modules that take plain Python data and return plain data. The Discord layer in `bot/` only translates Discord events into core calls and core results into Discord messages. This makes the logic testable without Discord.

## 4. Discord application setup (document in README)

The README must give step-by-step setup:

1. In the Discord Developer Portal, create an application and add a Bot.
2. Under Bot, enable these **Privileged Gateway Intents**: Message Content Intent and Server Members Intent. (For small bots in under 100 servers these are a toggle, not an approval.)
3. Copy the bot token into `.env`.
4. Generate an invite URL under OAuth2 with scopes `bot` and `applications.commands`, and these bot permissions: View Channels, Send Messages, Send Messages in Threads, Read Message History, Embed Links, Manage Messages (needed to pin the notice), Use Application Commands.
5. Invite the bot to a test server, run `python -m bot.main`, then run `/rt-setup` in the team channel.

Slash commands sync on startup. In development, set `DISCORD_GUILD_ID` so commands sync instantly to that server. Without it, sync globally (can take up to an hour to appear).

Gateway intents in code: `guilds`, `guild_messages`, `members`, `message_content`.

## 5. Project structure

```
roundtable/
  core/
    db.py                # SQLite schema and queries
    llm.py               # provider interface, OpenAI + Fake
    prompts.py           # all prompt templates in one place
    detection.py         # rolling window + detect-then-ask logic
    rounds.py            # decision round state machine
    clarify.py           # private clarify logic
    tasks.py             # task proposals, confirmation, workload
    timeline.py          # deadlines and backward timeline
    config.py            # env loading
  bot/
    main.py              # client setup, command sync, event wiring
    views.py             # persistent Views, Buttons, Selects, Modals
    commands.py          # slash commands and context menu
    scheduler.py         # background task loop (reminders, closing)
    formatting.py        # message text and embed builders
  tests/
  demo/script.md
  README.md
  requirements.txt
  .env.example
```

## 6. Data model (SQLite)

All Discord ids are stored as TEXT. All timestamps are UTC ISO strings.

```
channels(channel_id PK, guild_id TEXT, enabled INT DEFAULT 0, paused INT DEFAULT 0,
         detection_enabled INT DEFAULT 1, notice_message_id TEXT,
         last_prompt_at TEXT)

rounds(id PK, channel_id, question TEXT, options_json TEXT, status TEXT,
       -- status: collecting | closed | cancelled
       public_message_id TEXT, created_by TEXT, created_at TEXT, deadline_at TEXT,
       outcome TEXT, owner_user_id TEXT, closed_at TEXT, outcome_at TEXT)
-- outcome_at = when a member logged the outcome; time to decision = outcome_at - created_at

round_members(round_id, user_id, replied INT DEFAULT 0, reminded INT DEFAULT 0,
              PRIMARY KEY(round_id, user_id))

responses(round_id, user_id, option_label TEXT, reason TEXT, show_name INT DEFAULT 0,
          created_at TEXT, PRIMARY KEY(round_id, user_id))

tasks(id PK, channel_id, round_id NULL, title TEXT, owner_user_id TEXT NULL,
      due_at TEXT NULL, status TEXT, -- proposed | confirmed | done
      proposed_at TEXT, done_at TEXT NULL, created_by TEXT)

member_prefs(channel_id, user_id, preference_note TEXT, PRIMARY KEY(channel_id, user_id))

deadlines(id PK, channel_id, title TEXT, due_at TEXT, kind TEXT)
-- kind: assignment | milestone

metrics(channel_id TEXT, name TEXT, count INT DEFAULT 0, PRIMARY KEY(channel_id, name))
-- aggregate counters only, no content, no user ids. Counter names:
-- prompts_shown, prompts_accepted, prompts_dismissed,
-- clarify_uses, clarify_helpful, clarify_unhelpful,
-- tasks_done_on_time, tasks_done_late
```

Message text is never stored. `reason` in `responses` is the member's own private one-liner for the round and is the only free text kept, because the round needs it.

## 7. Features and exact behavior

General Discord rules for all interactions:
- Acknowledge every interaction within 3 seconds. Call `interaction.response.defer(ephemeral=True)` (or the appropriate public defer) first, then do LLM work, then `followup.send`.
- All buttons and selects that must survive a bot restart use persistent Views (`timeout=None`, stable `custom_id`s that encode ids, for example `rt:round:{round_id}:share`). Register them in `setup_hook`.
- Only handle interactions from members of the channel's guild. Ignore bot users.

### 7.1 Setup and transparency controls (MVP)

- `/rt-setup` (requires the Manage Channels permission or the server owner): sets `enabled=1` for that channel, posts the transparency notice, and pins it. The notice states: what the bot reads (recent messages in this channel, in a short window, only to detect decisions), what it never reads (files, documents, grades), what it stores (decisions, tasks, deadlines), that private clarify exists and is not visible to others, that reminders may mention members who have not yet replied to a round, and that any member can run `/rt-pause`.
- `/rt-pause` sets `paused=1` and posts `Roundtable (AI): Paused by a member. I'm not reading or posting until someone runs /rt-resume.` Do not name the member.
- `/rt-resume` clears it. `/rt-help` shows the command list ephemerally, starting with the pitch line: "Every team has someone who quietly turns five opinions into one plan. Roundtable does that job neutrally, so nobody has to, and everybody's view counts."

### 7.2 Detect-then-ask (CORE)

Goal: notice a likely decision in the channel and offer, never force, a decision round.

1. On each `on_message` event, ignore: bots, DMs, system messages, messages with attachments, edited-message events, channels where `enabled=0`, `paused=1`, or `detection_enabled=0`. Append text and author id to that channel's in-memory window.
2. Run detection when ALL are true: at least 4 new messages since the last check, at least 2 distinct authors in the window, at least 45 seconds since the last check, and at least 15 minutes since the last prompt posted in that channel.
3. Call the LLM with `DETECT_DECISION` (section 8). It returns JSON: `{"is_decision": bool, "confidence": 0-1, "question": str, "options": [str, ...]}`.
4. If `is_decision` is true, `confidence >= 0.75`, and there are 2-4 options, post ONE message with two buttons:
   - Text: `Roundtable (AI): It sounds like you're deciding "{question}" between {options}. Want me to run a quick private round so everyone's view is heard?`
   - Buttons: "Yes, run a round" (success style) and "No thanks" (secondary).
   - Increment the counter `prompts_shown` for the channel when the prompt is posted.
5. "Yes" starts a decision round (7.3) with the detected question and options. "No thanks" edits the message to "Okay, I'll stay quiet." and extends the cooldown to 60 minutes. Disable the buttons after the first click so they cannot be used twice. Increment `prompts_accepted` on "Yes" and `prompts_dismissed` on "No thanks".
6. `/rt-decide question:<text>` is the fallback trigger. It skips detection and calls `EXTRACT_OPTIONS` (section 8) on the question text only. If no options are given, the round lets members write their own option in their reason.

Env flag `DETECTION_MODE=auto|command_only` (default `auto`).

### 7.3 Decision moment (CORE)

State machine for a round: `collecting` -> `closed` (or `cancelled`).

DECISION: do not use DMs. Discord users can block DMs from server members, so views are submitted through ephemeral interactions instead.

1. **Start.** Create a `rounds` row. Add every non-bot member who can view the channel to `round_members` (uses the Server Members intent). Set `deadline_at` = now + `ROUND_DEADLINE_HOURS` (default 24; use 0.05 in demo mode).
2. **Public round message.** Post one message: `Roundtable (AI): Starting a private round on "{question}". Press "Share my view" and answer in one line, in any language you like. Nobody sees others' answers until everyone has replied or {deadline}.` It has one button: "Share my view". Store `public_message_id`. Update this message's footer as members reply: `{n} of {m} have shared a view` (count only, no names).
3. **Share flow (ephemeral).** Pressing the button sends the member an ephemeral message with:
   - A Select menu listing the options (plus "Another option" if no options were given).
   - A button "Add my reason" that opens a Modal with one text input, "Why? One line is fine" (max 300 characters, required).
   - A toggle button "Show my name: Off/On" (default Off).
   - Order: select option, then press "Add my reason" (modal), then the response is saved. A response requires both option and reason.
   - Show a confirmation: `Saved. You can change your answer until the round closes.`
4. **Edits.** Pressing "Share my view" again lets the member change their answer. Upsert into `responses`.
5. **Parsing free-text options.** If the member chose "Another option", call `PARSE_RESPONSE` on the reason text to produce an `option_label`.
6. **Close.** Close when all members replied, OR the deadline passes. If the deadline passes with replies missing, close with what exists and say so: `Proceeding with {n} of {m} views.`
7. **Result message** (one public message):
   - The spread: `3 lean toward A, 1 leans toward B.`
   - Each reason, shown without names by default and by name only for members who toggled "Show my name". If a reason is not in English, show the English translation from `PARSE_RESPONSE`, and keep the original in parentheses.
   - The steelman for the minority option from `STEELMAN_MINORITY` (section 8), framed as an idea. If tied or unanimous, steelman the least-chosen option, or print "No dissent raised." if all agree.
   - The key trade-off in one sentence (`trade_off` from `STEELMAN_MINORITY`) and the one question the team could answer next (`question`). This is neutral synthesis: it is built only from members' stated reasons, with no outside facts or statistics, and it never recommends an option.
   - Closing line: `The call is yours. Pick the outcome to log it.` with buttons for each option and "None of these".
8. **Outcome.** When a member presses an outcome button, record `outcome` and `outcome_at`, set `owner_user_id` to the presser, disable the buttons, and post a confirmation. Then offer "Want to turn this into tasks?" with a button that opens the task flow (7.6).
9. A closed round with no outcome logged appears in `/rt-log` as "open outcome".

### 7.4 Private clarify (MVP)

1. Triggered by a **message context-menu command** named "Ask Roundtable privately" (right-click a message, Apps menu). No slash command.
2. The interaction carries the single selected message. Use ONLY that message's text. Do not fetch surrounding messages. Ignore messages that are only attachments. Optionally open a Modal first with a one-line input "What's confusing? (optional)".
3. Defer ephemerally, call `CLARIFY_MESSAGE` (section 8), and reply with an **ephemeral** message visible only to the asker:
   - "What it likely means": plain, simple English, 1-3 sentences.
   - "Terms or idioms explained": only if present.
   - "Other possible readings": hedged wording ("could also mean"). Never speculate about the author's emotions beyond what the words support.
   - "If you want to check": one neutral follow-up question the asker could send.
4. Do not post anything public. Do not store the message text or the answer. Increment only the aggregate counter `clarify_uses` in `metrics`.
5. Add Helpful / Not helpful buttons to the ephemeral answer. Increment `clarify_helpful` or `clarify_unhelpful` only.

### 7.5 Reply tracking and nudges (MVP)

`bot/scheduler.py` runs a `discord.ext.tasks` loop every 60 seconds:
- For `collecting` rounds past 50% of the time to the deadline, with members who have not replied and `reminded=0`: try to DM each such member a gentle reminder (`Gentle reminder: the team is waiting on your view for "{question}". Press "Share my view" in the channel. One line is enough.`). If a DM fails (`discord.Forbidden`), do not retry. Instead post ONE channel message mentioning all members still missing: `Roundtable (AI): Still waiting on {mentions} for "{question}".` Set `reminded=1` for those members.
- For rounds past `deadline_at`: close them using 7.3 step 6.

### 7.6 Decision log (MVP)

- Every closed round is saved automatically.
- `/rt-log` shows an ephemeral list of the channel's last 10 decisions: question, outcome (or "open outcome"), who owns it, date. No reasons and no record of who voted what.

### 7.7 Task assignment (MVP)

Members break work into tasks themselves. The bot proposes owners; the team confirms.

1. Command group `/rt-tasks` with subcommands:
   - `add title:<text> due:<YYYY-MM-DD, optional>`: adds a task with `status=proposed` and no owner.
   - `list`: shows the task board (see 7.8).
   - `assign`: calls `PROPOSE_OWNERS` (section 8) using unowned tasks, current task counts per member, each member's optional preference note, and due dates. Posts one message listing proposed owners, with a "Confirm" button per task, a "Confirm all" button, and a "Swap" button that opens a user-select menu.
   - `prefs note:<text>`: stores the caller's own `preference_note`. Only the member can set it. It is used only for owner suggestions, never shown to others, and the response is ephemeral.
   - `done task_id:<id>`: marks a confirmed task done, sets `done_at`, and increments `tasks_done_on_time` (done on or before `due_at`, or no `due_at`) or `tasks_done_late` (done after `due_at`) for the channel. Any member can mark a task done.
2. Nothing is final until confirmed. Proposals expire after 24 hours and revert to unowned (handled in the scheduler).
3. Do not score or rank people on performance. Suggestions use only stated preferences and task counts.

### 7.8 Workload balance view (MVP)

`/rt-tasks list` includes a compact count per member, for example `Ana 3, Ben 2, Chen 2, Dee 1`, plus open vs. done. No other analytics.

### 7.9 Manual deadlines and timeline (MVP)

- `/rt-deadline add title:<text> date:<YYYY-MM-DD>` stores a deadline (`kind=assignment`).
- `/rt-timeline` builds a backward timeline from the nearest assignment deadline using `BUILD_TIMELINE` (section 8): 3-5 milestones with dates and a 1-2 day buffer, using existing tasks as input. Show it as an ephemeral message with a "Post to channel" button (the public version is labeled AI-generated).
- Timeline suggestions are proposals only. A task due after the assignment deadline is flagged: `Heads up: "{task}" is due after the assignment deadline.`

### 7.10 Pilot stats (MVP)

`/rt-stats` replies ephemerally with aggregate numbers for the current channel since setup. It exists so the team can measure the pilot goals in the PRD.

- Rounds started and closed.
- Average share of members who replied per round (from `round_members`).
- Median time from round start to `outcome_at`, over rounds with a logged outcome.
- Prompts shown, accepted, dismissed, and the accept rate.
- Clarify uses and the helpful rate (`clarify_helpful` / (`clarify_helpful` + `clarify_unhelpful`)).
- Tasks done on time, done late, and currently overdue (confirmed tasks past `due_at` and not done, computed live).

Rules: no names, no user ids, no message text, and no per-member breakdown. Who starts rounds or logs outcomes is deliberately not reported. "Who does the synthesis" and "original ideas per member" are measured by hand in the pilot, not by the bot.

## 8. Prompt templates (put verbatim in prompts.py)

All prompts return JSON only. Use a low temperature (0 to 0.3).

**DETECT_DECISION**
System: You watch a student team chat. Decide whether the team is currently weighing a decision between two or more concrete options (for example which market to pick, which model to use, which meeting time to choose). Casual banter, scheduling small talk, and settled decisions are NOT decisions. Be conservative. Return JSON: `{"is_decision": bool, "confidence": number 0-1, "question": string, "options": [string]}`. Use 2-4 short options taken from the messages. If not a decision, return `is_decision=false` and empty strings.
User: the numbered recent messages (text only, author initials only).

**EXTRACT_OPTIONS**
System: From the question, extract 2-4 options if they are stated. Return `{"question": string, "options": [string]}`. If none are stated, return an empty options list.

**PARSE_RESPONSE**
System: Match the member's reply to one of the given options. The reply may be in any language. Return `{"option_label": string or null, "reason": string in the member's words, translated to English if needed}`. If unclear, `option_label` is null.

**STEELMAN_MINORITY**
System: Write the strongest honest case for the given minority option in 2-3 sentences, based only on the reasons members gave. Frame it as an idea, never criticize any person, and do not invent facts or statistics. Also state the key trade-off between the options in one sentence, using only the members' stated reasons. End with one question the team could answer next to move the decision forward. Never recommend an option. Return `{"steelman": string, "trade_off": string, "question": string}`.

**CLARIFY_MESSAGE**
System: A student asked privately what a teammate's message means. Explain it in simple, plain English for a reader who may not be a native speaker. Do not guess emotions or intent beyond the words. Hedge alternatives with "could also mean". Do not rewrite or produce any academic work. Return `{"meaning": string, "terms": [{"term": string, "explanation": string}], "other_readings": [string], "follow_up_question": string}`.

**PROPOSE_OWNERS**
System: Suggest an owner for each unowned task so workload stays balanced. Use only the task counts, due dates, and members' own stated preference notes. Do not judge anyone's ability. Return `{"assignments": [{"task_id": int, "user_id": string, "reason": string (max 12 words)}]}`.

**BUILD_TIMELINE**
System: Given an assignment deadline, today's date, and the team's tasks, propose 3-5 milestones working backward with at least a 1-day buffer before the deadline. Return `{"milestones": [{"title": string, "date": "YYYY-MM-DD"}]}`. Dates must be on or before the deadline and not in the past.

## 9. Demo mode and fakes

The demo video must not depend on detection working perfectly:

- `LLM_PROVIDER=fake` makes `FakeProvider` return deterministic outputs: detection returns `is_decision=true` when the window contains the phrase "which market" (so a scripted chat triggers it), steelman and clarify return canned text, and timeline returns fixed milestones.
- `ROUND_DEADLINE_HOURS=0.05` (3 minutes) lets a full round, reminder, and close happen during a recording.
- Provide `demo/script.md` with the scripted chat: two members talking past each other about the target market, one member silent, the detection prompt appears, the round runs, the quiet member's view turns out best, and the log ends the scene.
- README section "Run the demo" with exact steps.

## 10. Tests (pytest, in-memory SQLite, FakeProvider)

Test the `core/` modules directly with plain data, and test the Discord layer with mocked interaction objects. Cover:

1. Detection fires only after 4 messages from 2+ authors, respects the 15-minute cooldown, and never fires when paused or in a channel that has not run `/rt-setup`.
2. A detection result with confidence below 0.75 posts nothing.
3. A round closes early when everyone replies, and closes at the deadline with a "Proceeding with n of m views" message.
4. No public message before close contains any response text, and the progress footer shows counts only.
5. A member can change their response until close, and a response requires both option and reason.
6. Clarify uses only the selected message text, replies ephemerally, and never sends a public message.
7. No message text is ever written to the database or logs (scan the DB file and log output after a test run).
8. Messages with attachments are ignored.
9. A failed DM reminder falls back to one channel mention message.
10. Task proposals expire after 24 hours and are not final until confirmed.
11. `/rt-pause` stops all reading and posting.
12. Persistent view `custom_id`s still work after the bot restarts (re-register views and handle a stored round).
13. The round result message includes the key trade-off and the next question, and both are built only from the stored member reasons.
14. `outcome_at` is set when an outcome is logged, and time to decision is computed from the round's start.
15. Counters increment correctly (prompts shown, accepted, dismissed; clarify uses and ratings; tasks done on time or late), and `/rt-tasks done` classifies late vs. on time from `due_at`.
16. `/rt-stats` output contains no user ids, names, or message text, and shows no per-member breakdown.

## 11. Environment variables (`.env.example`)

```
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...       # optional, dev only: sync commands instantly to one server
LLM_PROVIDER=openai        # openai | fake
OPENAI_API_KEY=...
LLM_MODEL=...              # set to a current model
DETECTION_MODE=auto        # auto | command_only
ROUND_DEADLINE_HOURS=24
DETECTION_CONFIDENCE=0.75
DETECTION_COOLDOWN_MIN=15
```

## 12. Acceptance criteria (MVP is done when all pass)

1. In a test server channel with 3+ people, after `/rt-setup`, a scripted decision conversation triggers one detection prompt, and pressing "Yes" starts a round.
2. Each member shares a view through "Share my view" using only ephemeral messages, and nobody sees others' answers before close.
3. On close, the channel gets the spread, anonymous reasons (names only if opted in), and a steelman of the minority view, followed by outcome buttons.
4. Selecting an outcome writes a row visible in `/rt-log`.
5. Using "Ask Roundtable privately" on any message gives only the asker an explanation, and nothing appears for anyone else.
6. Tasks can be added, owners proposed and confirmed, and `/rt-tasks list` shows per-member counts.
7. `/rt-deadline` and `/rt-timeline` produce a backward timeline.
8. `/rt-pause` makes the bot silent, and `/rt-resume` restores it.
9. Buttons still work after restarting the bot.
10. `pytest` passes, including the no-message-text-stored test.
11. A README lets a new person set up the app and run the demo in under 20 minutes.
12. Round results show the spread, the minority case, the key trade-off, and a next question, with no recommendation of an option.
13. `/rt-stats` shows the aggregate pilot numbers listed in 7.10 and nothing identifying.

## 13. Out of scope (do not build)

Brightspace or any calendar integration, tone check or draft assistance, automatic misunderstanding catcher, catch-up digest, contribution view, faculty setup, translation features, file or document reading, voice channels, any web UI, user accounts, analytics dashboards (the aggregate `/rt-stats` command is the only reporting allowed).

## 14. Build order

1. Skeleton, config, DB, Fake provider, `/rt-setup`, pause/resume, transparency notice.
2. Decision rounds with `/rt-decide` (command path first): share flow, reminders, close, log.
3. Private clarify (context-menu command).
4. Detection and the Yes/No prompt.
5. Tasks (including `done`), workload view, deadlines, timeline, metric counters, and `/rt-stats`.
6. Tests, demo script, README.

At each step, run the tests and confirm the Hard rules in section 2 still hold.
