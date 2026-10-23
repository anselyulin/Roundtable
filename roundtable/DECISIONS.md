# Roundtable decisions

## Step 1 (historical; later decisions below describe step 2)

1. **Project root.** Section 5's `roundtable/` is a subdirectory beside the input
   spec. Run commands from that directory so `python -m bot.main`, `.env`, and
   `./roundtable.db` resolve exactly as specified. Future modules and the demo
   script are placeholders; no later feature is activated.
2. **Storage conflict (2.4 vs 6).** Treat section 6's explicit schema as the
   allowlist. Discord ids are necessary for channel settings, round membership,
   private responses, and ownership. Structured questions, task/deadline titles,
   outcomes, and member preference notes are allowed by that schema, despite its
   final sentence calling `reason` the only free text. Metrics contain only the
   channel id, an allowlisted counter name, and a count. Chat history and message
   text are never persisted or logged. Step 1 writes channel settings only.
3. **Pause vs help/setup.** The strict silence rule wins while paused: setup and
   help return only the exact one-line paused confirmation, ephemerally. Setup
   never clears pause; only resume does. Resume confirms ephemerally. Pause posts
   a normal channel message and removes its deferred private reply, avoiding
   public interaction attribution of the caller. Failed public confirmation
   leaves pause in effect and confirms privately. Control actions are serialized
   per channel to prevent setup posting after a completed pause.
4. **Bootstrap help.** Private help is available before setup so members can find
   setup instructions; it never reads chat or enables anything. Other operations
   require setup except setup itself. Bot accounts are ignored. Commands require
   a guild member who can view that guild's text channel or thread. Setup checks
   effective Manage Channels permission or server ownership at runtime.
5. **Notice retries.** Save a new notice id before pinning, and enable a new
   channel only after pinning succeeds. Repeat setup edits/re-pins that specific
   bot notice without fetching chat history. Deleted notices are replaced.
   Failed re-pinning on an already enabled channel preserves its existing state.
6. **Current Discord pin permission.** Add **Pin Messages** to the spec's invite
   permissions. Discord's current [Pin Message API](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx#pin-message)
   requires `PIN_MESSAGES`; **Manage Messages** alone no longer suffices.
7. **Providers without later features.** Provide the required interface, Fake
   fixtures for all seven templates, lazy OpenAI adapter, and Pydantic JSON retry
   helper now. Step 1 commands make no provider calls. Unstated fake behaviors use
   conservative canned outputs (no extracted options/owner assignments, null
   parsed label); timeline fixture dates are fixed in 2099 for provider tests,
   not a live timeline. Template strings preserve section 8 wording.
8. **Privacy and logging.** Disable Discord's message cache and all stdlib
   logging at the executable entry point, including SDK logs. Override default
   event/command error reporting to avoid raw exceptions. Step 1 reads no chat
   fields at all; the rolling window and detection gates wait until step 4.
9. **Earlier tests/docs.** Follow the user's instruction to add tests now even
   though section 14 groups tests/docs at step 6. Include the README needed to
   run this increment; leave the full scripted decision demo until step 6.
   All behavior tests use in-memory SQLite. The file-scan privacy test exports
   a temporary snapshot solely to verify no chat sentinel appears on disk.

## Step 2

1. **One public round message.** Edit the starter into the result, reusing
   `public_message_id`, instead of introducing a second message id or schema
   fields. Before close it contains only the question/deadline and reply counts.
   Deleted starters are replaced on close. A failed initial post cancels the
   round. Unpublished collecting rows left by a crash are cancelled at startup.
2. **Closing vs provider failures.** Freeze responses and set `closed_at` when
   all members replied or the deadline is reached, before model work. Publication
   failures retry each scheduler tick. Closed, unlogged results are re-edited once
   after restart from stored reasons. This preserves closure and avoids duplicate
   result posts without storing generated synthesis. A closed unpublished result
   already appears as “open outcome” in the log.
   Saved-outcome button disabling is retried after an API failure and reconciled
   once after restart, without changing the outcome timestamp or sending another
   public confirmation.
3. **Pause and time.** Share, private forms, modal submission, outcomes, log, and
   reminders respect pause. Model calls run outside the channel lock; state is
   checked again before saving or publishing. Deadline clocks keep running while
   paused, and overdue rounds close after resume. The scheduler ticks every 60
   seconds; late submissions are blocked at the exact deadline regardless.
4. **DM wording conflict.** Section 7.3's no-DM default applies to submission.
   Use ephemeral forms/modals exclusively for views; implement section 7.5's DMs
   only for one-time reminders. Mark `reminded=1` before attempting each DM, so
   failures and restarts cannot cause repeated DMs. Any failed DM triggers one
   fallback mentioning all currently visible missing members. A failed fallback
   is not automatically reposted, favoring the stated no-repeat behavior.
5. **Modal acknowledgements and restarts.** Opening a modal uses `send_modal` as
   the immediate acknowledgement: Discord cannot defer and then open a modal.
   Submission defers ephemerally before any parsing. Stable public views are
   re-registered in `setup_hook`; dynamic private controls encode only ids,
   selection, and consent. Modal submits use a single id-based gateway route so
   an already-open modal works after restart without retaining its text in a
   process-local registry. Expired ephemeral forms can be reopened publicly.
6. **No stated options.** The select contains only “Another option”. Parse its
   submitted reason for a short label; an unclear/null label fails privately
   without saving. Known-option selection is authoritative. Original reasons
   alone are persisted; `PARSE_RESPONSE` supplies temporary English renderings
   at close, without adding translation fields or a general translation feature.
7. **Minority/ties/unanimity.** Consider only options with at least one response,
   since an unchosen option has no stated case to synthesize. The least-supported
   represented option is the minority; ties follow option order. Unanimous or
   empty rounds show “No dissent raised.” Trade-off and next question still
   appear; empty rounds use neutral fixed text without an LLM call.
8. **Input and output bounds.** Questions/reasons are capped at 300 characters;
   extracted/parsed option labels at 80 for Discord buttons. Extraction may not
   invent options absent from the question. Explicit forbidden work requests
   are refused before persistence/provider calls. The seven spec prompts remain
   verbatim, with a separate global provider safety instruction. Pydantic rejects
   malformed output and common recommendation/personal-attack wording, retries
   once, and returns fixed private errors. Formatting escapes mentions/Markdown,
   preserves original reasons, and respects Discord message/embed size limits;
   an oversized result is held rather than silently dropping member content.
9. **Membership and ownership.** Snapshot every non-bot guild member who can view
   the channel, chunking the member cache if needed; private threads also require
   thread membership or Manage Threads. Only snapshotted members can submit/view
   their own reason. Any currently authorized channel member may log the outcome.
   The first outcome wins atomically, setting its owner and `outcome_at`; elapsed
   decision time is measured from `created_at`, never `closed_at`.
10. **Task handoff and scope.** The outcome confirmation offers tasks with a
    disabled button labeled for step 5. No task flow, detection, clarify, timeline,
    stats, new integrations, or section 13 features are built. Update help/notice
    and the command-path README demo now; keep the full scripted demo for step 6.
11. **FakeProvider now exercises rounds.** Its deterministic demo grammar parses
    `Question: A or B` or `A | B | C`, free labels precede ` — ` or `:`, and canned
    synthesis quotes only submitted reasons. It does not translate arbitrary
    languages. Unchanged future-operation fixtures remain inert. All tests use
    FakeProvider/in-memory SQLite (plus temporary snapshots for privacy scans),
    mocked Discord, and no production credentials or live APIs.

## Gemini provider addition (historical; superseded by Vertex migration below)

1. **Scope and defaults.** Add `GeminiProvider` through the existing plain-text
   `Provider.complete(system, user)` interface. Select it only with
   `LLM_PROVIDER=gemini`; keep OpenAI as the default and preserve OpenAI/Fake
   implementations, all Discord commands, schema, prompts, and product behavior.
   No provider fallback or new product flow is introduced. In particular,
   `CLARIFY_MESSAGE` is supported as an existing prompt contract, while its
   still-placeholder Discord feature stays unimplemented.
2. **Configuration.** Gemini requires `GEMINI_API_KEY` and the existing shared
   `LLM_MODEL`, plus the existing `DISCORD_TOKEN` for the bot. OpenAI credentials
   are unnecessary in Gemini mode. The new key uses `repr=False`, and validation
   errors include variable names only. Do not edit the user's real `.env`, choose
   a default model, restart the bot, or make live API calls during implementation.
   The README gives `gemini-flash-latest` as an SDK-documented example; the model
   can be replaced with any suitable Gemini model accessible to the user's key.
3. **SDK transport.** Use official `google-genai>=1.75,<2`, tested with 1.75.0.
   Initialize lazily and explicitly select the Gemini Developer API
   (`enterprise=False`) with the configured key, rather than ambient enterprise
   credentials. Use `async with Client(...).aio`, a 30,000 ms timeout, and a single
   transport attempt to match the existing provider failure/retry behavior.
   Request `application/json` at temperature zero; combine the unchanged global
   safety instruction and operation prompt in `system_instruction`, keeping
   caller input separate in `contents`. No tools, file access, history, or caches
   are configured; automatic function calling is disabled.
4. **Shared validation.** Preserve `request_json` unchanged: validate with the
   caller's existing Pydantic model, retry invalid JSON/schema exactly once, then
   return `None` for the existing safe error. Do not duplicate the model registry
   or move validation into SDK-specific response schemas. This keeps every prompt
   operation usable, including EXTRACT_OPTIONS and CLARIFY_MESSAGE, without
   provider-specific product rules. Empty/blocked outputs take the same invalid
   JSON path; transport errors keep the existing quiet failure without retries.
5. **Privacy.** Sanitize all SDK initialization, generation, response-read, and
   cleanup exceptions into the existing value-free `ProviderError`, suppressing
   raw exception context. Add no logging or persistence. Explicitly clear SDK
   `DebugConfig` record/replay settings so environment flags cannot write request
   or response bodies to disk. Existing process-wide runtime logging suppression
   also covers Google SDK loggers; extend its regression test to include them.
6. **Verification.** Mock every Gemini SDK Client in the new tests. Reuse existing
   round Pydantic models and FakeProvider fixtures; unimplemented prompt contracts
   use test-only models, without creating runtime product modules. Cover all
   seven prompt templates, exact retry count, empty outputs, cleanup, safe
   errors, SDK recording flags, provider selection, config/secret handling, and
   the existing `/rt-decide` success/error paths using in-memory SQLite.
   Tests use synthetic keys and consume no credits. The official SDK reference
   is [Google Gen AI SDK documentation](https://googleapis.github.io/python-genai/).

## Vertex AI / Agent Platform migration

1. **Scope.** Replace the Developer API adapter and `LLM_PROVIDER=gemini` with
   `VertexAIProvider` and `LLM_PROVIDER=vertex`. Remove runtime consumption of
   `GEMINI_API_KEY`; never fall back to that API or key. Preserve Fake/OpenAI,
   existing Pydantic models, prompts, commands, database, and shared retry/error
   behavior. Clarify remains an unimplemented product flow.
2. **Authorization-key routing.** Keep official `google-genai>=1.75,<2`.
   SDK 1.75.0 rejects explicit `project`/`location` together with `api_key`.
   Use `Client(vertexai=True, api_key=...)` and its supported full model resource
   `projects/{project}/locations/global/publishers/google/models/{model}`.
   Explicitly set the Cloud base URL and stable `v1` API. This sends the key in
   `x-goog-api-key` to a project-scoped Cloud endpoint without ADC, a credential
   file, or ambient Developer API keys. A mocked real-SDK transport test asserts
   the complete request URL/header and forbids ADC and socket connections.
3. **Configuration.** Require `VERTEX_API_KEY`, `GOOGLE_CLOUD_PROJECT`, and
   `LLM_MODEL`; `GOOGLE_CLOUD_LOCATION` defaults to and accepts only `global` for
   this migration. Accept project ids or numeric project numbers. Require a short
   model id so configuration cannot override the chosen project/resource path.
   Secret fields stay excluded from settings/provider representations; config
   failures contain variable names only. Keep OpenAI as the existing default.
4. **Cloud-side ownership.** A service-account binding and API restriction cannot
   be inferred from an opaque key. The provided authorization key must already
   be bound to `roundtable-gemini`, restricted to Agent Platform API
   (`aiplatform.googleapis.com`), with inference permission in the configured
   project. No keys, IAM policies, billing settings, service-account JSON files,
   or Cloud resources are created or modified. Cloud billing determines whether
   trial credit applies; this implementation does not inspect the credit balance.
5. **Privacy and retries.** Preserve JSON mode, temperature zero, no tools or
   cache, disabled automatic function calling, 30-second timeout, and a single
   SDK transport attempt. Explicitly disable SDK disk record/replay. Retain
   process-wide runtime logging suppression and sanitized `ProviderError` with
   suppressed exception context. The unchanged shared validator retries invalid
   JSON/schema once. Provider tests cover all seven existing prompt contracts,
   including EXTRACT_OPTIONS and CLARIFY_MESSAGE, using mocks/FakeProvider and
   in-memory SQLite; no real credentials or paid requests are used by pytest.
6. **Live verification pending.** At implementation time `GOOGLE_CLOUD_PROJECT`
   and `VERTEX_API_KEY` are absent from the local configuration. Requested that
   the user add them locally without sharing credentials in chat. Consequently
   no model has yet been selected as available and no real Vertex connectivity
   test has run. Do not guess a model or reuse the Developer API key. Leave the
   user's `.env` unchanged until Cloud credentials and a verified model are
   available. This entry must be updated after the authenticated checks succeed.

References: [Google SDK](https://googleapis.github.io/python-genai/),
[Google Cloud authorization keys](https://docs.cloud.google.com/docs/authentication/api-keys),
[stable model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions).

## Open-ended decision answers

1. **Explicit answers.** The user's request supersedes section 7.3's no-options
   "Another option" selection and step 2 decision 6's inferred answer label.
   When extraction returns no options, the ephemeral form offers **Add my
   answer**, with required **Your answer** (80 characters) and **Why? One line
   is fine** (300 characters) fields. Keep the existing name-consent toggle.
   Fixed-option questions retain their existing select/reason flow.
2. **Storage and validation.** Store the trimmed member-authored answer in the
   existing `responses.option_label`, with the original reason in `reason`.
   No schema migration or additional storage is needed. Validate both inputs
   against existing content boundaries; reject blank/oversized answers and
   reserved outcome labels. An explicit answer cannot override a fixed option.
   Saving an open answer needs no LLM call. Existing close-time translation and
   neutral synthesis continue to use members' submitted labels and reasons.
3. **Privacy and outcomes.** Each member sees only their own saved answer/reason
   when reopening the form and may edit until the deadline/closure. Counts alone
   appear before close. Distinct submitted answer labels become outcome buttons,
   with identical trimmed labels counted together, followed by **None of these**.
   Existing anonymous results, reminders, outcome timestamps, log, pause rules,
   and restart handling continue to apply. No input or response logging is added.
4. **Older forms.** Keep persistent component ids unchanged. Already-open
   no-options modals from before this update lack the now-required answer field
   and fail privately without saving; reopen **Share my view** for the new form.
   Previously stored answers remain editable and retain their existing labels.
   Cover open questions, private forms, validation, edits, closure, outcomes,
   deadlines, pause, and restarts with FakeProvider/in-memory SQLite and mocked
   Discord; no real LLM calls or credits are used.

## Reopen previous decisions

1. **Command and history.** Use the exact new slash command `/re-open`, as
   requested, without an `rt-` prefix. Any current member with channel access can
   browse that channel's closed decisions, including ones awaiting an outcome.
   Show 10 per page with Previous/Next and a selection menu; keep `/rt-log`'s
   existing last-10 behavior. Clamp stale page numbers. The browser shows only
   question, outcome, owner, and date; selection adds prior option labels, never
   member reasons or individual votes.
2. **Fresh round, preserved history.** Interpret reopening as creating a new
   round linked to the selected closed decision. Preserve the old decision,
   responses, owner, outcome, and timestamps. Snapshot current visible non-bot
   members, reset all reply/reminder/consent states, and use a new full deadline.
   Members submit again. The public starter identifies the source decision; the
   new round has its own result and outcome in the log. No old reasons or votes
   enter the new synthesis.
3. **Choices.** Vote again reuses the source choices. For a previously open-ended
   decision, reuse its submitted answer labels; a source with no answers stays
   open-ended. Add option & reopen accepts one explicit 1–80 character option,
   with existing input boundaries and case-insensitive duplicate/reserved-label
   rejection. A first option may stand alone when the source had none. Reopening
   bypasses extraction so existing choices are preserved exactly; allow up to
   24 choices, leaving room for None of these within Discord's 25-button limit.
   Normal `/rt-decide` extraction keeps its existing 2–4 option contract.
4. **Minimal schema extension.** Add nullable `rounds.reopened_from INTEGER`
   referencing `rounds(id)`, with an idempotent startup migration for old SQLite
   databases. A partial unique index permits only one collecting child per source,
   preventing duplicate active rounds from repeat clicks, stale modals, or races.
   Cancel a child when its starter fails to post, permitting a retry. Existing
   orphan-round cancellation and persistent share/outcome routing apply to it.
5. **Interaction privacy.** Browsing, selection, and add-option forms are
   ephemeral/member-bound and channel-bound; all actions recheck current access,
   setup, and pause. Acknowledge before membership/network work; opening a modal
   acknowledges with send_modal. Recheck pause under the shared channel lock
   before creating/posting. Use dynamic ids containing ids/page/action only and
   the restart-safe gateway modal route. No prompts, reasons, responses, or
   credentials are logged; reopening itself requires no LLM call.
6. **Scope and validation.** This user-requested extension adds only reopening
   and its history browser; no later build steps or section 13 features are
   enabled. Tests use FakeProvider, in-memory SQLite, and mocked Discord,
   including old-schema migration, pagination, validation, preserved history,
   fresh voting/outcomes, duplicates, failures, pause/access, and restarts.

## Step 3: private clarify and early ending

1. **Exact trigger and intake.** Register the message context menu **Ask Roundtable
   privately**, guild-only, with no clarify slash command. Omit the optional
   "What's confusing?" modal for the simplest specified flow. Defer ephemerally
   before work; require setup/current guild-channel access and an active channel
   before accessing the selected message. Verify it belongs to that same channel.
   Read its text only, never authors, attachments, embeds, reply targets, links,
   or surrounding chat. Attachment/embed-only messages fail privately because
   their text is empty; mixed messages supply only text. Cap selected text at
   4,000 characters and reuse existing input boundaries.
2. **Core schema and output.** Implement platform-independent `ClarifyService`
   and strict Pydantic `Clarification`/`ClarifiedTerm` schemas in `core/clarify.py`.
   Keep the spec's CLARIFY_MESSAGE template and provider safety instructions
   unchanged, using shared JSON validation and one invalid-output retry. Bound
   each field/list so escaped output fits a Discord embed; never silently truncate
   an answer. Show the specified headings, omitting empty optional sections.
   All answers and errors are private, with no public usage announcement.
3. **Aggregate-only counters.** Count `clarify_uses` only after successfully
   sending an explanation; refusals, provider failures, failed delivery, and
   pause-discarded output do not count. Count Helpful or Not helpful once per
   answer using atomic metric upserts and the shared channel lock. Persist only
   channel/counter/count in the existing metrics table; never selected text,
   answers, source ids, usage records, or member ids. No schema change is needed.
4. **Feedback lifetime vs persistence.** Use random, one-use in-memory receipts
   containing channel/expiry only, with a 15-minute TTL and at most 1,024 pending
   receipts. Buttons bind the receipt to the asking member/channel; do not retain
   source text or output in the view. Consume a receipt after the counter write,
   so repeated/opposing clicks cannot double-count even if disabling the buttons
   fails. Pending feedback expires on restart (or memory eviction); dynamic
   routing returns a private expired message for old controls. This deliberately
   avoids persisting per-use receipts or identities just to preserve feedback.
5. **Early-end authorization and privacy.** Interpret the user's early-end request
   as closing a collecting round with its received views. Permit its starter,
   server owner, or Manage Channels members; ordinary members can still end
   rounds they started. Add **End round early** beside **Share my view**, with
   persistent ids, runtime guild/access/message binding, setup, and pause checks.
   Public starter wording discloses this closure condition before members share.
   No per-member blame or early-ender attribution is posted.
6. **Early-end state and recovery.** Freeze before LLM work under the channel lock,
   saving `closed_at` once and preserving the original deadline. Existing closed
   state blocks new submissions/reminders and survives restarts. Publish the same
   neutral result/outcomes, saying **Ended early. Proceeding with n of m views.**
   when incomplete. No replies means a fixed neutral result without an LLM call.
   `outcome_at` remains unset until a member logs an outcome. Infer incomplete
   early closure from existing timestamps/state, with no new schema field.
   Preserve existing failed-publication retry/reconciliation and duplicate guards.
7. **Existing messages and scope.** On the first active scheduler tick after
   startup, refresh collecting bot messages with the new control and disclosure;
   paused channels defer refresh until resume. Deleted messages keep existing
   close-time recovery. Update help, the setup notice, and README; an existing
   pinned notice updates on the next authorized `/rt-setup`. Tests/docs accompany
   this step per the user's rules. Do not start detection (step 4), later product
   features, or anything from section 13. Tests use FakeProvider, mocked SDKs and
   Discord, and in-memory SQLite; privacy snapshots contain synthetic test data.

## Step 4: detection and the Yes/No offer

1. **Window and check timing.** Implement platform-independent detection in
   `core/detection.py`, with Discord intake and rendering in `bot/`. Keep at most
   15 text messages per enabled, active, detection-enabled channel, removing each
   at its 10-minute boundary even when no further messages arrive. The first
   eligible four-message/two-author window may be checked immediately; later
   checks require four new still-recent messages and at least 45 seconds. Permit
   one in-flight check per window and one unanswered offer per channel. Cooldown
   gates model calls as well as posts; an unanswered offer suppresses further
   offers until resolved. Defaults remain auto, confidence 0.75, and 15 minutes.
2. **Intake and privacy.** Skip bots, DMs, system messages, edited messages/events,
   messages containing attachments or embeds, and unavailable channels before
   reading text. Check attachment/embed presence only, never their contents.
   Reuse existing forbidden-input validation and the 4,000-character text bound;
   refused text clears the window. Keep author IDs only in the transient window;
   send anonymous A/B/etc. initials, local to that window, with numbered text.
   Never fetch history, files, reply targets, or display names. Command-only mode
   returns before inspecting any ordinary message fields. Explicit commands,
   selected-message clarify, and prior Yes/No controls still work in that mode.
3. **Validation and cancellation.** Preserve the DETECT_DECISION prompt verbatim
   and the shared strict Pydantic JSON validation/one-retry path. Negative results
   accept a blank question and an empty options list. Positive offers require a
   nonblank question, configured confidence, and two to four distinct bounded
   option labels mentioned in the source window (case-insensitive matching).
   Detection failures are silent. Bound model work by the earliest source-text
   expiry; recheck before each JSON attempt and before posting. Pause clears and
   invalidates the window immediately, including across a rapid pause/resume;
   late output is discarded. Release request snapshots when evaluation finishes.
   Add no logging, SDK recording, or source-message persistence.
4. **Minimal durable decision metadata.** Extend the existing schema with
   `decision_prompts`: proposed structured question/options, bot message ID,
   pending/accepted/dismissed/cancelled state, timestamps, and optional linked
   round. Treat this as decision metadata within the storage allowlist, not chat
   history. Save no raw source text, source IDs, source authors, or clicker records
   in this table. Add `channels.detection_cooldown_until` because `last_prompt_at`
   alone cannot preserve both the actual posting time and a dismissal's longer
   quiet period. Migrations are idempotent; preserve existing decisions/responses.
   No collecting round or membership snapshot is created before Yes.
5. **Consent, counters, and races.** Use persistent Yes/No controls bound to the
   known bot message/channel; recheck setup, current membership/access, and pause.
   Yes snapshots current visible non-bot members and starts the existing share
   flow directly from detected choices, without EXTRACT_OPTIONS. The first valid
   click claims the prompt and updates its aggregate counter atomically. Yes
   creates its linked round in the same transaction. Repeated/opposing clicks
   cannot double-count or create multiple rounds. Count `prompts_shown` only
   after the send returns a bot message ID. `prompts_accepted` means accepted Yes
   intent: it remains counted if the subsequent starter send fails and the round
   is cancelled. No counts once and sets a quiet period of at least 60 minutes
   from dismissal, preserving any longer configured cooldown. The hard AI-label
   rule takes precedence over the unprefixed dismissal copy: use
   `Roundtable (AI): Okay, I'll stay quiet.`
6. **Restart and delivery recovery.** Restore pending controls and durable quiet
   periods on startup. Consumed prompts render disabled; failed edits retry on
   active scheduler ticks, including after restart, without repeating counters.
   Missing bot messages are not recreated. Cancel unpublished prompt drafts on
   failed sends or startup. A crash after Discord accepts a send but before its
   ID is saved can leave an inert orphan offer; do not fetch chat history to
   recover it or claim exactly-once delivery across Discord and SQLite. No
   confirmed shown counter is invented for such an unconfirmed send.
7. **Scope and tests.** Update help, the pinned-notice template, README, and
   example configuration. Existing pins refresh on authorized `/rt-setup`.
   Exercise detection, timing/expiry, filtering, private acknowledgements,
   counters, consent races, pause, failures, migrations, and restart recovery
   with FakeProvider, mocked Discord, and in-memory SQLite. Privacy scans use
   synthetic text and temporary database snapshots only. No live APIs, real
   credentials, or credits are used. Tasks, workload, deadlines, timelines,
   `/rt-stats`, the full scripted demo, and section 13 features remain unbuilt.

## Step 5: tasks, workload, deadlines, timeline, and pilot stats

1. **Member-written tasks and private preferences.** Implement task and stats
   rules in `core/tasks.py`, timeline rules in `core/timeline.py`, SQL in
   `core/db.py`, and Discord transport/views/formatting in the existing `bot/`
   modules. Keep the section 5 layout. `/rt-tasks add` takes a 200-character
   member-authored title and an optional date. The outcome's Turn into tasks
   button opens a title/date modal and links the task to that logged round;
   no model generates tasks or deliverable content. Preferences are the caller's
   own note only, up to 300 characters, stored under the existing explicit schema
   allowlist. Reuse input boundaries, private acknowledgements, setup, current
   guild/channel access, and pause checks. Never echo notes publicly.
2. **Owner proposals and batch size.** Suggest owners for up to 10 unowned tasks
   per invocation so Confirm/Swap per task plus Confirm all fit Discord's limit.
   Repeat assign for remaining tasks. Supply only task metadata, current visible
   non-bot member IDs, open task counts, due dates, and those members' own notes;
   no names, round responses, grades, or performance measures. Pending unexpired
   owners reserve a count when making further suggestions. Validate the unchanged
   PROPOSE_OWNERS JSON contract, including exact supplied task/member scope,
   unique tasks, and reasons of at most 12 words; retry malformed/schema output
   once. Validate reasons but discard them rather than risk exposing a preference
   through a model explanation. The public proposal lists tasks and owners only.
   FakeProvider deterministically balances the supplied counts without network
   access. Stale task/count/preference/membership snapshots cannot overwrite newer
   state. The team can confirm any proposal; nobody is assigned finally by an
   LLM response or a swap alone.
3. **Durable controls and expiry.** Add nullable `tasks.proposal_batch` and
   `tasks.proposal_message_id`, with idempotent migrations. They contain only
   task-control metadata, preserving existing tables and task rows. Stable dynamic
   buttons/selects and the single modal-submit gateway route survive restarts.
   Bind public controls to the saved bot message/channel and private selects to
   their requesting member/channel. Single-task confirmation binds the displayed
   owner; bulk confirmation includes a numeric fingerprint of task IDs, owners,
   and statuses so a stale button cannot confirm changed suggestions. Confirm
   all applies only to its batch. Swaps remain proposed and keep the original
   24-hour expiry. At the exact boundary, unconfirmed owners revert to unowned;
   retain the task and confirmed owners. Check expiry on actions and in the
   scheduler. Failed edits retry on active ticks and once after restart, without
   repeating mutations. Failed sends or unpublished startup drafts revert to
   unowned. As with detection, a crash across Discord/SQLite can leave an inert
   orphan message; fetch no chat history to recover it. Old outcome messages
   whose step-5 handoff was disabled remain unchanged; commands are available.
4. **Completion and workload semantics.** Any current channel member may mark
   only a confirmed unfinished task done. Atomically update status/`done_at` and
   one allowlisted channel counter. Completion on or before `due_at`, including
   exact equality, or with no due time counts on time; later completion counts
   late. Repeated/racing calls preserve the first timestamp/counter. Keep the
   existing owner, recording no per-member completion metric. List tasks
   privately with eight per page, open/done totals, and confirmed open counts per
   current visible member in member order (including zeros). Pending owners are
   labeled as suggestions and excluded from this displayed workload count. Show
   unavailable members' open count generically. No ranking or other analytics.
5. **Dates and timeline proposals.** Interpret every YYYY-MM-DD as inclusive UTC
   end-of-day, `23:59:59.999999+00:00`, with strict date validation. Past task due
   dates are allowed for overdue tracking; manual assignment deadlines must be
   today or future. Choose the nearest upcoming `kind=assignment` deadline.
   Preserve BUILD_TIMELINE verbatim, requesting/validating three to five distinct
   milestone titles with dates today or later and at least one calendar day
   before the assignment deadline. Dates can repeat and are sorted for display;
   reject a same-day deadline before model work because its buffer is impossible.
   Use only task titles, due dates, and statuses as task input. Flag tasks due
   after the assignment deadline, including completed tasks. Do not create confirmed milestones,
   change tasks, or add calendar integration. Timeline previews are private
   proposals, with one-use, member/channel-bound Post to channel controls; label
   public posts AI-generated and suppress all mentions. Retain at most 128
   previews in memory for 15 minutes; pause/restart expires them. Changed task
   snapshots/deadlines or past milestone dates require a fresh preview. Failed
   sends can retry; successful sends consume the receipt before editing controls
   so failed edits/racing clicks cannot duplicate a public post.
6. **Aggregate-only stats boundary.** Keep a numeric `PilotStats` projection with
   no identity or text fields. Its SQL queries select aggregate counts, per-round
   reply ratios, numeric elapsed durations, and metric names/counts only; never
   questions, response text, task titles, preferences, or member IDs. Formatting
   accepts only that numeric projection and performs no identity lookup or model
   call. Since setup means all data for the enabled channel; repeated setup does
   not reset it. Exclude cancelled attempts from rounds started/closed and reply
   share. Average each non-cancelled round's replied/member ratio equally,
   including collecting rounds; zero-member snapshots have no defined ratio and
   are excluded. Median decision duration uses `outcome_at - created_at` on
   logged closed rounds, never closure time. Prompt accept rate is accepted/shown;
   helpful rate is helpful/(helpful + unhelpful). Undefined rates/medians display
   N/A. Show saved on-time/late completion counters and compute overdue confirmed
   unfinished tasks live. Stats are ephemeral, channel-scoped, and contain no
   names, user IDs, content, ownership/start attribution, or per-member breakdown.
7. **Pause, errors, and scope.** Model work stays outside shared channel locks;
   writes/public sends recheck active state under the lock. Pause invalidates
   in-flight task/timeline generations, including across rapid resume and before
   JSON retries. Discard late output and private timeline receipts. Add no logs,
   raw exception payloads, prompt/response persistence, dependencies, credentials,
   or environment variables. Update help, notice, and command documentation;
   existing pins refresh on `/rt-setup`. Tests use FakeProvider, in-memory SQLite,
   mocked Discord/SDKs, and synthetic privacy snapshots only; make no real API
   calls or consume credits. No section 13 feature or full step-6 scripted demo
   is implemented. The user's `.env` and production database are untouched by
   implementation/tests; schema upgrades run when the bot is restarted.

## Step 6: acceptance audit, remaining tests, and demo documentation

1. **Verification scope.** Add an integrated acceptance rehearsal using the exact
   invented chat/reason tables in `demo/script.md`, real application handlers,
   FakeProvider, in-memory SQLite, unique simulated bot-message IDs, and mocked
   Discord transport. Exercise setup/pinning, one offer and explicit Yes,
   three private submissions, missing-member reminder fallback, closure,
   minority-result sections, human-selected outcome/log, private clarify/rating,
   confirmed tasks/workload/completion, a relative-date timeline and optional
   posting, and numeric-only stats. Scan synthetic database snapshots and logs
   for raw chat/selected-text sentinels. Add a suite-wide socket connection guard
   to prevent accidental live API requests/credits. No real `.env`, production
   database, Discord message history, or credentials are read by these tests.
2. **Actual restart dispatch.** Beyond direct callback tests, serialize public
   and private controls into Discord component objects, restore SQLite into a
   fresh client, run its mocked startup registration, and dispatch old controls
   through discord.py's actual ViewStore. Submit the old modal through the
   gateway route. This checks routing as well as state restoration without
   pretending a mocked run is a live Discord session. Existing detection/task/
   reopen restart tests remain. Timeline/rating receipts keep their previously
   documented transient lifetimes; no schema or persistence expansion is added.
3. **Acceptance failure fixed: recommendation guards.** New tests demonstrated
   that “Pick Campus.”, “Campus is the best option.”, and “We should go with
   Campus.” could pass the existing neutral-synthesis validator. Extend its
   conservative wording rejection to imperative choices, reversed best-option
   claims, and go-with/opt-for recommendations. Test all three synthesis fields;
   invalid output retries once, never publishes/persists, and recovers on a
   neutral later result. Keep prompts and Pydantic output schemas unchanged.
   These guards do not prove that every possible natural-language model output
   is grounded or neutral; real-model output still needs human review.
4. **Acceptance failure fixed: pause and JSON retries.** New regressions found
   extraction, response parsing, synthesis, and clarify could start their invalid
   JSON retry after the channel was paused. Add a stateless CheckedProvider guard
   before each attempt, plus per-channel round/clarify generations invalidated
   by the existing pause action. Recheck after model work and before publication;
   carry generation metadata only in prepared Python objects, never SQLite or
   prompts. Discard valid late output even across a rapid pause/resume. Prepared
   questions/results cannot later be committed after invalidation. Requests
   already in flight are not claimed to be remotely cancelled. Preserve round
   closure/failure recovery, fixed private errors, provider configuration,
   counters, feedback lifetimes, and all existing structured schemas.
5. **Demo and setup choices.** The main four-minute scene has two initially
   disagreeing speakers and one quiet member whose concrete contacts change the
   humans' decision despite a 2–1 spread. The bot never calls an option best;
   humans choose and the log ends the scene. Separate branches cover tasks,
   clarify, timeline, stats, pause, restart, reminders, deadline/early closure,
   and the command fallback. Use process-scoped FakeProvider/three-minute
   settings so `.env` stays unchanged, fresh channels to respect cooldowns, and
   relative UTC dates. Never delete history to reset a take. Document FakeProvider
   limits and restoration of normal settings. A fallback recording does not
   certify automatic-detection criterion 1. Protect existing `.env` on first
   installation, prioritize guild sync, validate shell snippets/local links,
   and remove build-stage wording from help.
6. **Honest acceptance statuses.** README maps section 12 criteria 1–13 to
   reproducible test evidence. All automated checks pass after fixes. Criterion
   1's offline equivalent passes, but a new live three-human Discord run was
   not observed in this audit. Criterion 11 has a documented 17-minute setup/
   demo target, but no independently timed novice run. Keep these live/timing
   checks explicitly unverified rather than certifying the entire MVP from
   mocks. No live messages, paid requests, Cloud/billing changes, new dependencies,
   environment variables, schema changes, or section 13 features are introduced.

## Vote-progress visual from hosung

- Port only the count-based progress display from `origin/hosung` commit
  `42fee89`. Keep the existing round message, results, providers, deadlines,
  controls, and Sumedha detection changes; that branch bundles unrelated work.
- Use green/white dots for groups of up to ten and the branch's ten-segment bar
  for larger groups. Keep the exact numeric footer for accessibility and precise
  counts. An empty group displays a dash. Dots represent counts, never members,
  options, or reasons. Updates reuse the existing saved-response progress path,
  so editing a submission does not increase the count.
- No schema, configuration, persistence, or model-call changes. Tests cover the
  initial display, private submission updates, edits, empty/full counts, larger
  groups, and absence of identities/choices/reasons in the progress embed.

## No thanks dismisses only the current offer

- The user's updated behavior supersedes section 7.2's 60-minute dismissal
  extension and the earlier step-4 decision. No thanks resolves/disables only
  that offer, creates no round, and increments prompts_dismissed exactly once.
  Public/private confirmations now say the round will not start.
- Preserve the configured normal offer cooldown from the original posting time;
  dismissal never extends or restarts it. This retains the anti-spam behavior
  while allowing a later discussion to trigger detection normally.
- Enforce that cooldown using last_prompt_at and current configuration. Retain
  detection_cooldown_until for database compatibility, but stop honoring legacy
  extended timers, so existing 60-minute dismissals also stop blocking detection
  once the normal cooldown expires. No production database edits or reset of
  history, metrics, or settings are needed.
- Tests use FakeProvider and in-memory SQLite to verify no new round, one-shot
  counters/buttons, normal and late dismissal, restart, legacy timers, configured
  5/15/90-minute cooldowns, and a subsequent automatic offer.

## Decision deadlines from hosung

- Selectively port the optional /rt-decide deadline argument, relative-duration
  parser, presets, and autocomplete from origin/hosung commit 42fee89. Exclude
  its unrelated result-summary, resume, task-announcement, and asset changes.
  Preserve the current vote-progress visual and No thanks behavior.
- Custom durations accept minutes, hours, days, and weeks, including decimal
  amounts and the branch's unit aliases, from five minutes through 30 days.
  Validate before option extraction and again at the core start boundary. Use
  an explicit None check so invalid zero overrides cannot silently fall back.
- Omitting the argument keeps ROUND_DEADLINE_HOURS, including the existing
  three-minute demo default. Auto-detected/reopened rounds retain that default.
  Store the chosen deadline in the existing deadline_at field; existing display,
  scheduler, exact-deadline submission guards, and restart recovery use it.
- No new environment variables, schema changes, paid calls, or live Discord
  messages. Tests use FakeProvider/mocks/in-memory SQLite to cover parsing and
  boundaries, autocomplete and command registration, invalid-input privacy and
  no model calls, persisted custom deadlines, reminders, closure, and late votes.
