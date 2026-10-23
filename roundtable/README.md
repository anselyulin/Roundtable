# Roundtable

Python 3.11+. This increment includes the project skeleton, environment config,
complete SQLite schema, stateless Fake/OpenAI/Vertex provider boundary, `/rt-setup`,
`/rt-pause`, `/rt-resume`, `/rt-help`, and the pinned transparency notice, plus
command-triggered decision rounds, `/re-open`, the private `/rt-log`, private
selected-message clarify with feedback counters, an early-ending button, and
automatic decision detection with a Yes/No offer, confirmed task assignment,
workload counts, manual deadlines, proposed timelines, and aggregate pilot stats.

With `DETECTION_MODE=auto`, enabled, active channels use a transient recent-chat
window to offer decision rounds. `DETECTION_MODE=command_only` leaves ordinary
chat unread. `/rt-decide` uses only the question supplied in the command.
Submissions use private ephemeral forms and a reason modal;
reasons are revealed only after closure. Clarify reads only an explicitly selected
message and replies privately. Nothing from spec section 13 is built.

## Quick setup and demo (target: under 20 minutes)

Start with Python 3.11+, permission to manage a Discord test server, and three
human participants available. Use FakeProvider for this path; no LLM key, Google
Cloud setup, or paid model call is needed. The timing is a planning estimate,
not a measured first-time user result; platform approvals/global sync can take
longer. **Set a test-server `DISCORD_GUILD_ID` to use guild command sync.**

| Budget | Action |
| --- | --- |
| 0–3 min | Follow [Install and configure](#install-and-configure). Copy `.env.example` only if `.env` does not exist. |
| 3–8 min | Follow [Discord application setup](#discord-application-setup): create the bot, enable both intents, invite it with the listed permissions. |
| 8–10 min | Set `DISCORD_TOKEN` and numeric `DISCORD_GUILD_ID` locally. Keep keys out of screenshots and chat. |
| 10–12 min | Run the demo command below from `roundtable/`, then `/rt-setup` in a fresh channel visible to exactly the three participants and the bot. Verify the pinned notice. |
| 12–17 min | Follow [Run the demo](#run-the-demo) and the exact dialogue in [demo/script.md](demo/script.md). End with `/rt-log`. |

With the virtual environment activated, this command overrides only the new
process, preserving existing `.env` credentials/provider settings:

```sh
LLM_PROVIDER=fake DETECTION_MODE=auto ROUND_DEADLINE_HOURS=0.05 \
DETECTION_CONFIDENCE=0.75 DETECTION_COOLDOWN_MIN=15 python -m bot.main
```

Stop any old bot process first; run one instance. For the offline acceptance
rehearsal, which needs no Discord token at all, run `python -m pytest -q` instead.
See [Acceptance audit](#acceptance-audit) for exactly what has been verified.

## Install and configure

From the directory containing `build_spec_discord.md`:

```sh
cd roundtable
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
```

Edit `.env`: set `DISCORD_TOKEN` and use `LLM_PROVIDER=fake` for a credential-free
LLM fallback (a Discord bot token is still required). Optionally set
`DISCORD_GUILD_ID` to your test server's numeric id. Enable Developer Mode in
Discord's Advanced settings, then right-click the server and select Copy Server
ID. Leave that variable empty for global command sync.

`LLM_PROVIDER` defaults to `openai`. For that setting, supply `OPENAI_API_KEY` and
a JSON-capable model name in `LLM_MODEL`. The providers are stateless; FakeProvider
requires no network access. Providers handle decision detection, option extraction, round-reason
translation, neutral synthesis, private clarification, owner suggestions, and
timeline proposals.
All other defaults are shown in `.env.example`, including the demo's
`ROUND_DEADLINE_HOURS` override of `0.05`. Existing process environment variables
take precedence over `.env`. Secrets are excluded from settings representations
and `.env` is gitignored.

### Use Gemini through Google Cloud Vertex AI / Agent Platform

Install/update dependencies in the activated environment:

```sh
python -m pip install -r requirements.txt
```

Use the Google Cloud project attached to your Cloud billing/trial account. Enable
the **Agent Platform API** (`aiplatform.googleapis.com`, previously Vertex AI).
Your dedicated `roundtable-gemini` service account must have permission to invoke
Gemini models in this project (the usual predefined role is Vertex AI User,
`roles/aiplatform.user`). Use a Google Cloud **authorization API key** bound to
that service account and restricted to **Agent Platform API**. The binding and
restriction are Cloud-side settings; the application cannot establish them from
the opaque key string. See Google's [authorization-key documentation](https://docs.cloud.google.com/docs/authentication/api-keys).

Edit your existing `.env`, keeping your Discord token:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
LLM_PROVIDER=vertex
GOOGLE_CLOUD_PROJECT=your_project_id
GOOGLE_CLOUD_LOCATION=global
VERTEX_API_KEY=your_service_account_bound_authorization_key
LLM_MODEL=
```

Set `LLM_MODEL` to a **stable text Flash model id verified as available to this
project through Vertex/Agent Platform**, with JSON output support. Leave out URL,
publisher, project, and location prefixes; the provider constructs the project
resource path. Do not use preview, Live, audio, image, or TTS models for this bot.
Google's [model lifecycle table](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions)
identifies stable versions, but catalog presence alone does not prove that your
project can invoke a model. The offline acceptance suite does not check your
project's live model availability or billing. Keep a model you have verified in
your project; this guide does not guess one or claim a live connectivity result.

The official [google-genai SDK](https://googleapis.github.io/python-genai/) runs
with `vertexai=True`, API version `v1`, and the Cloud endpoint
`https://aiplatform.googleapis.com/`. Calls target
`projects/{project}/locations/global/publishers/google/models/{model}` and
authenticate using the key header. This scopes inference to the configured Cloud
project. Cloud trial-credit application depends on the project's billing
account, remaining credit, and eligibility; Roundtable does not inspect billing
or promise a balance.

Remove obsolete `GEMINI_API_KEY` settings. `LLM_PROVIDER=gemini` is no longer
accepted; there is no Developer API fallback. Neither `OPENAI_API_KEY`,
`GOOGLE_API_KEY`, ADC login, nor a downloaded service-account JSON file is needed
in Vertex mode. Keep the authorization key only in your ignored local `.env`.
Process environment variables take precedence over `.env`; update any shell/IDE
overrides, then restart the bot with `python -m bot.main`.

The provider sends exactly the existing text and safety/operation instructions.
It requests JSON and uses the unchanged shared Pydantic models; malformed JSON
or schema is retried once. Transport errors retain the fixed private error.
There is no automatic provider fallback, tool use, chat history, SDK recording,
or response cache. Step 3's private clarify uses the existing `CLARIFY_MESSAGE`
contract and the same JSON retry/safe-error path. Fake/OpenAI configuration
is unchanged.

## Discord application setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, and add a Bot.
2. Under Bot, enable **Message Content Intent** and **Server Members Intent**.
   For a small bot below Discord's verification threshold these are toggles;
   larger verified bots may need approval. Code enables `guilds`, `guild_messages`,
   `members`, and `message_content` only.
3. Copy/reset the bot token and place it in `.env` as `DISCORD_TOKEN`.
4. Under OAuth2 / URL Generator, select scopes **bot** and
   **applications.commands**. Grant **View Channels**, **Send Messages**,
   **Send Messages in Threads**, **Read Message History**, **Embed Links**,
   **Manage Messages**, **Pin Messages**, and **Use Application Commands**.
   Current Discord has a separate **Pin Messages** permission; the spec's
   Manage Messages permission alone is insufficient for pinning. See Discord's
   [message API](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx#pin-message).
   Check channel-level permission overrides too.
5. Open the generated URL and invite the bot to a test server. In the activated
   environment, from `roundtable/`, run:

   ```sh
   python -m bot.main
   ```

6. In the team's text channel run `/rt-setup` as a member with Manage Channels
   permission or as the server owner. The bot posts and pins its transparency
   notice. A new channel becomes enabled only after pinning succeeds.

Slash commands sync in `setup_hook` on startup, using discord.py's
[guild development sync](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.app_commands.CommandTree.copy_global_to)
when `DISCORD_GUILD_ID` is set. Without it they sync globally and can take up to
an hour to appear. Restart after changing the sync target.

## Commands available now

| Command | Result |
| --- | --- |
| `/rt-setup` | Enable this channel; post/update and pin its notice. Requires Manage Channels or server ownership. |
| `/rt-pause` | Any channel member can pause; posts the exact anonymous one-line confirmation. |
| `/rt-resume` | Any channel member can resume; confirms privately. |
| `/rt-help` | Private command list, beginning with the required pitch line. |
| `/rt-decide question:<text>` | Start a private-first decision round for all visible non-bot channel members. |
| `/rt-log` | Private list of the channel's last 10 closed decisions: question, outcome or “open outcome”, owner, date. |
| `/re-open` | Browse the channel's previous decisions privately; reopen one with the same options or add a new option. |
| **Ask Roundtable privately** (message context menu) | Explain only the selected message, privately; optionally rate the answer Helpful / Not helpful. |
| `/rt-tasks add title:<text> due:<YYYY-MM-DD, optional>` | Add a member-written task, unowned and unconfirmed. |
| `/rt-tasks list` | Private paginated task board, open/done totals, and confirmed open task counts per current member. |
| `/rt-tasks assign` | Public owner suggestions with Confirm, Confirm all, and Swap controls. |
| `/rt-tasks prefs note:<text>` | Save only your own private preference note for owner suggestions. |
| `/rt-tasks done task_id:<id>` | Any current channel member can mark a confirmed task done, once. |
| `/rt-deadline add title:<text> date:<YYYY-MM-DD>` | Save a manual assignment deadline. |
| `/rt-timeline` | Private backward timeline proposal with an optional Post to channel button. |
| `/rt-stats` | Private channel totals and rates only, with no identities, message text, or member breakdown. |

While paused, setup and help give only the one-line paused confirmation. Resume
restores access to full help. Help is available before setup, but never enables a
channel. Pause/resume before setup only ask the caller to run setup first.

Repeated setup updates/re-pins the existing notice. If it was deleted, setup
replaces it. If pinning fails, fix permissions and retry; the saved notice is
reused. Setup never silently resumes a paused channel.

## Decision rounds

1. Run `/rt-decide question:Which market: Campus or Local businesses?`.
   Questions are limited to 300 characters. The public message contains only the
   question, deadline, Share my view / End round early buttons, and counts of submitted views.
2. Press **Share my view**. The form is ephemeral. For questions with stated
   options, choose one, optionally toggle **Show my name** (off by default), then
   press **Add my reason** and enter a required reason of at most 300 characters.
   For an open-ended question such as `/rt-decide question:How should we organize
   our meetings?`, press **Add my answer** instead. Enter **Your answer** (up to
   80 characters) and **Why? One line is fine** (up to 300 characters). Both are
   required. Every member enters their own answer privately; no model inference
   is needed to save it. The same optional name toggle applies.
3. Reopen the same button to edit your own response before close. The team sees
   only counts, and you can only view your own saved submission. For open-ended
   rounds, both your saved answer and reason are prefilled for editing. Distinct
   submitted answers become outcome choices at close; identical answers share
   a count. If you have an old reason-only modal open from before this update,
   reopen **Share my view** to get the new form.
4. When everyone has replied, the round closes immediately. Otherwise the
   60-second scheduler closes it at the first tick at/after its UTC deadline.
   Submissions are rejected at the exact deadline even before the next tick.
   The round starter, server owner, or a member with **Manage Channels** may also
   press **End round early**. This immediately freezes submissions and publishes
   the normal result using only received views, with **Ended early. Proceeding
   with n of m views.** when replies are missing. A round with no replies can be
   ended too; it invents no reasons or dissent. Outcome logging remains a
   separate member action. The original deadline and closure timestamp are kept.
5. At halfway, missing members receive one DM reminder. If a DM fails, one
   channel message mentions all members still missing. DMs are reminders only;
   submissions never use DMs. Only missing members may be mentioned by this post.
6. Closure replaces the starter with one result: spread, anonymous reasons
   (names only by explicit consent), minority steelman, key trade-off, and next
   question. Non-English reasons include temporary English rendering plus the
   stored original. Synthesis sees only members' reasons and option labels, with
   no identities, surrounding chat, outside facts, or option recommendation.
7. Any channel member can click an outcome, including **None of these**. The
   first click records `outcome`, `owner_user_id`, and `outcome_at` and disables
   all outcome buttons. `/rt-log` never shows reasons or voting records. The
   **Turn into tasks** button opens a form for a member-written task linked to
   that decision; it does not generate tasks or assign an owner automatically.

Pause suppresses round forms, submissions, synthesis publication, and reminders.
Deadlines remain absolute UTC times. After resume, an expired round closes on the
next scheduler tick. Model work does not hold the channel's pause lock; an
in-flight answer is discarded if pause happens before it can be used.

Public share/outcome views re-register at startup. Private control ids include
only round/member ids, option index, and consent; no reasons appear in ids.
Modals opened before a restart can still be submitted through the restart-safe
modal route. Discord's ephemeral messages can expire; reopen Share my view if
needed. Saved responses survive restarts.

Rounds freeze before synthesis/publication, even if a provider or Discord fails.
The scheduler retries pending results and re-edits the same bot message. After a
restart, closed rounds awaiting outcomes are reconciled once from stored reasons.
Saved outcomes also reconcile disabled buttons without relogging or reposting a
confirmation, covering a failed edit or crash between the database/API updates.
Deleted starters are recreated at close. An unpublished collecting round left by
a startup crash is cancelled rather than sending unusable reminders.
After startup, the scheduler updates older collecting messages with the early-end
button and its disclosure on its first active tick. Paused channels wait until
resume. Failed early-close publication follows the same retry/restart recovery.

## Automatic detection and the Yes/No offer

Set `DETECTION_MODE=auto` in `.env` and restart. In an enabled, unpaused channel,
Roundtable keeps at most 15 recent text messages, each for less than 10 minutes,
in memory. It ignores bots, DMs, system messages, edits, and messages containing
attachments or embeds. It never fetches chat history or reads files. Author IDs
stay in memory; the model sees anonymous author initials and numbered text.

A check needs at least four new messages and two authors in the window. The
first eligible check can run immediately; later checks are at least 45 seconds
apart. A public offer requires confidence at least `DETECTION_CONFIDENCE=0.75`,
a question, and two to four options actually mentioned in the window. There is
at least `DETECTION_COOLDOWN_MIN=15` minutes between offers and only one
unanswered offer per channel. Invalid output is retried once; detection failures
stay silent.

The AI-labeled offer has **Yes, run a round** and **No thanks** buttons. Yes
starts the existing private share/reason flow using the proposed choices and
current visible members; no round starts before consent. No changes the offer to
**Roundtable (AI): Okay, I'll stay quiet.** and extends the quiet period to at
least 60 minutes from dismissal. Anyone currently authorized in that channel
can answer; the first valid click wins. Pause blocks both actions and immediately
clears the chat window.

Only the proposed decision question/options, bot message ID, state, and cooldown
are persisted to restore controls after a restart. Source chat, source message
IDs, and author IDs are never saved. Aggregate metrics count confirmed posted
offers (`prompts_shown`) and first accepted Yes/No actions (`prompts_accepted` /
`prompts_dismissed`). If Yes succeeds but posting the round fails, acceptance
still counts, the round is cancelled, and `/rt-decide` remains available.

`DETECTION_MODE=command_only` skips ordinary gateway messages before inspecting
their fields. Manual commands, explicit private clarification, and previously
posted Yes/No controls remain available. No new configuration variables or
dependencies are needed. Startup upgrades the database automatically; restart
`python -m bot.main` and rerun `/rt-setup` to refresh the pinned notice.

## Private clarify

1. Right-click a teammate's message in an enabled channel, then choose
   **Apps → Ask Roundtable privately**. There is no clarify slash command.
2. The bot acknowledges privately before model work and uses only that message's
   text (up to 4,000 characters). It never fetches surrounding messages, follows
   replies/links, or reads the message's attachments/embeds. An attachment-only
   or embed-only message has no accepted text. A mixed message contributes only
   its text. Assignment prompts, submissions, grades, and deliverable-writing
   requests are refused using the existing input boundaries.
3. Only the asker sees **What it likely means**, optional **Terms or idioms
   explained** and **Other possible readings**, and **If you want to check**.
   The unchanged clarify prompt requires plain language, hedged interpretations,
   and no unsupported speculation about a teammate's emotions or intent.
4. Optionally press **Helpful** or **Not helpful**. Only the asker can rate their
   answer, once, in the original channel. Feedback expires after 15 minutes or
   a bot restart. An expired/repeated click responds privately without counting.
   An accepted rating disables both buttons, including if opposing clicks race.

No public message announces clarification or feedback. Selected text and answers
are never saved or logged. SQLite stores only `clarify_uses` for successfully
delivered answers and `clarify_helpful` / `clarify_unhelpful` for accepted ratings,
keyed by channel. No usage identifiers, source message ids, or per-user records
are saved. Content-free pending feedback receipts live in bounded memory only.
Pause/setup and channel access checks run before reading selected text, and pause
is checked again before publishing an answer. In-flight output is discarded if
the channel is paused. Clarify does not use the automatic detection window.

Restart `python -m bot.main` to sync the context menu. Then run `/rt-setup` again
as the server owner or a Manage Channels member to refresh the pinned notice.

## Tasks and workload

1. Add tasks with `/rt-tasks add title:Compare market choices`, optionally adding
   `due:YYYY-MM-DD`. Titles are your own words, up to 200 characters; the bot
   never breaks a deliverable into written content. You can also use **Turn into
   tasks** after logging a decision. Its form links each added task to that round.
2. Optionally set `/rt-tasks prefs note:I prefer organizing team meetings`.
   Only you can set your own note (up to 300 characters). It is stored privately,
   used only for owner suggestions, and never displayed on the board or in
   public explanations. Setting it confirms privately without echoing the note.
3. Run `/rt-tasks assign`. The model receives unowned tasks, due dates, current
   task counts, and current visible members' own preference notes. It receives
   no round reasons, chat history, names, grades, or performance scores. The fake
   provider deterministically balances counts. Each invocation proposes at most
   10 unowned tasks so controls fit in Discord; invoke again for remaining tasks.
   Pending suggestions reserve workload counts for subsequent suggestions.
4. The AI-labeled public proposal has **Confirm #id**, **Swap #id**, and
   **Confirm all**. Any current channel member can confirm. Swap opens a private
   member selector, updates the suggestion publicly, and still needs confirmation.
   Hidden members and bots cannot be chosen. Confirm all applies only to that
   displayed batch; stale controls cannot silently confirm changed owners.
5. Unconfirmed suggestions expire exactly 24 hours after proposal, including
   after a restart. They revert to unowned, keeping the task. Swapping does not
   extend expiry. The scheduler refreshes expired controls at its next active
   60-second tick; clicks also enforce expiry immediately. Confirmed owners do
   not expire. Pause blocks task changes and defers scheduler edits.
6. `/rt-tasks list` shows eight tasks per page, each with status, owner (explicitly
   marked as suggested until confirmed), and due date. It also shows open vs.
   done totals and a compact count of confirmed open tasks per current member,
   in member order. Done tasks and pending suggestions are excluded from these
   per-member counts. There are no rankings or performance analytics.
7. Any current member can run `/rt-tasks done task_id:1`. Only a confirmed,
   unfinished task can be completed. The first completion saves `done_at` and
   increments exactly one aggregate counter, even if clicks race or the bot
   restarts. Completion at or before the due time, or without a due date, counts
   as on time; completion after it counts as late. The original owner remains.

All date-only inputs use **23:59:59.999999 UTC on the specified day**. Past task
due dates are allowed so overdue work can be tracked. No timezone or calendar
integration is added. Proposal controls, private paging/selects, and linked-task
modals restore after restart. Failed proposal sends revert to unowned; failed
public edits retry without repeating state changes. Older outcome confirmations
with the previously disabled step-5 button remain unchanged; use `/rt-tasks add`
for those decisions. No history is fetched to find those old messages.

## Deadlines and timeline proposals

Run `/rt-deadline add title:Team checkpoint date:YYYY-MM-DD` using today or a
future UTC day. `/rt-timeline` uses the nearest upcoming assignment deadline and
the channel's task titles, due dates, and statuses to propose three to five
dated milestones. It excludes owner identities, preferences, and round reasons.
All milestone dates must be today or later and at least one calendar day before
the assignment deadline. A same-day deadline cannot satisfy that buffer and
returns a private explanation. Multiple milestones may share a date.

The preview is ephemeral. It flags each task whose due date is after
the assignment deadline with **Heads up: "{task}" is due after the assignment
deadline.** Nothing changes existing tasks, and suggested milestones are not
saved as confirmed deadlines. Only the requester can press **Post to channel**;
the public version carries the AI label. Posting succeeds once, even with racing
clicks. If sending fails, the same preview can be retried.

Previews expire after 15 minutes, a pause, or a restart. Changed tasks/deadlines
or dates that have become past require a fresh `/rt-timeline` proposal. This
keeps generated previews out of persistent storage. Owner suggestions and
timelines use the existing JSON validator and one invalid-output retry, with
fixed private errors and no model-output logging.

## Aggregate pilot stats

`/rt-stats` shows only current-channel numbers since its first setup:

- Rounds started and closed, excluding cancelled attempts.
- The unweighted average replied-member share across non-cancelled rounds with
  a membership snapshot, including collecting rounds.
- Median seconds from `created_at` to `outcome_at`, displayed in minutes, over
  closed rounds with a logged outcome.
- Prompts shown, accepted, dismissed, and accepted/shown.
- Clarify uses and helpful/(helpful + unhelpful).
- Tasks done on time, done late, and confirmed unfinished tasks currently past
  their due time (computed live).

An undefined rate or median displays **N/A**. Repeating setup does not reset
history. Stats make no model calls or identity lookups; database queries project
only numbers/counter names. No names, user IDs, message text, task titles,
preferences, round questions, reasons, or per-member statistics enter the output.

## Reopen a previous decision

1. Run `/re-open` in the original decision's channel. Any current channel member
   can use it after setup, while the channel is active.
2. Browse the private decision log using **Previous** / **Next** (10 decisions
   per page, including history older than the last 10). Each entry shows the
   question, outcome or “open outcome”, owner, and date. Choose a decision from
   the menu to see its previous options and outcome.
3. Press **Vote again** to start a round using the previous choices, or press
   **Add option & reopen**, enter one new option (up to 80 characters), and submit.
   The new option must differ from existing choices; reserved labels are rejected.
   Repeated reopening can grow the choice list up to 24 options. The round also
   retains **None of these** as an outcome button.
4. Everyone currently able to view the channel gets a fresh round with a new
   deadline and empty responses. For an originally open-ended decision, previous
   answers become the choices for the new vote. If it had no answers, a revote
   stays open-ended; adding an option supplies its first choice.
5. Members use the normal **Share my view** flow. The original decision, responses,
   outcome, owner, and timestamps stay in the log. The new round links to the old
   decision and records its own outcome when closed. Old votes and reasons are
   never copied into the new round or synthesis.

**Back to decisions** returns to the same history page. The browser and option
modal are private and bound to the member/channel that opened them. They respect
pause and restore through the existing restart-safe routing. Repeated clicks
cannot start another active reopening of the same decision. If posting the new
round fails, that attempt is cancelled and can be retried.

Restart `python -m bot.main` to apply this update and sync `/re-open`. The database
upgrade runs automatically at startup; no manual SQL or environment changes are
needed.

## Data and privacy

SQLite lives at `./roundtable.db`, relative to the launch directory. Always run
from `roundtable/`. All eight specified tables exist, including aggregate-only
`metrics`, `rounds.outcome_at`, and `tasks.done_at`. The reopening update adds the
nullable `rounds.reopened_from` foreign key and an index that prevents duplicate
active reopenings of one decision. Existing databases upgrade automatically,
preserving historical rows. Step 4 adds `decision_prompts` for proposed structured
decisions and `channels.detection_cooldown_until` for restart-safe quiet periods.
Step 5 adds nullable `tasks.proposal_batch` and `tasks.proposal_message_id` for
restart-safe proposal controls. Model explanations are never stored; only
member-authored titles, proposed owners, timestamps, and control metadata persist.
Discord identifiers are TEXT. The UTC timestamp helper rejects naive
datetimes. SQLite enforces foreign keys, status values, and allowed metric names.

There is no chat-history table, message-content persistence API, or Discord
message cache. Command-only mode ignores every ordinary chat message without
reading its fields. Automatic mode uses only the bounded in-memory window
described above and can post an AI-labeled offer. There is no file reader.
Explicit inputs are decision questions, members' own
round answers/reasons and new options, task/deadline titles, private task
preferences, and the single selected text for clarify.
Clarify text/answers and automatic detection's raw window never enter SQLite.

Runtime stdlib logging is disabled process-wide, including Discord and LLM SDK
debug logs, to prevent request bodies or exception payloads leaking into logs.
Event errors are silent; slash-command failures return a fixed private message.
Startup failures report fixed, content-free text to stderr. Don't enable SDK
debug logging or log event/interaction objects. The OpenAI adapter requests JSON,
uses temperature zero, sets `store=False`, and sanitizes provider errors. The
Vertex adapter also requests JSON at temperature zero, uses a 30-second request
timeout, closes its async client after each call, and sanitizes SDK exceptions.
Its SDK transport retries are disabled; only the existing JSON/schema retry is
used. SDK record/replay environment flags cannot enable disk recording. The
shared Pydantic validator retries invalid JSON/schema exactly once, then returns
a quiet failure. No live API call is needed for tests.

The notice explains detection, round reminders, and private clarification. Members' answer
labels and original round reasons are persisted;
translations and synthesis are generated in memory. The more specific schema is the storage allowlist; see
[DECISIONS.md](DECISIONS.md) for spec ambiguities resolved in this step.

## Tests

From `roundtable/` with the environment activated:

```sh
python -m pytest -q
```

Latest acceptance-audit run: **524 passed, 1 warning**. The warning is the
existing google-genai Python 3.14 deprecation of `_UnionGenericAlias`; it is not
a failed bot check. The suite made no live API calls or consumed credits.

Tests use FakeProvider, in-memory SQLite, and mocked Discord interactions. The
privacy test writes a temporary snapshot of the in-memory database solely to
scan its bytes for a unique chat sentinel; it also scans SQL dumps and captured
logs. Vertex tests mock the SDK client for all prompt operations, including option
extraction, response parsing, synthesis, and the clarify contract, plus failure,
retry, configuration, and privacy paths. No production database, live Discord or
LLM connection, API key, or API credits are used.

Reopening tests cover pagination, adding an option, voting again, fresh membership
and deadlines, original-history preservation, duplicate prevention, validation,
pause/access checks, result/outcome logging, restart routing, failed-post retries,
and an in-memory upgrade from the previous database schema.

Step 3 tests cover context-menu registration, single-message intake, ephemeral
output, JSON/schema retries, counters, feedback ownership/deduplication/expiry,
pause during a model call, failed delivery, and file/log privacy scans. Early-end
tests cover permissions, empty/partial rounds, late submissions, duplicate clicks,
outcomes, publication retries, restored controls, and closure after restart.

Step 4 tests cover window bounds and idle expiry, filtering before text access,
check thresholds, confidence/schema validation, cooldowns, pause during provider
calls and retries, aggregate counters, duplicate/opposing clicks, fresh rounds,
failed sends/edits, startup migrations and restored controls, command-only mode,
and raw-chat/author-ID database and log privacy.

Step 5 tests cover owner confirmation/swaps, 24-hour expiry, due-date boundaries,
atomic completion counters, compact workload/pagination, manual dates, validated
timeline buffers and optional posting, pause/in-flight invalidation, failed
delivery/edits, restart controls/modals, old-schema migration, and numeric-only
stats queries/output. FakeProvider and mocked SDKs make no network calls.

Restart `python -m bot.main` to upgrade the database and sync the new command
groups. Rerun `/rt-setup` to refresh the notice. No new dependencies or environment
variables are required; your local `.env` is unchanged.

## Run the demo

The complete recording script, roles, expected messages, relative dates, and
optional acceptance branches are in [demo/script.md](demo/script.md). All dialogue
there is invented test material; use no real coursework, files, or private notes.

1. Start the scoped FakeProvider demo command from [Quick setup](#quick-setup-and-demo-target-under-20-minutes). Use a fresh enabled channel and three human members.
2. Send the script's four separate messages as Ana and Ben. Chen initially stays
   quiet. Expect exactly one offer; click **Yes, run a round**.
3. Ana and Ben privately choose Campus and submit their scripted reasons, with
   names Off. The public channel shows only counts. Reopen a private form to
   verify that it contains only its caller's response.
4. Chen waits for the halfway reminder, then privately chooses Local businesses
   and shares the two existing shop-owner contacts before the three-minute
   deadline. All three responses close the round immediately. The result shows
   the 2–1 spread, anonymous reasons, minority case, trade-off, and next question.
5. The humans decide that Chen's access changes their choice. A human selects
   **Local businesses**; the bot never recommends it. Finish the scene with
   `/rt-log`, showing the saved outcome privately without reasons or vote records.

The script also gives separate checks for clarify/feedback, task confirmation and
done, workload, deadline/timeline posting, aggregate stats, pause/resume, restart
controls, failed-DM fallback, and deadline closure with a missing member. Relative
dates prevent the timeline demo from becoming stale. FakeProvider returns canned
interpretations and synthesis, balances supplied task counts, and produces
deterministic milestone dates relative to the supplied deadline. It does not
translate arbitrary languages or establish any real market facts.

At `ROUND_DEADLINE_HOURS=0.05`, a reminder becomes eligible at +90 seconds and
sends on the next 60-second tick. Deadline closure happens at +180 seconds or
the next tick; late submissions are rejected at +180 seconds immediately. A
deadline result reports the received/total view count. Pausing does not stop the
deadline clock. Completing a three-minute round via the command fallback is
useful for a recording, but does not verify automatic detection.

Stop with Ctrl-C after the scoped demo. A later normal `python -m bot.main` uses
your original settings; keep `LLM_PROVIDER=fake` in `.env` if that is your desired
ongoing mode. Production rounds normally use `ROUND_DEADLINE_HOURS=24`.

## Troubleshooting

| Symptom | Check/action |
| --- | --- |
| Detection worked once, then stopped | The 15-minute offer cooldown survives resume/restart. No thanks extends it to at least 60 minutes; an unanswered offer also blocks new ones. Use a fresh enabled test channel or wait and send new messages. Keep decision history. |
| Messages after pause do nothing | Run `/rt-resume` and wait for its private confirmation, then send four new messages from two authors. Messages sent while paused were discarded. Resume does not reset cooldowns. |
| No automatic offer | Verify the running process uses `DETECTION_MODE=auto`. Send four new, plain-text messages from two humans within 10 minutes; later checks also need 45 seconds. FakeProvider needs the phrase “which market” and both Campus/Local businesses in the window. Attachments/embeds, edits, and bot messages are ignored. |
| Commands absent | Check the test-server ID, restart to sync, and verify the bot was invited with `applications.commands`. Without guild sync, global propagation can exceed the 20-minute target. |
| Setup cannot pin | Check effective channel permissions, including Pin Messages. Retry `/rt-setup`; it reuses its saved notice without enabling a new channel until pinning succeeds. |
| Generic private error | Reproduce with the scoped FakeProvider command to separate bot flow from provider configuration/availability. Check permissions and model/key settings locally; do not paste keys, prompts, model responses, or exception payloads into logs/chat. |
| Old preview/rating expires after restart | Timeline previews and clarify-rating receipts are deliberately transient. Run a fresh `/rt-timeline` or ask for another clarification. Persistent round, detection, task, and reopen controls restore. |
| Need a reliable round trigger now | Use `/rt-decide question:Which market: Campus or Local businesses?`. It bypasses detection/cooldown while respecting setup/pause. |

## Acceptance audit

**Pass (automated)** below means FakeProvider + in-memory SQLite + mocked Discord
transport, not a claim that a new multi-person Discord session or a paid model
was exercised. The full suite blocks socket connections. The scripted rehearsal
reads the exact demo tables, assigns unique bot-message IDs, and runs real
application handlers. Restart checks also route serialized components through
discord.py's actual view dispatcher. Existing targeted tests cover error and
privacy branches. Real-model grounding/neutrality still needs human review;
prompts, bounded schemas, and recommendation rejection are guards, not a proof
of every possible model response.

| Section 12 | Status | Verification |
| --- | --- | --- |
| 1. Setup, 3+ people, detection offer → Yes → round | Automated pass; live-server check pending | `test_acceptance.py::test_scripted_demo_setup_detection_private_round_result_log_tasks_timeline_and_stats` pins setup, feeds the exact four demo lines from two authors among three members, checks one offer/no automatic round, then accepts it. Live three-person run has not been repeated in this audit. |
| 2. Ephemeral sharing, no pre-close answer exposure | Pass (automated) | Rehearsal drives select → modal → gateway submission for all three members and scans every public send/edit before close; `test_round_discord.py` checks private ownership, edits, and consent. |
| 3. Spread, anonymous/opted-in reasons, minority case, outcome buttons | Pass (automated) | Rehearsal checks the 2–1 result and minority reason; `test_round_discord.py::test_all_replies_close_once_with_full_result_and_outcomes` verifies explicit name consent. |
| 4. Outcome persisted and visible in log | Pass (automated) | Rehearsal selects the minority option as a human, checks `outcome_at` and `/rt-log`, and verifies no reasons/votes appear; targeted tests check duplicate clicks. |
| 5. Selected-message clarify is private | Pass (automated) | Rehearsal invokes the registered context-menu callback and feedback without a public event; `test_clarify_discord.py` checks selected-only intake, ownership, and DB/file/log privacy. |
| 6. Add/propose/confirm tasks and show workload | Pass (automated) | Rehearsal adds two tasks, requires confirmation, checks per-member counts, then completes one; `test_tasks_core.py` / `test_tasks_discord.py` cover swaps, expiry, late counters, and races. |
| 7. Manual deadline and backward timeline | Pass (automated) | Rehearsal uses a relative seven-day deadline, checks three milestones and a one-day buffer, and posts only on consent; timeline tests cover invalid dates, warnings, and failed delivery. |
| 8. Pause stops work; resume restores it | Pass (automated) | Pause tests across detection, rounds/scheduler, clarify, tasks, and timeline verify no new reads/model calls/posts and private paused replies; resume/recovery tests restore work while preserving clocks/cooldowns. |
| 9. Buttons work after restart | Pass for persistent controls (automated) | `test_acceptance.py::test_restart_dispatches_serialized_public_and_private_controls_through_discord_store` restores an in-memory snapshot into a fresh client and dispatches old share/select/modal controls; detection/task/reopen suites cover their persistent controls. Transient timeline/rating receipts intentionally expire. |
| 10. Full pytest suite and no-chat-storage check | Pass (automated) | Run `python -m pytest -q`; privacy tests scan synthetic SQLite snapshots/SQL dumps/captured logs. Network guard prevents real API connections. See the latest run summary rather than treating this table as a fixed test-count claim. |
| 11. New user setup and demo under 20 minutes | Documentation ready; timed novice run unverified | Quick setup has a 17-minute target, guild sync, no paid-provider setup, non-overwriting config copy, and an exact rehearsed script. An independent new person's elapsed time has not been measured. |
| 12. Neutral result with trade-off and next question | Pass (automated) | Rehearsal verifies all result sections and that synthesis receives only stored reasons/labels. Recommendation regressions reject direct “Pick …”, “… is the best option”, and “We should go with …”, retry once, publish nothing invalid, and recover with neutral output. |
| 13. Aggregate pilot numbers, nothing identifying | Pass (automated) | Rehearsal checks rates/duration/completion totals; `test_timeline_stats_discord.py::test_stats_query_and_output_have_no_names_ids_message_text_or_member_breakdown` inspects SQL/output and forbids identity lookup. |

Evidence files: [integrated rehearsal](tests/test_acceptance.py),
[rounds](tests/test_round_discord.py), [reminders](tests/test_round_scheduler.py),
[clarify](tests/test_clarify_discord.py), [detection](tests/test_detection_discord.py),
[tasks](tests/test_tasks_discord.py), [timeline/stats](tests/test_timeline_stats_discord.py),
and [privacy boundaries](tests/test_client_privacy.py).

No known automated failure remains after fixes. Full live acceptance is not
claimed: a three-human test-server run and a timed first-time setup remain to be
observed. Use the demo's optional branches to record those observations without
collecting real chat or credentials.
