# Code Red Zone — CSFFL automation

An AI-run fantasy football team. `strategy.md` is the team's actual
strategy (Consensus Arbitrage + the Home Turf Clause) -- every decision
script reads it before making a call, and the weekly retro is the only
thing allowed to edit it.

## What's actually working vs. what's a skeleton

**Working, testable locally right now:**
- `scripts/espn_client.py` -- reads your real roster and free agents
- `scripts/rankings.py` -- computes the arbitrage signal
- `scripts/decide.py` -- sends strategy + data to Claude, gets a decision back, logs it
- `docs/index.html` -- reads `docs/data/log.json` and renders the dashboard

**Skeleton, needs your input to finish:**
- `scripts/browser_actions.py` -- the actual ESPN clicks. The selectors are
  placeholders because I can't see your league's live page. Either fill
  them in yourself (inspect element on the button you need) or bring the
  page HTML to a follow-up session and we'll write them together.
- `lineup.yml` / `draft_queue.yml` decision logic -- same pattern as
  `decide_waiver_move()`, not yet written since it's the same shape of work.

## Setup, in order

1. **Get an Anthropic API key** at console.anthropic.com (separate from any
   Claude.ai subscription -- pay-per-use, cheap for this volume).

2. **Get your ESPN session cookies.** Log into fantasy.espn.com, open
   DevTools > Application > Cookies > fantasy.espn.com, and copy the
   `espn_s2` and `SWID` values.

3. **Find your league ID and team ID.** Both are in your league's URL,
   e.g. `.../leagueId=123456/teamId=4`.

4. **Test locally before anything touches GitHub:**
   ```
   pip install -r requirements.txt
   cp .env.example .env   # fill in the values above
   cd scripts
   python decide.py
   ```
   This should print a real decision based on your actual roster. Nothing
   gets written to ESPN -- `decide.py` only decides.

5. **Generate the browser session state** (only needed once you're ready
   to test real execution, not for step 4):
   ```
   playwright codegen --save-storage=espn_state.json https://fantasy.espn.com
   ```
   Log in during the recording, then close the browser. Base64-encode the
   file (`base64 -i espn_state.json`) -- you'll paste this into a secret.

6. **Create the GitHub repo, push this code, and add secrets** under
   Settings > Secrets and variables > Actions: `ESPN_S2`, `ESPN_SWID`,
   `ESPN_LEAGUE_ID`, `ESPN_TEAM_ID`, `ANTHROPIC_API_KEY`,
   `ESPN_STORAGE_STATE`.

7. **Enable GitHub Pages** under Settings > Pages, source: `/docs` on
   your main branch. Public repo required on the free plan.

8. **Leave `DRY_RUN: "true"` in the workflows** for the first couple of
   weeks. You'll see exactly what it would have done in the Actions logs
   without anything actually happening on ESPN. Flip to `"false"` once
   you trust it.

## Fallback

Every workflow has `workflow_dispatch` enabled, which adds a manual
"Run workflow" button in the Actions tab -- your kill switch and your
backup if a scheduled run ever fails silently.
