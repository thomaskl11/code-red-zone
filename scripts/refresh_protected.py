"""Refreshes scripts/protected_players.json from ESPN's live Undroppables page.

The list changes through the season (injuries, trades), and the old copy was
a hand-pulled Sept 9 snapshot, so players who'd since come off it stayed
protected. The page renders client-side, so this loads it in headless
Chromium (already installed for the browser actions) and parses the visible
text. It's a public page: no login needed.

Safe by construction: if the page doesn't parse into a plausible list, the
existing file is left untouched, so a layout change or outage can never
leave the protection list empty. `extra_names` (personal must-keeps) is
always preserved.
"""
import json
import os
import re
import sys
from datetime import date

from playwright.sync_api import sync_playwright

URL = "https://fantasy.espn.com/football/players/undroppables"
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_players.json")
POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}
MIN_PLAUSIBLE = 5  # the real list has been ~13-20 names all season


def parse_undroppables(text):
    """Names from the page text. Each player is three lines: name, team, position."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if "Current Undroppables" in lines:
        lines = lines[lines.index("Current Undroppables") + 1:]
    names = []
    for i in range(len(lines) - 2):
        team, pos = lines[i + 1], lines[i + 2]
        if pos in POSITIONS and re.fullmatch(r"[A-Z]{2,4}", team):
            names.append(lines[i])
    return names


def fetch_names():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(URL, timeout=60000)
            page.get_by_text("Current Undroppables").wait_for(timeout=30000)
            page.wait_for_timeout(2000)  # let the list itself finish rendering
            return parse_undroppables(page.inner_text("body"))
        finally:
            browser.close()


def main():
    names = fetch_names()
    if len(names) < MIN_PLAUSIBLE:
        print(f"Only parsed {len(names)} names; keeping the existing list.", file=sys.stderr)
        sys.exit(1)

    data = json.load(open(PATH, encoding="utf-8"))
    old = set(data.get("espn_undroppables", []))
    data["espn_undroppables"] = names
    data["fetched_at"] = date.today().isoformat()
    data["source"] = URL
    data["note"] = (
        "ESPN's own anti-collusion/anti-tanking list. Refreshed automatically from the "
        "live page before each waiver check (scripts/refresh_protected.py); if that "
        "refresh fails the previous copy stays in place. Add any personal must-protects "
        "to extra_names below -- those are never dropped regardless of ESPN's list."
    )
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    added, removed = sorted(set(names) - old), sorted(old - set(names))
    print(f"Undroppables refreshed: {len(names)} players. Added: {added or 'none'}. Removed: {removed or 'none'}.")


if __name__ == "__main__":
    main()
