"""Executes the lineup decision from `decide.py --lineup` on ESPN, with fallbacks.

Run from the lineup workflow after the decision step. Each swap may carry
ranked `alternatives` (backup bench players for a bye starter); if ESPN
won't make the first move, the next one is tried instead of giving up on
that slot. Afterwards the roster is re-read from ESPN and any bye starter
still in the lineup gets one more round of fixes -- the case where a kicker
on a bye stayed in because some other move had failed.
"""
import json
import subprocess
import sys

import browser_actions
import log_store


def _log_failure(swap, errors):
    tried = "; ".join(f'{a["start"]} over {a["sit"]}: {e}' for a, e in errors)
    log_store.append_entry(
        kind="lineup",
        headline="MOVE COULD NOT BE MADE",
        reasoning=(
            f'Tried to start {swap["start"]} over {swap["sit"]}'
            + (f' (and {len(errors) - 1} backup option(s))' if len(errors) > 1 else "")
            + f', but the automation failed: {tried} The decision was made but never '
            "applied on ESPN. If the reason says the player is locked, it can't be "
            "changed anymore; otherwise come make it manually if it matters."
        ),
        meta={
            "execution_failed": True,
            "attempted_start": swap["start"],
            "attempted_sit": swap["sit"],
        },
    )


def run_swaps(swaps, skip_sits=()):
    """Execute swaps, falling back to each swap's alternatives. Returns the
    names of players we failed to bench (and the count of failures)."""
    moved_in, benched, failed_sits = set(), set(skip_sits), set()
    failures = 0
    for swap in swaps:
        errors = []
        for attempt in [swap] + swap.get("alternatives", []):
            if attempt["start"] in moved_in or attempt["sit"] in benched:
                continue  # already used by an earlier swap this run
            try:
                browser_actions.set_lineup(attempt["start"], attempt["sit"])
            except Exception as e:  # noqa: BLE001 -- any ESPN/Playwright failure
                errors.append((attempt, e))
                continue
            moved_in.add(attempt["start"])
            benched.add(attempt["sit"])
            if attempt is not swap:
                log_store.append_entry(
                    kind="lineup",
                    headline=f'Fallback: started {attempt["start"]} over {attempt["sit"]}',
                    reasoning=(
                        f'The planned move ({swap["start"]} over {swap["sit"]}) could not '
                        f'be made on ESPN, so {attempt["start"]} took the slot instead. '
                        f'{swap.get("reason", "")}'
                    ).strip(),
                    meta={"fallback_swap": True},
                )
            break
        else:
            if errors:
                failures += 1
                failed_sits.add(swap["sit"])
                _log_failure(swap, errors)
    return failed_sits, failures


def _remaining_bye_fixes():
    """Fresh read of the roster from ESPN: bye starters that can still be
    replaced. Any problem here is non-fatal -- it's a safety net only."""
    try:
        out = subprocess.run(
            [sys.executable, "decide.py", "--verify-byes"],
            capture_output=True, text=True, timeout=300, check=True,
        ).stdout
        return json.loads(out)["swaps"]
    except Exception as e:  # noqa: BLE001
        print(f"Bye verification skipped: {e}", file=sys.stderr)
        return []


def main(decision_path="/tmp/decision.json"):
    decision = json.load(open(decision_path))
    failed_sits, failures = run_swaps(decision["swaps"])

    # Don't re-attempt a slot that just failed every option it had.
    leftover = [s for s in _remaining_bye_fixes() if s["sit"] not in failed_sits]
    if leftover:
        _, more = run_swaps(leftover)
        failures += more

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main(*sys.argv[1:])
