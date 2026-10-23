# Roundtable

Discord bot (Python 3.11+, discord.py) that facilitates student team decisions, built for
the USC Marshall 7th Annual AI Global Case Competition (competition day: Sat 2026-10-24).

- `roundtable/`: the bot. Run tests with `roundtable/.venv/bin/python -m pytest -q` from
  `roundtable/`. Start it with `python -m bot.main` after activating `.venv`. Secrets live in
  `roundtable/.env` (gitignored); never put real values in `.env.example`.
- `roundtable/README.md` explains every feature; `roundtable/DECISIONS.md` records design
  choices, including deliberate departures from `build_spec_discord.md` (the original spec).
- `docs/proposal/`: snapshot of the team proposal (live doc:
  https://claude.ai/artifact/RcEJcmjUwNajd6VGY1xY3Y).
- `docs/competition/`: competition guidelines PDF, kept local only (the GitHub repo is public).
- `video/PLAN.md`: the plan for the 60-second demo video. **Start there for video work.**

Team: Sumedha, Hosung, Ansel, Vicky, Suri, Cecilia, Aubrey. Work on branch `hosung`.
