# Code Red Zone — Project Handoff

An AI-run ESPN fantasy football team ("Code Red Zone") in the Christmas
Sunday Fantasy Football League (CSFFL). This file exists so a fresh
Claude Code session has full context without re-deriving it from scratch.
Read this first, then `strategy.md`.

**Status as of 2026-09-22: fully live.** Draft is done, the site is public
and being shared with the league, both automation pipelines execute real
changes on ESPN, not just dry-run prints. This is not a "getting started"
project anymore — it's a running system with real history. Read the
"lessons learned" section before touching `browser_actions.py`; several
real bugs were found and fixed the hard way and are easy to reintroduce.

## What this project is

`strategy.md` is the team's actual strategy (Consensus Arbitrage as the
core decision engine, the Home Turf Clause as a tiebreaker) plus the real
CSFFL league settings. Every decision script sends this file to Claude as
system-prompt context before deciding anything.

Public site: **https://thomaskl11.github.io/code-red-zone/** (Season Log,
Strategy, Draft Queue pages). Repo: **github.com/thomaskl11/code-red-zone**.

## What's built and live right now

- `scripts/decide.py` — `decide_waiver_move()`, `decide_lineup()`, and
  `build_draft_queue()` all exist and work. Each sends real ESPN data +
  `strategy.md` to Claude and logs the decision.
- `scripts/browser_actions.py` — `set_lineup()`, `submit_waiver_claim()`,
  and `move_player_to_ir()` all have **real, live-verified selectors**,
  confirmed by actually executing against the real roster/waiver wire and
  checking the result via `league.transactions()` / roster snapshots
  afterward, not just by eyeballing the DRY_RUN print. `set_draft_queue()`
  is intentionally **not implemented** — the user enters the draft queue
  into ESPN by hand from `docs/draft-queue.html`, no automation needed.
- Both `.github/workflows/lineup.yml` and `waivers.yml` run with
  `DRY_RUN: "false"` — they execute for real, not just decide.
  `draft_queue.yml` is manual-dispatch-only (the draft is over; it's kept
  around in case a future season wants it, not currently scheduled).
- `docs/index.html` — Season Log dashboard: category totals, a card per
  kind showing the latest move, a collapsible filterable full history.
  Failed executions render as a distinct red "⚠ alert" card (see below).
- `docs/strategy.html`, `docs/draft-queue.html` — the other two pages.
- `scripts/protected_players.json` — ESPN's real Undroppables list
  (periodically-refreshed snapshot) plus room for personal additions.
  Hard-enforced in code, not just requested of the model.
- `scripts/lineup_state.json` — persisted injury-status snapshot so
  `decide_lineup()` doesn't re-litigate a settled call without new info.

## Real safeguards built (all hard-enforced in code, not just prompted)

1. **Protected-player veto** — never drops anyone on
   `protected_players.json`, full stop, checked after the model responds.
2. **Already-rostered veto** — never "adds" a player already on our own
   roster (the model did this once, confusing `add` with `ir_move`).
3. **Pending-claim guard** — via `league.transactions()`, skips the week
   entirely if a prior WAIVER transaction isn't `EXECUTED` yet, so it
   never has to rank its own claims against each other.
4. **Lineup anti-churn gate** — a call only reopens when a player's
   `injury_status` actually changed since the last check; rank/ownership
   noise alone doesn't reopen a settled decision. Proven in ~2 weeks of
   real production use: mostly clean skips, only reopening on genuine news.
5. **Dashboard failure alerting** — a failed execution (expired session,
   selector break, anything) gets caught, logged as a visible red
   "MOVE COULD NOT BE MADE" entry with what it tried and why, and the
   commit-to-dashboard step runs `if: always()` so it publishes even
   though the job still exits non-zero for GitHub-side visibility too.

## Lessons learned the hard way — read before editing browser_actions.py

- **Always `git fetch` before trusting local git state.** `git status`
  saying "up to date" only reflects the last actual fetch, not the live
  remote. Got badly confused twice this way — once nearly re-litigating
  "lost" log entries that were just sitting on the remote unfetched.
- **D/ST names contain `/`, which breaks Playwright regex-based
  selectors.** Playwright serializes a Python `re.Pattern` into a JS
  regex literal (`/pattern/flags`) for the browser side and doesn't
  escape internal `/`. `re.escape()` doesn't help — `/` isn't special in
  Python regex, only in that wire format. Every function in
  `browser_actions.py` now uses plain-string substring matching (
  Playwright's default for a bare string) instead of regex, specifically
  to avoid this whole class of bug, not just patch one instance.
- **`Locator.count()` does not wait.** Unlike `.click()`, it checks the
  DOM *right now* — using it to decide between two possible buttons
  (e.g. "Add" vs "Claim") races ahead of the page actually rendering
  results and can guess wrong. Use a real `.click(timeout=...)` +
  `except` fallback instead, which properly waits.
- **The player-search box on the Add/Claim page needs an explicit Enter
  to submit.** Typing alone (even verified via `input_value()`) does not
  filter the results list — found this by adding temporary debug
  screenshots to the live workflow (via `actions/upload-artifact`) and
  literally looking at what rendered. If something in that flow breaks
  again, that debug-artifact technique is the fastest way back to ground
  truth — don't just keep guessing and retriggering blind.
- **The model doesn't reliably output pure JSON.** `parse_json_response()`
  in `decide.py` tries bare JSON, then a fenced block found *anywhere* in
  the text (not anchored to the whole string), then a last-resort
  first-`{`-to-last-`}` scan. This was needed because the model sometimes
  reasons in prose *before* a properly-fenced JSON block on a genuinely
  close call — don't revert to a simpler anchored-regex parser.
- **ESPN's own "IR Eligible" list is broader than we want** — it includes
  anyone merely `OUT` this week, not just true long-term IR. Our code
  only ever acts on `injury_status == "INJURY_RESERVE"` specifically;
  don't loosen that without deliberately deciding to.
- **`dict.get(key, default)` doesn't help against an explicit `null`** —
  only against a *missing* key. The model reliably includes
  `"drop": null` explicitly, which crashed a `.get("drop", "")` check
  once. Use `decision.get("drop") or ""` instead.

## Known unverified / open items

- **`submit_waiver_claim(add_name, drop_name=None)`'s dropless path is
  still unverified live.** The one real confirmed transaction (Vikings
  D/ST for Jonathon Brooks) used an explicit drop. The "Confirm add"
  (without "and drop X") button text when no drop is selected has never
  actually been observed — confirm before trusting it fully.
- **`open_roster_spots` (recognizing a pre-existing empty non-IR slot,
  not just one freed this turn) was just added and hasn't been live-
  observed picking the dropless path yet** — a pending claim was blocking
  further decisions at the time it was built. Worth checking the next
  real waiver run for a `"drop": null` decision when a spot is open.
- **ESPN session (`ESPN_STORAGE_STATE`) expires roughly every 1-2 weeks**
  (the Disney-side `dtcAuth` cookie is the binding constraint; two real
  data points: 7 days and 13 days). No proactive alert exists for this
  specifically beyond the general "MOVE COULD NOT BE MADE" dashboard
  entry a failed run produces. To regenerate: `playwright codegen
  --save-storage=espn_state.json https://fantasy.espn.com`, log in, close
  the window, then trim to just espn.com-domain cookies (GitHub secrets
  cap at 48KB, a raw dump is ~500KB) before setting `ESPN_STORAGE_STATE`.
- **Bye weeks** — user flagged that once byes start, lineup logic needs
  to think more holistically (a whole week's slate, not one slot at a
  time). Not designed or built yet, deliberately deferred.
- **Weekly 3-line retro** (what worked, what didn't, does `strategy.md`'s
  changelog need updating) — discussed conceptually, never implemented.
- **Posting a "fun update" to the league** — dashboard exists and is
  being shared directly; nothing auto-posts elsewhere, and that's
  probably fine as-is unless the user asks for it.

## Decisions already made — rationale, so it isn't re-litigated

- Home Turf Clause tie threshold is loose on purpose (same tier, or
  ~15% of projected points) — meant to fire often, it's a comedic trait,
  not a rare easter egg. Team voice is friendly/nerdy, not villain-edge
  (deliberately revised from an earlier draft).
- Waiver checks: 2x/week (Tue, Fri), not daily — daily rarely found
  anything real; Tuesday catches post-Sunday/MNF fallout, Friday catches
  Wed/Thu injury news before the weekend.
- Lineup checks: 7x/week, timed around actual kickoff windows including
  international games (~7am ET Sunday for London-style early kickoffs)
  and a dedicated Sunday Night Football check — the original 4x/week
  schedule had real gaps here.
- The waiver arbitrage pool is grouped **by position** (top 4 each) before
  ranking, not a flat top-15 overall — a flat ranking let QBs dominate
  purely by scoring the most raw points, regardless of actual roster need
  (this is a single-QB league with a locked-in starter).
- `rankings.py`'s arbitrage signal is still a self-contained proxy
  (projected points vs. percent owned) for the *recurring* waiver/lineup
  checks. The one-time draft queue build used real FantasyPros + Yahoo
  data as genuine second/third sources — that rigor was never extended to
  the ongoing weekly checks. Would be a real, scoped improvement if the
  user wants it later.

## Credential / infra status

- Repo, Pages, all 6 GitHub secrets: live and set.
- `.env` locally filled in and working.
- `.gitignore` excludes `.env` and `espn_state.json` — don't remove this.

## Suggested next priorities (nothing urgent)

1. Verify `submit_waiver_claim`'s dropless path and `open_roster_spots`
   logic on a real week when both are actually exercised.
2. Bye-week holistic lineup handling, whenever byes start mattering.
3. The weekly retro mechanism, if the user still wants it.
4. Consider extending the FantasyPros/Yahoo real-source approach (used
   for the draft queue) to the recurring waiver checks too, if the user
   wants sharper-than-proxy signal on an ongoing basis.

## Safety notes to preserve

- Never print, log, or commit the actual secret values.
- Both workflows are live (`DRY_RUN: "false"`) — a future change to
  `browser_actions.py` executes for real on the next scheduled run, not
  just in a sandbox. Test locally / verify carefully before pushing
  anything that touches the execution path.
