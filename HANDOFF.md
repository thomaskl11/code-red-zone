# Code Red Zone — Project Handoff

An AI-run ESPN fantasy football team ("Code Red Zone") in the Christmas
Sunday Fantasy Football League (CSFFL). This file exists so a fresh
Claude Code session has full context without re-deriving it from scratch.
Read this first, then `strategy.md` and `README.md`.

## What this project is

`strategy.md` is the team's actual strategy (Consensus Arbitrage as the
core decision engine, the Home Turf Clause as a tiebreaker) plus the real
CSFFL league settings baked in. Every decision script sends this file to
Claude as context before deciding anything. `README.md` has setup steps.

## What's built and working right now

- `scripts/espn_client.py`, `rankings.py`, `decide.py`, `log_store.py` --
  a complete pipeline: reads real roster/free-agent data from ESPN,
  computes an arbitrage signal, sends it plus `strategy.md` to Claude,
  logs the decision to `docs/data/log.json`. Only waivers are implemented
  so far (`decide_waiver_move()`).
- `docs/index.html` -- dashboard reading the log, ready for GitHub Pages.
- `.github/workflows/*.yml` -- all three are scheduled correctly (see
  rationale below), but only `waivers.yml` has real logic wired in.
  `lineup.yml` and `draft_queue.yml` currently just echo placeholders.
- `.gitignore` -- excludes `.env` and `espn_state.json`. Don't remove this.

## What's NOT built yet -- the actual remaining work

1. `decide_lineup()` in decide.py -- same pattern as decide_waiver_move(),
   not yet written.
2. `build_draft_queue()` -- not yet written, and time-sensitive: **draft
   is Sep 8, 2026, 6:00 PM PDT.**
3. `browser_actions.py` real selectors -- currently placeholders. Needs
   either live inspection of the user's actual ESPN pages, or screenshots
   / HTML brought into a session to write real ones.
4. Weekly 3-line retro (what worked, what didn't, does strategy.md's
   changelog need an update) -- discussed conceptually, never implemented.
5. Pushing a short "fun update" to the league -- dashboard exists, nothing
   posts to wherever the league actually talks yet (channel not decided).

## Decisions already made -- rationale, so it isn't re-litigated

- Home Turf Clause tie threshold is loose on purpose (same tier, or
  ~15% of projected points) -- meant to fire often, it's a comedic trait,
  not a rare easter egg.
- Waiver period stays at 1 day, deliberately, to match the league's
  existing habit of setting claims Tuesday night for ~midnight processing.
- `waivers.yml` runs daily at 10am UTC -- timed to land safely after
  ESPN's nightly ~3-5am ET processing, since waivers process nightly, not
  weekly (the Wednesday pattern people notice is emergent from the NFL's
  weekly game schedule, not a hardcoded system rule).
- `lineup.yml` runs 4x/week (Thu night, Sun early, Sun late, Mon night)
  because this league's Lineup Protection is off and locks are per-player
  at kickoff, not one Sunday cutoff.
- The arbitrage signal (v1) is a self-contained proxy -- projected points
  vs. percent owned -- rather than a true multi-source comparison, since a
  free second public rankings API wasn't readily available. This is a
  known, documented simplification in `rankings.py`, not an oversight.

## Credential status as of handoff

- Anthropic API key: created.
- ESPN espn_s2 / SWID / league ID / team ID: user was walked through
  getting these via browser DevTools -- confirm `.env` is actually filled
  in before assuming `decide.py` can run.
- GitHub repo: not yet created. GitHub Pages: not yet enabled.

## Suggested first-session priorities

1. Confirm `.env` is complete, run `python scripts/decide.py` for real,
   sanity-check the decision against `strategy.md`.
2. Given the Sep 8 deadline, prioritize `build_draft_queue()` next.
3. Then `decide_lineup()`.
4. Then the weekly retro and the league-update mechanism -- lowest
   urgency, but the part with the most personality payoff.

## Safety notes to preserve

- Keep `DRY_RUN=true` in every workflow until the user explicitly says
  they've watched enough dry runs to trust it.
- Never print, log, or commit the actual secret values.
