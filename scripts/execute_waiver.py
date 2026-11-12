"""Executes the waiver decision from `decide.py` on ESPN, with a fallback.

If the IR move can't be made (ESPN greys out the button -- e.g. the IR slot
is already full), the old behavior was to give up on the whole claim. Now it
asks `decide.py --no-ir` to re-plan the same week without IR (an add with a
real drop, or no move) and executes that instead. Failures are still logged
as red alerts on the dashboard.
"""
import json
import subprocess
import sys

import browser_actions
import log_store


def _replan_without_ir():
    out = subprocess.run(
        [sys.executable, "decide.py", "--no-ir"],
        capture_output=True, text=True, timeout=600, check=True,
    ).stdout
    return json.loads(out)


def _add(decision):
    """Execute the add (with its drop, if any). Returns True on success."""
    drop_target = None if decision.get("ir_move") else decision.get("drop")
    try:
        browser_actions.submit_waiver_claim(decision["add"], drop_target)
        return True
    except Exception as e:  # noqa: BLE001 -- any ESPN/Playwright failure
        detail = f", dropping {drop_target}" if drop_target else ""
        log_store.append_entry(
            kind="waiver",
            headline="MOVE COULD NOT BE MADE",
            reasoning=(
                f'Tried to add {decision["add"]}{detail}, but the automation failed: '
                f"{e}. Nothing changed on ESPN -- come make this manually if "
                "it matters, or check whether the ESPN session needs "
                "refreshing."
            ),
            meta={
                "execution_failed": True,
                "attempted_add": decision["add"],
                "attempted_drop": drop_target,
            },
        )
        return False


def main(decision_path="/tmp/decision.json"):
    d = json.load(open(decision_path))
    ok = True

    if d.get("ir_move"):
        try:
            browser_actions.move_player_to_ir(d["ir_move"])
        except Exception as e:  # noqa: BLE001
            log_store.append_entry(
                kind="waiver",
                headline="IR move didn't work -- re-planning without IR",
                reasoning=(
                    f'Tried to move {d["ir_move"]} to IR to free a roster spot, but the '
                    f"automation failed: {e}. Nothing changed on ESPN. Asking for a "
                    "different plan that doesn't need IR."
                ),
                meta={"ir_move_failed": d["ir_move"]},
            )
            try:
                d = _replan_without_ir()
            except Exception as e2:  # noqa: BLE001
                log_store.append_entry(
                    kind="waiver",
                    headline="MOVE COULD NOT BE MADE",
                    reasoning=(
                        f'The IR move for {d["ir_move"]} failed and re-planning without '
                        f"it also failed ({str(e2)[:200]}). Nothing changed on ESPN -- "
                        "come make this manually if it matters."
                    ),
                    meta={"execution_failed": True, "attempted_ir_move": d["ir_move"]},
                )
                sys.exit(1)

    if d.get("action") == "add_drop":
        ok = _add(d)

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main(*sys.argv[1:])
