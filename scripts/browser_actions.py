"""Executes whatever decide.py decided, by driving a real logged-in browser
session against fantasy.espn.com. ESPN has no supported write API, so this
is the execution layer.

*** THIS FILE IS A SKELETON, NOT A FINISHED SCRIPT. ***
The exact selectors for the waiver-claim, lineup-swap, and draft-queue
screens depend on ESPN's current markup, which I can't inspect without
either your league's live HTML or a shared browser session. Fill in the
TODOs below yourself (open your league, right-click the element you need,
Inspect, copy a stable selector), or bring the page HTML to a follow-up
session and we'll write the exact selectors together.

Auth pattern: rather than scripting ESPN's own login (fragile, and can trip
bot detection in headless CI), log in ONCE locally with Playwright, save
the authenticated session, and reuse it every run:

    playwright codegen --save-storage=espn_state.json https://fantasy.espn.com

Then TRIM it before storing -- a real codegen session dumps ~500KB+ of
every cookie from every ad-tech domain you brushed past, plus full
localStorage, and GitHub Actions secrets cap out at 48KB. Only the
espn.com-domain cookies actually matter for auth; localStorage isn't
needed at all:

    import json
    d = json.load(open("espn_state.json", encoding="utf-8"))
    filtered = {"cookies": [c for c in d["cookies"] if "espn.com" in c["domain"]], "origins": []}
    json.dump(filtered, open("espn_state_trimmed.json", "w"))

Base64-encode THAT (should land around 15-20KB) and store the result as
the ESPN_STORAGE_STATE GitHub secret. This script decodes it back to a
file at runtime, so the Action never needs to see your actual password.

One real limitation: the Disney-side auth cookie (dtcAuth) is short-lived
(~1 week from generation, confirmed 2026-09-09), unlike SWID/espn_s2
(~1 year). Expect to redo this whole capture-and-trim dance roughly
weekly -- there's no way around a real login session eventually expiring.
"""
import base64
import os
import re

from playwright.sync_api import sync_playwright

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def _load_storage_state():
    encoded = os.environ["ESPN_STORAGE_STATE"]
    path = "/tmp/espn_state.json"
    with open(path, "wb") as f:
        f.write(base64.b64decode(encoded))
    return path


def submit_waiver_claim(add_name: str, drop_name: str):
    if DRY_RUN:
        print(f"[DRY RUN] Would submit waiver: add {add_name}, drop {drop_name}")
        return

    league_id = os.environ["ESPN_LEAGUE_ID"]
    team_id = os.environ["ESPN_TEAM_ID"]
    storage_state = _load_storage_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        page.goto(
            f"https://fantasy.espn.com/football/team?leagueId={league_id}&teamId={team_id}"
        )

        # TODO -- replace every line below with real selectors from your league.
        page.get_by_placeholder("Search Players").fill(add_name)
        page.get_by_text(add_name).first.click()
        page.get_by_role("button", name="Add").click()
        page.get_by_text(drop_name).first.click()
        page.get_by_role("button", name="Submit").click()

        page.screenshot(path="/tmp/waiver_confirmation.png")
        browser.close()


def set_lineup(swap_in: str, swap_out: str):
    """Confirmed live against the real roster page (2026-09-09): each row has
    a "MOVE" button (aria-label "Select {name} to move"); clicking it turns
    every eligible destination row's button into "HERE" (aria-label "Confirm
    move of {name} to {slot name}" -- the slot name varies, so match loosely
    on the leading "Confirm move of {name}" instead of the full label). The
    swap applies immediately on the HERE click -- no separate Save step.
    """
    if DRY_RUN:
        print(f"[DRY RUN] Would start {swap_in} over {swap_out}")
        return

    league_id = os.environ["ESPN_LEAGUE_ID"]
    team_id = os.environ["ESPN_TEAM_ID"]
    storage_state = _load_storage_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        page.goto(
            f"https://fantasy.espn.com/football/team?leagueId={league_id}&teamId={team_id}"
        )

        page.get_by_role("button", name=f"Select {swap_out} to move").click()
        page.get_by_role(
            "button", name=re.compile(f"^Confirm move of {re.escape(swap_in)} to")
        ).click()

        page.screenshot(path="/tmp/lineup_confirmation.png")
        browser.close()


def set_draft_queue(ordered_player_names: list):
    if DRY_RUN:
        print(f"[DRY RUN] Would set draft queue to: {ordered_player_names}")
        return
    # TODO -- the draft queue is drag-and-drop in ESPN's UI. Playwright's
    # drag_to() can do this, but needs real selectors to test against.
    raise NotImplementedError("Fill in once we've confirmed the draft queue page selectors.")
