# Roundtable demo video: production plan

Written 2026-10-23 for a fresh Claude Code session. Goal: a **60-second product demo
video with voice-over** for the USC Marshall 7th Annual AI Global Case Competition,
showing Roundtable's UX inside Discord with the team's real names.

**Deadline context:** slides are due Fri Oct 23, 5 pm; the competition is Sat Oct 24
(check-in 7:30 am, Round I 8:45 am). The video is *not* submitted in advance, but it must
be finished, exported and preloaded on the presenting laptop before Saturday morning.

## 1. Inputs in this repo

| What | Where | Notes |
| --- | --- | --- |
| Competition rules | `docs/competition/competition-guidelines-summary.pdf` | Local only (gitignored; the repo is public). Section 13 governs the video. |
| Proposal (what the product is) | `docs/proposal/roundtable-proposal.md` | Snapshot of the live doc: https://claude.ai/artifact/RcEJcmjUwNajd6VGY1xY3Y |
| How-it-works diagram | `docs/proposal/how-roundtable-works.png` | Can be adapted for one shot |
| Brand art | `roundtable/assets/roundtable-discord-icon.png`, `roundtable-discord-banner.png` | Bot avatar, end card |
| Exact bot wording | `roundtable/bot/formatting.py`, `roundtable/bot/views.py` | Every on-screen bot message must match these strings |
| Product behavior | `roundtable/README.md`, `roundtable/DECISIONS.md` | Read before changing any on-screen flow |

## 2. Hard constraints from the guidelines (section 13)

- **At most 60 seconds**, audio/voice-over allowed. Aim for 56–58 s to leave margin.
- **Prepared and recorded.** No live demo, no internet at the venue: deliver a self-contained
  file (MP4, H.264, AAC, 1920×1080, 30 fps) that plays offline from the team laptop, and
  also embed it in the PowerPoint deck. Test playback inside PowerPoint slide show.
- It plays **inside the 8-minute running clock**, so it must start instantly (no title
  padding, no fade-from-black longer than 0.5 s).
- Judges score Completeness, Critical Thinking, Product, Storytelling, **Customer Value**,
  Professionalism, Delivery and Teamwork. The video should prove product value and tell a
  story, not list features.
- Visuals should be "seen, not read": on-screen text short enough to read in the shot's time.

## 3. Style brief ("like muse.ai's ads")

The user asked for something similar to muse.ai's ads. A quick search in the planning session
did not surface muse.ai's own ads, so this brief is an interpretation:
**clean SaaS motion-graphics product ad**. Before building, ask the user for one reference
ad link if they have it, and adjust this section.

- Bold kinetic typography on a solid dark background (2–6 words per card, big type).
- The product UI floats in a rounded, softly shadowed frame; the camera pushes in and pans
  to the exact element that matters (a button, the dots, the summary).
- Fast, clean cuts on the beat; every shot under ~6 s; smooth eased motion (no wipes,
  no stock-footage clichés).
- Upbeat, light instrumental music under a calm, confident voice-over.
- Palette: Discord dark UI plus one accent (Roundtable blurple `#5865F2` for collecting,
  green `#57F287` for decided). End card uses the Roundtable banner.

## 4. Story and cast

Seven-person case team in a Discord server, channel `#case-team`:
**Sumedha, Hosung, Ansel, Vicky, Suri, Cecilia, Aubrey.** Use simple colored initial
avatars (no real photos). Give everyone neutral, respectful lines; nobody is the villain.

Arc: the first answer is winning and two members are quiet → Roundtable offers a private
round → everyone answers privately → the neutral summary surfaces Suri's reason → the team
changes its mind and logs the decision → the decision becomes owned tasks → Vicky privately
checks an idiom → pitch line.

## 5. Storyboard (60 s) with voice-over

Word count for the VO is about 125 words, which fits 60 s at a relaxed pace.
Bot text in quotes below is copied from the real bot; keep it verbatim.

| Time | Shot | On screen | Voice-over |
| --- | --- | --- | --- |
| 0:00–0:04 | Hook card | Kinetic text: "5 opinions." → "1 plan." → "Who does that work?" | "Every team has someone who quietly turns five opinions into one plan." |
| 0:04–0:11 | Problem in chat | `#case-team` scrolls. Ansel: "ChatGPT says campus clubs, let's just go with that 👍" Hosung: "works for me". Sumedha: "local businesses could actually pay though". Cecilia: "campus clubs or local businesses? we need to pick today". Vicky and Suri: a typing indicator appears, then disappears. | "In team chats, the first answer usually wins, and quieter voices drop out." |
| 0:11–0:15 | Brand reveal | Roundtable icon + "Roundtable" + subtitle "An AI facilitator inside your team's Discord" | "Meet Roundtable, an AI facilitator that lives in your team's chat." |
| 0:15–0:21 | Detection offer | Bot message: "Roundtable (AI): It sounds like you're deciding "Which market for our pilot?" between Campus clubs, Local businesses. Want me to run a quick private round so everyone's view is heard?" Buttons **Yes, run a round** / **No thanks**. Cursor (Aubrey) clicks Yes. | "When it spots a decision, it asks first. One tap starts a private round." |
| 0:21–0:30 | Private answer | Round card (question in bold, "Closes in 1 day", seven grey dots). Zoom to Suri's private view, labeled "Only you can see this": picks **Local businesses**, **Show my name: Off**, **Add my reason**, modal "Why? One line is fine": "I already know 3 shop owners near campus who said they'd try it." Dots fill one by one to 7 of 7. | "Everyone answers privately, in any language. No names, no anchoring, just progress dots." |
| 0:30–0:40 | Result card | Grey card. "🟢🟢🟢🟢⚪⚪⚪ 4 · Campus clubs" / "🟢🟢🟢⚪⚪⚪⚪ 3 · Local businesses" / "7 of 7 shared a view". **Summary** (highlight the clause about the three shop-owner contacts), **Key trade-off**, **Next question**, then "What members said" quotes grouped by option. Footer: "The call is yours. Pick the team's decision below to log it." | "Then Roundtable does the synthesis job: a neutral summary, the key trade-off and the next question. The call stays with the team." |
| 0:40–0:44 | Decision | Cecilia types "Suri's contacts change things. Local?" Hosung clicks **Local businesses**: the card turns green, footer "✅ Decided: Local businesses · Saved to /rt-log". | (music hit, no VO, or) "Here, the team changes its mind." |
| 0:44–0:50 | Tasks | "Roundtable (AI): New task 1: **Email the 3 shop owners** · unowned. From decision: Local businesses. Use /rt-tasks assign for owner suggestions." Then the "Proposed task owners" card (Suri: Email the 3 shop owners; Vicky: Draft the pilot survey; Ansel: Build the pricing slide) and a click on **Confirm all**. | "Decisions turn into tasks, with owners balanced and confirmed by the team." |
| 0:50–0:55 | Ask privately | Ansel: "let's not boil the ocean, MVP first and circle back on the extras". Vicky right-clicks → Apps → **Ask Roundtable privately**. Private card "Private clarification": What it likely means / Terms or idioms explained (boil the ocean, circle back) / If you want to check. | "Lost in a teammate's idiom? Ask privately. Only you see the answer." |
| 0:55–0:60 | End card | Banner art. "Roundtable" / "Every view counts. One plan." / small: "Sumedha · Hosung · Ansel · Vicky · Suri · Cecilia · Aubrey · USC Marshall MSBA" | "Roundtable. Every view counts." |

Shot notes:
- 4–3 for Campus with the team then choosing Local shows "the call is yours" and that the
  minority view was heard. Do not show the bot recommending anything.
- The summary text is an example (the real one is AI-written). Keep it neutral, built only
  from the members' reasons shown in "What members said", and free of words like "best",
  "should" or "recommend" (the bot rejects those).
- The reasons for all seven members must appear in "What members said". Write the other six
  short reasons yourself (e.g. "Clubs reply fast on Instagram", "Easier to reach students").
  The quiet members' answers are anonymous: no name after their quotes.
- Don't invent pilot numbers. If the team has real VoC results by Friday, add one stat card
  (e.g. at 0:50) and trim the clarify shot; otherwise leave it out.

## 6. Production approach (recommended)

**Remotion** (React → MP4, renders deterministically from code; Node v24 is installed,
ffmpeg is not, and Remotion ships its own). Build a faithful Discord dark-theme mock as
React components, animate it per the storyboard, then render.

1. `npx create-video@latest video/remotion` (blank template, TypeScript). 1920×1080, 30 fps,
   1800 frames max.
2. Components: `DiscordShell` (server rail, channel list with `#case-team`, member list with
   the 7 names + Roundtable APP badge), `Message`, `BotMessage` (APP badge, embeds with left
   color bar, buttons), `EphemeralNote` ("Only you can see this · Dismiss message"),
   `Modal`, `ContextMenu`, `KineticTitle`, `EndCard`, `Cursor` (animated pointer + click ripple).
3. One `<Sequence>` per storyboard row; spring/easing camera via CSS transform on a wrapper.
4. Voice-over: draft a scratch track with macOS `say -v Samantha -r 165 -o vo.aiff "<script>"`
   to lock timing, then replace it with a human recording from a teammate (preferred) or a
   licensed TTS voice. Keep the final VO in `video/audio/`.
5. Music: a royalty-free track whose license allows use in a recorded presentation (for
   example the YouTube Audio Library or Pixabay Music). Record the source and license in
   `video/audio/CREDITS.md`. Duck music under the VO (about −18 dB).
6. Render `video/out/roundtable-demo.mp4` (H.264, CRF ~18). Check duration ≤ 60.0 s.
7. Add captions burned in (short lines) so it still works if the room audio is weak.

Alternative if Remotion is blocked: record the real bot in Discord with seven test accounts
(screen capture), then edit in iMovie or CapCut with the same storyboard. More authentic,
much slower to stage. A hybrid also works: Remotion for titles, real capture for 1–2 shots.

**Authenticity:** the mock must show only what the bot really does, with its real wording.
Mention in the briefing that the video recreates real bot screens. Don't use Discord's logo
on the end card or imply endorsement; showing the Discord-style interface as the setting is fine.

## 7. Acceptance checklist

- [ ] Duration ≤ 60.0 s; plays offline as MP4 and inside PowerPoint slide show.
- [ ] Every bot string matches `bot/formatting.py` / `bot/views.py` (spot-check each shot).
- [ ] All 7 names appear; anonymous answers show no names; no member is portrayed negatively.
- [ ] The bot never recommends an option; a human logs the decision and confirms owners.
- [ ] VO is clear over the music; burned-in captions are readable at a glance.
- [ ] Customer value is clear by 0:40 (quiet view heard, decision logged).
- [ ] Music and voice licenses recorded in `video/audio/CREDITS.md`.
- [ ] Final file copied to the presenting laptop and to a USB backup.

## 8. Questions to ask the user at the start of the next session

1. Do you have a muse.ai ad link (or another reference) to match the style?
2. Who records the final voice-over, or should we use TTS?
3. Remotion mock (fast, polished) or real Discord screen capture (authentic, slower)?
4. Any real pilot (VoC) results to show?

## 9. Prompt to start the next session

> Read `CLAUDE.md` and `video/PLAN.md`, then ask me the questions in section 8. After that,
> build the 60-second Roundtable demo video in `video/` following the plan, render it to
> `video/out/roundtable-demo.mp4`, and check it against the acceptance checklist.
