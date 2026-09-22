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

from playwright.sync_api import sync_playwright

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"


def _load_storage_state():
    encoded = os.environ["ESPN_STORAGE_STATE"]
    path = "/tmp/espn_state.json"
    with open(path, "wb") as f:
        f.write(base64.b64decode(encoded))
    return path


def submit_waiver_claim(add_name: str, drop_name: str = None):
    """Confirmed live against the real Players > Add page (2026-09-12). This
    league uses waiver priority, not instant free-agent adds, so the button
    reads "Add" for true free agents and "Claim" for players on waivers --
    matched here with either verb since decide.py doesn't know in advance
    which one it'll be. The flow: search -> click Add/Claim -> a panel opens
    where you must pick a player to conditionally drop -> Continue -> a
    final Confirm Transaction modal. The Confirm button's own aria-label
    only ever says "add" (even for a Claim) and never names the add target,
    just the drop -- matched on that basis. This submits a conditional
    waiver claim, not an instant transaction; ESPN processes it later
    according to this league's waiver priority.

    drop_name=None is for when a roster spot was already freed another way
    (e.g. move_player_to_ir() ran first) -- *** UNVERIFIED ***: this skips
    the Drop Player click on the assumption the panel lets you Continue with
    an open spot and no drop selected, and that Confirm's aria-label drops
    the "and drop X" clause entirely when there's nothing to drop. Neither
    of those has actually been seen live. Confirm against the real page
    before trusting this branch with DRY_RUN off.

    Uses plain-string substring matching throughout, not regex, on purpose:
    caught live 2026-09-22 trying to add "Vikings D/ST" -- the "/" broke
    Playwright's serialization of a Python regex into a JS regex literal on
    the browser side (unescaped "/" prematurely closes the literal). D/ST
    names all contain that character, so this isn't an edge case. Plain
    strings sidestep the whole class of special-character bugs, not just
    this one.
    """
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
            f"https://fantasy.espn.com/football/players/add?leagueId={league_id}&teamId={team_id}"
        )

        page.get_by_placeholder("Player Name").fill(add_name)
        page.wait_for_timeout(2000)  # DEBUG: let any live-filter settle before capturing
        page.screenshot(path="/tmp/debug_search.png")
        with open("/tmp/debug_search.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        # count() doesn't wait for the live-filtered list to actually
        # render after fill() -- caught live 2026-09-22, it raced ahead and
        # guessed wrong. click()'s real auto-wait (with a bounded timeout on
        # the first attempt) is what actually needs to wait for the row.
        try:
            page.get_by_role("button", name=f"Add {add_name}").click(timeout=8000)
        except Exception:
            page.get_by_role("button", name=f"Claim {add_name}").click()

        if drop_name:
            page.get_by_role("button", name=f"Drop Player {drop_name}").click()

        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Confirm add").click()

        page.screenshot(path="/tmp/waiver_confirmation.png")
        browser.close()


def move_player_to_ir(player_name: str):
    """Confirmed live against the real roster page (2026-09-22). Separate
    mechanism from set_lineup()'s MOVE/HERE -- there's a dedicated "IR"
    toolbar button (next to Add/Drop, identified by the attribute
    data-myteam-mode="manageir" rather than its visible text, since "IR" as
    plain text also appears elsewhere on the page as the slot label) that
    opens a "Manage IR" panel. In that panel, each IR-eligible bench player
    has a "TO IR" button (aria-label "{name} to Injured Reserve"); clicking
    it applies immediately, no separate confirm step, same as MOVE/HERE.

    Note ESPN's own "IR Eligible" list is broader than we actually want --
    it includes anyone merely OUT this week, not just true INJURY_RESERVE
    players. That filtering already happens in decide.py before this
    function is ever called; this function trusts whatever name it's given.
    """
    if DRY_RUN:
        print(f"[DRY RUN] Would move {player_name} to IR")
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

        page.locator('[data-myteam-mode="manageir"]').click()
        page.get_by_role("button", name=f"{player_name} to Injured Reserve").click()

        page.screenshot(path="/tmp/ir_confirmation.png")
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
        page.get_by_role("button", name=f"Confirm move of {swap_in}").click()

        page.screenshot(path="/tmp/lineup_confirmation.png")
        browser.close()


def set_draft_queue(ordered_player_names: list):
    if DRY_RUN:
        print(f"[DRY RUN] Would set draft queue to: {ordered_player_names}")
        return
    # TODO -- the draft queue is drag-and-drop in ESPN's UI. Playwright's
    # drag_to() can do this, but needs real selectors to test against.
    raise NotImplementedError("Fill in once we've confirmed the draft queue page selectors.")
