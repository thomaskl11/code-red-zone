"""Feeds strategy.md + current league data to Claude and gets back a
decision. This script only decides -- it never touches ESPN directly.
That's browser_actions.py's job, and it only acts for real when
DRY_RUN=false.
"""
import json
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from anthropic import Anthropic

from espn_client import get_league, get_my_team, get_roster_snapshot, get_free_agents
from rankings import rank_by_arbitrage
from log_store import append_entry

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "..", "strategy.md")
DRAFT_QUEUE_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "draft_queue.json")
FANTASYPROS_PATH = os.path.join(os.path.dirname(__file__), "fantasypros_ecr.json")
YAHOO_ADP_PATH = os.path.join(os.path.dirname(__file__), "yahoo_adp.json")

FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def load_strategy():
    with open(STRATEGY_PATH) as f:
        return f.read()


def _normalize_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_external_rankings():
    """Manually-pulled snapshots of two real independent sources -- FantasyPros
    Standard-scoring expert consensus (ECR) and Yahoo's Standard-scoring ADP.
    Neither is a live feed; both are dated snapshots (see each file's
    fetched_at). Returns two name-keyed lookup dicts.
    """
    fp_lookup, yahoo_lookup = {}, {}
    if os.path.exists(FANTASYPROS_PATH):
        with open(FANTASYPROS_PATH) as f:
            fp_data = json.load(f)
        fp_lookup = {_normalize_name(p["name"]): p for p in fp_data["rankings"]}
    if os.path.exists(YAHOO_ADP_PATH):
        with open(YAHOO_ADP_PATH) as f:
            yahoo_data = json.load(f)
        yahoo_lookup = {_normalize_name(p["name"]): p for p in yahoo_data["rankings"]}
    return fp_lookup, yahoo_lookup


def parse_json_response(text):
    """Claude reliably wraps JSON in a ```json fence despite being told not
    to -- strip it before parsing instead of fighting the model on it."""
    text = text.strip()
    match = FENCE_RE.match(text)
    if match:
        text = match.group(1)
    return json.loads(text)


def decide_waiver_move():
    league = get_league()
    team = get_my_team(league)
    roster = get_roster_snapshot(team)
    free_agents = get_free_agents(league, size=60)
    ranked_fas = rank_by_arbitrage(free_agents)[:15]

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    system_prompt = load_strategy() + (
        "\n\nYou are the decision engine described above. Given the current "
        "roster and the top available free agents (each with an arbitrage_gap "
        "score, where higher means more underpriced by the market), decide the "
        "single best waiver move this week, or explicitly decide to make no "
        "move. Respond with ONLY valid JSON, no other text: "
        '{"action": "add_drop" or "no_move", '
        '"add": "player name or null", "drop": "player name or null", '
        '"reasoning": "2-3 sentences, in team voice", '
        '"rule_applied": "arbitrage_gap" or "home_turf_tiebreak" or "none"}'
    )

    user_payload = json.dumps({
        "current_roster": roster,
        "top_free_agents_by_arbitrage": [
            {**p, "arbitrage_gap": round(gap, 1)} for p, gap in ranked_fas
        ],
    })

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_payload}],
    )

    decision = parse_json_response(response.content[0].text)

    if decision["action"] == "add_drop":
        headline = f'Added {decision["add"]}, dropped {decision["drop"]}'
    else:
        headline = "No waiver move this week"

    entry = append_entry(
        kind="waiver",
        headline=headline,
        reasoning=decision["reasoning"],
        meta={"rule_applied": decision.get("rule_applied", "none")},
    )

    return decision, entry


def decide_lineup():
    """Pre-kickoff lineup check. Lineup Protection is off in this league and
    locks are per-player at kickoff, so this needs to actually catch injury
    news and bench/starter value gaps before each wave of games, not just
    once a week.
    """
    league = get_league()
    team = get_my_team(league)
    roster = get_roster_snapshot(team)

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    system_prompt = load_strategy() + (
        "\n\nYou are the decision engine described above, now checking the "
        "starting lineup before kickoff. Given the full roster below (each "
        "player's lineup_slot -- a starting slot name, or 'BE' for bench -- "
        "position, injury_status, and projected_points), decide whether any "
        "bench player should start over a current starter at an eligible "
        "slot. Only recommend a swap when there's a real edge: the current "
        "starter is questionable/doubtful/out, or a bench player at an "
        "eligible position clearly out-projects them. Respond with ONLY "
        "valid JSON, no other text: "
        '{"swaps": [{"start": "player name", "sit": "player name", '
        '"reason": "one line"}], '
        '"reasoning": "2-3 sentences overall, in team voice"}'
        ' If no changes are needed, "swaps" must be an empty list.'
    )

    user_payload = json.dumps({"roster": roster})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_payload}],
    )

    decision = parse_json_response(response.content[0].text)

    if decision["swaps"]:
        headline = "; ".join(f'Start {s["start"]} over {s["sit"]}' for s in decision["swaps"])
    else:
        headline = "No lineup changes needed"

    entry = append_entry(
        kind="lineup",
        headline=headline,
        reasoning=decision["reasoning"],
        meta={"swap_count": len(decision["swaps"])},
    )

    return decision, entry


def build_draft_queue(pool_size=250):
    """Pre-draft only. Snake draft, 10 teams, 14 rounds -- the queue needs
    enough depth to cover the whole draft, not just a top-15 shortlist like
    the waiver move does.
    """
    league = get_league()
    pool = get_free_agents(league, size=pool_size)
    ranked = rank_by_arbitrage(pool)

    fp_lookup, yahoo_lookup = load_external_rankings()

    def with_external_ranks(player):
        key = _normalize_name(player["name"])
        fp = fp_lookup.get(key)
        yahoo = yahoo_lookup.get(key)
        enriched = dict(player)
        if fp:
            enriched["fantasypros_overall_rank"] = fp["rank"]
            enriched["fantasypros_pos_rank"] = fp["pos_rank"]
        if yahoo:
            enriched["yahoo_overall_rank"] = yahoo["rank"]
        return enriched

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    system_prompt = load_strategy() + (
        "\n\nYou are the decision engine described above, now setting a full "
        "snake-draft queue instead of a single move. Given the ranked player "
        "pool below, return an ordered draft queue: the order to draft "
        "players in, best pick first, covering every player you're given. "
        "Each player has an arbitrage_gap score (a proxy: projected points vs. "
        "ESPN ownership -- higher means more underpriced by that proxy). Where "
        "present, a player also has fantasypros_overall_rank (and "
        "fantasypros_pos_rank) and/or yahoo_overall_rank -- these are REAL "
        "independent public sources (FantasyPros Standard expert consensus, "
        "Yahoo Standard ADP), not a proxy. For any player carrying one or both "
        "of those, treat the disagreement between whichever real sources are "
        "present (and, secondarily, the proxy) as the actual Consensus "
        "Arbitrage signal this strategy is built around -- a real rank gap "
        "between sources outweighs the proxy. Only fall back to the "
        "arbitrage_gap proxy alone for players with no external rank data. "
        "Respect the league's single-QB format -- do not queue a second QB "
        "anywhere near the top of the list. When two players are a genuine "
        "tie by this strategy's own tie definition, break it with the Home "
        "Turf Clause. Respond with ONLY valid JSON, no other text: "
        '{"queue": ["player name", ...], '
        '"reasoning": "2-3 sentences, in team voice, on the overall queue shape"}'
    )

    user_payload = json.dumps({
        "ranked_player_pool": [
            with_external_ranks({**p, "arbitrage_gap": round(gap, 1)}) for p, gap in ranked
        ],
    })

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_payload}],
    )

    decision = parse_json_response(response.content[0].text)

    pool_lookup = {p["name"]: p for p, _gap in ranked}
    enriched_queue = []
    for i, name in enumerate(decision["queue"]):
        pool_player = pool_lookup.get(name, {})
        key = _normalize_name(name)
        fp = fp_lookup.get(key)
        yahoo = yahoo_lookup.get(key)
        enriched_queue.append({
            "rank": i + 1,
            "name": name,
            "position": pool_player.get("position"),
            "pro_team": pool_player.get("pro_team"),
            "fantasypros_overall_rank": fp["rank"] if fp else None,
            "fantasypros_pos_rank": fp["pos_rank"] if fp else None,
            "yahoo_overall_rank": yahoo["rank"] if yahoo else None,
        })
    save_draft_queue(enriched_queue, decision["reasoning"])

    entry = append_entry(
        kind="draft",
        headline=f'Draft queue set -- {len(decision["queue"])} players ranked',
        reasoning=decision["reasoning"],
        meta={"queue_size": len(decision["queue"])},
    )

    return decision, entry


def save_draft_queue(enriched_queue, reasoning):
    """Writes the full ranked queue to docs/data/draft_queue.json -- the
    public record docs/draft-queue.html reads from. Separate from
    docs/data/log.json (an append-only history) since this is a single
    current snapshot, overwritten each time the queue is rebuilt.
    """
    sources = {}
    if os.path.exists(FANTASYPROS_PATH):
        with open(FANTASYPROS_PATH) as f:
            fp_data = json.load(f)
        sources["fantasypros"] = {"source": fp_data["source"], "fetched_at": fp_data["fetched_at"]}
    if os.path.exists(YAHOO_ADP_PATH):
        with open(YAHOO_ADP_PATH) as f:
            yahoo_data = json.load(f)
        sources["yahoo"] = {"source": yahoo_data["source"], "fetched_at": yahoo_data["fetched_at"]}

    os.makedirs(os.path.dirname(DRAFT_QUEUE_PATH), exist_ok=True)
    with open(DRAFT_QUEUE_PATH, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reasoning": reasoning,
            "sources": sources,
            "queue": enriched_queue,
        }, f, indent=2)


if __name__ == "__main__":
    import sys

    if "--draft-queue" in sys.argv:
        decision, entry = build_draft_queue()
    elif "--lineup" in sys.argv:
        decision, entry = decide_lineup()
    else:
        decision, entry = decide_waiver_move()
    print(json.dumps(decision, indent=2))
