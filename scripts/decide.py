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
PROTECTED_PLAYERS_PATH = os.path.join(os.path.dirname(__file__), "protected_players.json")
LINEUP_STATE_PATH = os.path.join(os.path.dirname(__file__), "lineup_state.json")

FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


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


def load_protected_players():
    """Players that never get dropped, full stop -- enforced in code below,
    not just asked of the model. Combines ESPN's own Undroppables list (a
    periodically-refreshed snapshot, see protected_players.json's fetched_at)
    with any personal must-protects in that file's extra_names. Returns a
    set of normalized names.
    """
    if not os.path.exists(PROTECTED_PLAYERS_PATH):
        return set()
    with open(PROTECTED_PLAYERS_PATH) as f:
        data = json.load(f)
    names = data.get("espn_undroppables", []) + data.get("extra_names", [])
    return {_normalize_name(n) for n in names}


def parse_json_response(text):
    """Claude doesn't reliably respect "ONLY valid JSON, no other text" --
    sometimes a ```json fence with nothing else, sometimes plain JSON,
    sometimes (caught live 2026-09-22, on a genuinely close call) a chunk
    of prose reasoning *before* a properly fenced block. Try each in order
    of how permissive it is, instead of assuming any one shape.
    """
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = FENCE_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Last resort: something got embedded in surrounding prose with no
    # fence at all -- grab from the first { to the last } and hope it's
    # balanced. Raises naturally if this still isn't valid JSON.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise ValueError(f"Could not find JSON in model response: {text[:200]!r}")


JSON_ONLY_SUFFIX = (
    "\n\nBegin your response with the opening { of the JSON object. Do all "
    "of your reasoning inside the JSON's \"reasoning\" field -- do not write "
    "any analysis, headings, or bullet points before it."
)


def call_for_json(client, system_prompt, user_payload, max_tokens=2000, attempts=2):
    """One model call that has to come back as parseable JSON.

    Found 2026-10-07: decide_waiver_move() capped output at 500 tokens, and
    the model likes to think out loud in prose first on a close call -- in a
    5-run local test it hit stop_reason=max_tokens twice, both before it ever
    wrote any JSON, so there was nothing for parse_json_response() to find
    (a smarter parser can't recover JSON that was never written). That was
    a ~40% failure rate on waiver checks, and it's what killed the Sept 29
    run. Gives real headroom (cost is only for tokens actually used), asks
    for JSON-first, and retries once since the output is nondeterministic.
    """
    last_err = None
    for _ in range(attempts):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system_prompt + JSON_ONLY_SUFFIX,
            messages=[{"role": "user", "content": user_payload}],
        )
        try:
            return parse_json_response(response.content[0].text)
        except ValueError as e:  # JSONDecodeError is a ValueError subclass
            last_err = e
    raise last_err


def _has_pending_waiver_claim(league, team):
    """True if this team has any WAIVER transaction that isn't the one
    confirmed-terminal status. Deliberately conservative: an unrecognized
    status (including a real pending one we haven't seen an example of yet)
    blocks a new claim rather than risking two claims stacked at once,
    which would force deciding a priority order between our own claims --
    exactly the situation we're avoiding.
    """
    try:
        transactions = league.transactions(types={"WAIVER"})
    except Exception as e:
        # espn_api raises this when ESPN omits the 'transactions' key, which
        # happens right after the scoring period rolls over (Tuesday, after
        # MNF) before anything has been processed in the new week. That means
        # "no transactions", not a failure -- anything else still propagates.
        if "No transactions found" not in str(e):
            raise
        return False
    for t in transactions:
        if t.team.team_name == team.team_name and t.status != "EXECUTED":
            return True
    return False


def _cleanup_drop_candidates(roster, protected):
    """The drops the cleanup order allows right now, as (reason, [names]); the
    first tier that has anyone is the ONLY acceptable drop (the user's rule:
    dead weight goes before anything else, enforced in code because the
    prompt alone was overridden -- the model dropped Loveland while a spare
    kicker sat on the bench).

    1. Players with no NFL team -- they can't score.
    2. A surplus K or D/ST. Never carry two of either. Keep the better one
       (points per game); if the one we'd keep is on a bye this week the spare
       is still covering it, so hold both until that week passes.
    Returns (None, []) when neither applies, leaving the choice to the model.
    """
    def droppable(p):
        return (
            p["lineup_slot"] != "IR"
            and not p.get("locked")
            and _normalize_name(p["name"]) not in protected
        )

    teamless = [p["name"] for p in roster if p["no_nfl_team"] and droppable(p)]
    if teamless:
        return "a player with no NFL team", teamless

    surplus = []
    for pos in ("K", "D/ST"):
        group = [p for p in roster if p["position"] == pos and p["lineup_slot"] != "IR"]
        if len(group) < 2:
            continue
        keeper = max(group, key=lambda p: p["avg_points_per_game"] or 0)
        if keeper["on_bye"]:
            continue  # the spare is covering the keeper's bye this week
        surplus += [p["name"] for p in group if p is not keeper and droppable(p)]
    if surplus:
        return "a surplus kicker or D/ST (never carry two)", surplus
    return None, []


def _waiver_problem(decision, roster, protected, ir_slot_open, fa_positions=None):
    """Why a proposed waiver move can't happen, or None if it can. Caught
    here so the model can be re-asked for a workable alternative, instead of
    the move dying at execution (ESPN greys out the button -- the Nov 3 and
    Nov 6 failures: IR slot already full; and a drop of a player whose game
    already started isn't allowed at all)."""
    if decision.get("action") != "add_drop":
        return None
    if not decision.get("add"):
        return (
            "an add_drop needs a player to add -- the system cannot drop someone "
            "without adding someone, so pair any needed drop with the best "
            "workable add for a real need, or choose no_move"
        )
    by_name = {_normalize_name(p["name"]): p for p in roster}
    if _normalize_name(decision.get("add") or "") in by_name:
        return f'{decision["add"]} is already on our roster'
    # Never carry two kickers or two D/STs. The waiver check runs before the
    # lineup swap, so a benched K/D-ST covering a bye still shows as "BE" --
    # the model once tried to add a third kicker while Boswell sat on the
    # bench covering Lutz's bye. If we already have a usable one (healthy, not
    # on a bye this week), adding another is never needed.
    add_pos = (fa_positions or {}).get(_normalize_name(decision.get("add") or ""))
    if add_pos in ("K", "D/ST"):
        usable = [
            p["name"] for p in roster
            if p["position"] == add_pos and p["lineup_slot"] != "IR"
            and not p["on_bye"] and not p["no_nfl_team"]
            and p["injury_status"] not in ("OUT", "INJURY_RESERVE")
        ]
        if usable:
            return (
                f'we already have a usable {add_pos} ({usable[0]}, healthy and not on a '
                f"bye this week); only add a {add_pos} when every one we have is on a bye or out"
            )
    ir = decision.get("ir_move")
    if ir:
        p = by_name.get(_normalize_name(ir))
        if p is None:
            return f"{ir} is not on our roster"
        if p["lineup_slot"] == "IR":
            return f"{ir} is already in the IR slot"
        if not ir_slot_open:
            return "the IR slot is already occupied, so nobody can be moved to IR"
        if p["injury_status"] != "INJURY_RESERVE":
            return f"{ir} is not INJURY_RESERVE status, so is not IR-eligible"
        return None
    drop = decision.get("drop")
    if drop:
        p = by_name.get(_normalize_name(drop))
        if p is None:
            return f"{drop} is not on our roster"
        if _normalize_name(drop) in protected:
            return f"{drop} is on the protected list and can never be dropped"
        if p.get("locked"):
            return f"{drop}'s game already started, so he is locked and cannot be dropped"
        reason, allowed = _cleanup_drop_candidates(roster, protected)
        if allowed and _normalize_name(drop) not in {_normalize_name(n) for n in allowed}:
            return (
                f"the cleanup order requires the drop to be {reason} first: "
                f'{", ".join(allowed)}. Pair the add with one of those instead of {drop}'
            )
    return None


def decide_waiver_move(no_ir=False):
    league = get_league()
    team = get_my_team(league)
    roster = get_roster_snapshot(team, league.current_week)

    if _has_pending_waiver_claim(league, team):
        entry = append_entry(
            kind="waiver",
            headline="Skipped -- a previous claim is still pending",
            reasoning=(
                "At least one of our WAIVER transactions isn't showing as EXECUTED yet. "
                "Holding off on a new claim rather than risk stacking two at once, which "
                "would mean deciding a priority order between our own pending claims -- "
                "not something we want this making up on its own."
            ),
            meta={"rule_applied": "pending_claim_hold"},
        )
        return {"action": "no_move", "add": None, "drop": None}, entry

    # Widened pool (60 -> 100) and grouped by position before ranking. The
    # raw arbitrage_gap formula naturally favors QBs -- they score the most
    # points relative to ownership -- so a flat top-15-overall list was
    # QB-dominated regardless of whether the roster actually needed one.
    # Slicing per position first means every position gets a fair shot at
    # being considered, and the model reasons about roster need on top of
    # that instead of just chasing the single highest number.
    free_agents = get_free_agents(league, size=100)
    ranked_all = rank_by_arbitrage(free_agents)
    by_position = {}
    for player, gap in ranked_all:
        by_position.setdefault(player["position"], []).append((player, gap))
    ranked_fas = []
    for players in by_position.values():
        ranked_fas.extend(players[:4])
    ranked_fas.sort(key=lambda pg: pg[1], reverse=True)

    protected = load_protected_players()

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    system_prompt = load_strategy() + (
        "\n\nYou are the decision engine described above. Given the current "
        "roster and the top available free agents per position (each with an "
        "arbitrage_gap score, where higher means more underpriced by the "
        "market), decide the single best waiver move this week, or "
        "explicitly decide to make no move. Reason like a real manager: "
        "first look at the roster for actual positions of need (an injured "
        "or unreliable starter with no strong healthy bench option behind "
        "them, or a position that's thin), then look at the top targets for "
        "those specific needy positions -- not just whichever position has "
        "the single highest arbitrage_gap overall. QBs will almost always "
        "post the highest raw gap since they score the most points, but "
        "that alone doesn't make QB a need in a single-QB league with a "
        "locked-in starter -- don't let the biggest number override actual "
        "roster need. That said, don't default to inertia either: a real, "
        "solid upgrade at a genuine position of need is worth making even "
        "when it's not an overwhelming slam-dunk -- lean slightly toward "
        "action over sitting on your hands when the need is real. The "
        "roster below marks some players as \"protected\": true -- these "
        "can never be the drop, full stop, no matter how compelling the add "
        "looks. This league has an IR roster slot: a player whose "
        "injury_status is exactly \"INJURY_RESERVE\" can move there instead "
        "of occupying a normal bench spot, which frees a roster spot for "
        "free, without dropping anyone. The roster below marks each "
        "player's lineup_slot, and ir_slot_open tells you whether the IR "
        "slot is currently available. If a rostered player has "
        "injury_status \"INJURY_RESERVE\" and isn't already in the IR slot, "
        "and ir_slot_open is true, prefer moving them to IR (set ir_move to "
        "their name) over dropping a rosterable player outright -- it "
        "accomplishes the same roster-space goal without permanently "
        "losing them. Separately, open_roster_spots tells you how many "
        "non-IR roster spots are already sitting empty right now, for any "
        "reason (e.g. a past IR move that already happened) -- not just "
        "one you're making this turn. If open_roster_spots is greater than "
        "0, an add doesn't need a drop at all: leave \"drop\" null. Only "
        "propose an actual drop when there's no open spot (via an existing "
        "gap or a fresh ir_move) and no such free option exists, or when "
        "dropping is clearly correct regardless. Besides the projection, season_points and avg_points_per_game show what a player has actually produced: a player ESPN projects well but who has averaged very little all season is a mirage (a low-average running back is not a real starter just because he holds a slot), and a proven producer is worth more than a flimsy projection suggests. A projection of 0 usually means a bye week, not a bad player. Weigh both. If required_drop_candidates in the payload is not empty, any drop you propose MUST be one of those names -- this order is enforced and a proposal that drops anyone else will be rejected. ROSTER CLEANUP: when a drop is needed, choose it with these preferences, in order, unless something clearly outweighs them. (1) A player with no_nfl_team true -- he has no team and cannot score. (2) A surplus kicker or D/ST: never carry two kickers or two D/STs. Keep the better one, but remember bye_week and the current week: if the one you'd keep is on a bye THIS week, the spare is still covering it, so hold it until that week passes; a good D/ST whose bye has already passed (bye_week earlier than the current week) is worth keeping for the rest of the season rather than churning. When you add a K or D/ST, prefer one whose bye isn't this week or soon. (3) A benched player with injury_status INJURY_RESERVE who can't go in the IR slot (it's full) and has little value to us. Only the model can see who is truly a backup versus a starter, so use judgment: never drop a real starter or a protected player for these reasons. The system can only drop a player as part of an add, never on its own, so pair a cleanup drop with the best add for a REAL need -- for example a starter listed in starters_on_bye_with_no_bench_cover, or a genuinely thin position -- not a redundant player. The lineup check runs separately and will start a bench K or D/ST that is not on a bye, so a bench kicker already covers a starting kicker's bye: do not add another kicker or D/ST. This is a single-QB league: do not add a backup QB just for depth while the starter is healthy and his bye has passed. A player with locked true "
        "already had his game start this week and cannot be dropped or moved "
        "to IR until it rolls over. If the user payload includes "
        "previous_proposal_rejected, that proposal is impossible for the "
        "stated reason: propose a different workable move (for example a "
        "different drop, or an add with a drop instead of an IR move), or "
        "no_move if nothing workable remains. Respond with ONLY valid "
        "JSON, no other text: "
        '{"action": "add_drop" or "no_move", '
        '"add": "player name or null", "drop": "player name or null", '
        '"ir_move": "player name or null -- set instead of drop when '
        'freeing a roster spot via IR is the better option", '
        '"reasoning": "2-3 sentences, in team voice", '
        '"rule_applied": "arbitrage_gap" or "home_turf_tiebreak" or "none"}'
    )

    roster_with_protection = [
        {**p, "protected": _normalize_name(p["name"]) in protected} for p in roster
    ]

    ir_slot_open = not no_ir and not any(p["lineup_slot"] == "IR" for p in roster)
    fa_positions = {_normalize_name(p["name"]): p["position"] for p, _ in ranked_fas}

    # Non-IR roster size for this league is 14 (1 QB/2 RB/2 WR/1 TE/1 FLEX/
    # 1 D-ST/1 K/5 BE); IR is a bonus 15th slot on top of that, not counted
    # in the 14. A player moving to IR (this run or a past one) vacates
    # their old non-IR spot, so the roster can be short of 14 non-IR
    # players even though nobody was just dropped for it.
    NON_IR_ROSTER_SIZE = 14
    non_ir_count = sum(1 for p in roster if p["lineup_slot"] != "IR")
    open_roster_spots = max(0, NON_IR_ROSTER_SIZE - non_ir_count)

    user_payload = json.dumps({
        "current_roster": roster_with_protection,
        "starters_on_bye_with_no_bench_cover": [
            {"name": p["name"], "slot": p["lineup_slot"]}
            for p in roster
            if _is_starter(p) and p["on_bye"] and not _bye_candidates(roster, p)
        ],
        "required_drop_candidates": _cleanup_drop_candidates(roster, protected)[1],
        "ir_slot_open": ir_slot_open,
        "open_roster_spots": open_roster_spots,
        "top_free_agents_by_position": [
            {**p, "arbitrage_gap": round(gap, 1)} for p, gap in ranked_fas
        ],
    })

    decision = call_for_json(client, system_prompt, user_payload, max_tokens=2000)

    # If the proposal can't actually be carried out, ask once more with the
    # reason rather than letting it fail at execution or giving up on the
    # week. Checked again; a second miss becomes no_move.
    retry_note = None
    problem = _waiver_problem(decision, roster, protected, ir_slot_open, fa_positions)
    if problem:
        retry_note = problem
        payload = json.loads(user_payload)
        payload["previous_proposal_rejected"] = {"proposal": decision, "why_impossible": problem}
        decision = call_for_json(client, system_prompt, json.dumps(payload), max_tokens=2000)
        problem = _waiver_problem(decision, roster, protected, ir_slot_open, fa_positions)
        if problem:
            decision = {
                "action": "no_move", "add": None, "drop": None, "ir_move": None,
                "reasoning": (
                    f"Vetoed: the first proposal wasn't possible ({retry_note}) and "
                    f"the retry still wasn't ({problem}). No move made this week."
                ),
                "rule_applied": "infeasible_move_veto",
            }

    # Hard enforcement, independent of whatever the model decided -- a
    # protected player is never dropped, no matter what the prompt said or
    # how the model reasoned. This is the actual guarantee; the prompt above
    # is just a courtesy so it doesn't waste a call proposing one.
    overridden = False
    if decision.get("action") == "add_drop" and _normalize_name(decision.get("drop") or "") in protected:
        overridden = True
        decision = {
            "action": "no_move",
            "add": None,
            "drop": None,
            "ir_move": None,
            "reasoning": (
                f'Vetoed: the model proposed dropping {decision["drop"]}, who is on the '
                "protected list. Overridden in code before anything could execute -- "
                "no move made this week."
            ),
            "rule_applied": "protected_player_veto",
        }

    # Same idea, different bug class: caught live 2026-09-22, the model
    # proposed "add"-ing a player already on our own roster (Jordan Mason --
    # looks like it confused the ir_move field with add while reasoning).
    # ESPN naturally has no "Add"/"Claim" button for your own player, so
    # this just times out in browser_actions with a confusing-looking
    # error. Catch the nonsensical decision before it ever reaches
    # execution, not after.
    roster_names = {_normalize_name(p["name"]) for p in roster}
    if decision.get("action") == "add_drop" and _normalize_name(decision.get("add") or "") in roster_names:
        overridden = True
        decision = {
            "action": "no_move",
            "add": None,
            "drop": None,
            "ir_move": None,
            "reasoning": (
                f'Vetoed: the model proposed adding {decision["add"]}, who is already on '
                "the roster -- almost certainly confused with the ir_move field. "
                "Overridden in code before anything could execute -- no move made this week."
            ),
            "rule_applied": "already_rostered_veto",
        }

    if decision["action"] == "add_drop" and decision.get("ir_move"):
        headline = f'Added {decision["add"]}, moved {decision["ir_move"]} to IR'
    elif decision["action"] == "add_drop":
        headline = f'Added {decision["add"]}, dropped {decision["drop"]}'
    elif overridden and decision.get("rule_applied") == "already_rostered_veto":
        headline = "Waiver move blocked -- tried to add an already-rostered player"
    elif decision.get("rule_applied") == "infeasible_move_veto":
        headline = "Waiver move blocked -- no workable option"
    elif overridden:
        headline = "Waiver move blocked -- protected player"
    else:
        headline = "No waiver move this week"

    entry = append_entry(
        kind="waiver",
        headline=headline,
        reasoning=decision["reasoning"],
        meta={
            "rule_applied": decision.get("rule_applied", "none"),
            **({"retried_because": retry_note} if retry_note else {}),
        },
    )

    return decision, entry


def _load_last_lineup_state():
    if not os.path.exists(LINEUP_STATE_PATH):
        return {}
    with open(LINEUP_STATE_PATH) as f:
        return json.load(f)


def _save_lineup_state(roster):
    state = {p["name"]: p["injury_status"] for p in roster}
    with open(LINEUP_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _is_starter(p):
    return p["lineup_slot"] not in ("BE", "IR")


def _bye_candidates(roster, sit, used=()):
    """Bench players who could replace a bye starter, best projection first.
    Out/IR players, locked players and players on a bye themselves can't."""
    candidates = [
        b for b in roster
        if b["lineup_slot"] == "BE"
        and b["name"] not in used
        and not b["on_bye"]
        and not b["locked"]
        and b["injury_status"] not in ("OUT", "INJURY_RESERVE")
        and sit["lineup_slot"] in b["eligible_slots"]
    ]
    return sorted(candidates, key=lambda b: b["week_projected_points"] or 0, reverse=True)


def _bye_alternatives(roster, sit, chosen):
    """Backup swaps for a bye starter, tried in order if ESPN won't make the
    first one (the executor skips any whose player was already used)."""
    return [
        {"start": c["name"], "sit": sit["name"]}
        for c in _bye_candidates(roster, sit)
        if c["name"] != chosen
    ]


def _bye_fixes(roster, skip_sit=(), skip_start=()):
    """Deterministic swaps that take starters on a bye out of the lineup.

    A bye player scores zero, so this never needs a judgment call about
    value -- only which bench player is eligible and best. The model's own
    picks are validated against the same facts and this is the backstop when
    it misses one. Each fix carries ranked `alternatives` for the executor to
    fall back on. Starters with no eligible replacement are simply left alone
    (the caller reports them).
    """
    fixes, used = [], set(skip_start)
    for sit in roster:
        if not (_is_starter(sit) and sit["on_bye"]) or sit["name"] in skip_sit:
            continue
        candidates = _bye_candidates(roster, sit, used)
        if not candidates:
            continue
        best = candidates[0]
        used.add(best["name"])
        fixes.append({
            "start": best["name"],
            "sit": sit["name"],
            "reason": f'{sit["name"]} is on a bye this week and scores nothing.',
            "alternatives": _bye_alternatives(roster, sit, best["name"]),
        })
    return fixes


def _validate_swaps(roster, swaps):
    """Hard checks on the model's swaps, same spirit as the waiver vetoes:
    never start a bye player, never move a player whose game already started
    (ESPN removes the MOVE button, so the swap would just fail), and only
    use a bench player in a slot he's actually eligible for. Returns
    (valid_swaps, rejected_descriptions).
    """
    by_name = {p["name"]: p for p in roster}
    valid, rejected, used = [], [], set()
    for s in swaps:
        start, sit = by_name.get(s.get("start")), by_name.get(s.get("sit"))
        label = f'{s.get("start")} over {s.get("sit")}'
        if not start or not sit:
            why = "player not on roster"
        elif start["lineup_slot"] != "BE" or not _is_starter(sit):
            why = "must move a bench player into a starting slot"
        elif start["on_bye"]:
            why = f'{start["name"]} is on a bye'
        elif start["locked"] or sit["locked"]:
            why = "a player involved is locked (game already started)"
        elif sit["lineup_slot"] not in start["eligible_slots"]:
            why = f'{start["name"]} is not eligible for the {sit["lineup_slot"]} slot'
        elif start["name"] in used or sit["name"] in used:
            why = "player already used in another swap"
        else:
            used.update((start["name"], sit["name"]))
            valid.append(s)
            continue
        rejected.append(f"{label} ({why})")
    return valid, rejected


def decide_lineup():
    """Pre-kickoff lineup check. Lineup Protection is off in this league and
    locks are per-player at kickoff, so this needs to actually catch injury
    news and bench/starter value gaps before each wave of games, not just
    once a week.

    strategy.md already has a rule for this -- "no decision gets reversed
    mid-week without a new data point" -- but nothing enforced it. Every
    check was re-reasoning the whole roster from scratch with no memory of
    the last call, so day-to-day noise in rank/ownership (not real news)
    was enough to flip a close call back and forth. Found in the wild:
    Kelce vs. Loveland flipped between checks while both stayed ACTIVE the
    entire time -- nothing real changed, just noise. Now it only reopens
    the question for players whose injury_status actually changed.
    """
    league = get_league()
    team = get_my_team(league)
    roster = get_roster_snapshot(team, league.current_week)

    last_state = _load_last_lineup_state()
    current_state = {p["name"]: p["injury_status"] for p in roster}
    changed = [name for name, status in current_state.items() if last_state.get(name) != status]

    # A starter on a bye is a new data point on its own -- it never shows up
    # as an injury_status change -- so it reopens the call until it's fixed.
    bye_starters = [p["name"] for p in roster if _is_starter(p) and p["on_bye"]]
    bye_fixable = _bye_fixes(roster)

    if last_state and not changed and not bye_fixable:
        reasoning = (
            "Every rostered player's injury status is unchanged since the last check. "
            "Per strategy, a call doesn't get reopened without a new data point -- "
            "rank or ownership drift alone doesn't count."
        )
        entry = append_entry(
            kind="lineup",
            headline="No lineup changes -- nothing new since last check",
            reasoning=reasoning,
            meta={"swap_count": 0},
        )
        _save_lineup_state(roster)
        return {"swaps": [], "reasoning": reasoning}, entry

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    system_prompt = load_strategy() + (
        "\n\nYou are the decision engine described above, now checking the "
        "starting lineup before kickoff. Given the full roster below (each "
        "player's lineup_slot -- a starting slot name, or 'BE' for bench -- "
        "position, injury_status, and projected_points), decide whether any "
        "bench player should start over a current starter at an eligible "
        "slot. A starter with on_bye true plays no game this week and scores "
        "zero, so he must be replaced by an eligible bench player who is "
        "not on a bye, not locked and not out -- this is required, not a "
        "close call. Never start a player with on_bye true. A player with "
        "locked true already had his game start and cannot be moved at all, "
        "in either direction. Use week_projected_points (ESPN's projection "
        "for this week) to compare players, and eligible_slots to check a "
        "bench player can fill a slot. Besides the projection, season_points and avg_points_per_game show what a player has actually produced: a player ESPN projects well but who has averaged very little all season is a mirage (a low-average running back is not a real starter just because he holds a slot), and a proven producer is worth more than a flimsy projection suggests. A projection of 0 usually means a bye week, not a bad player. Weigh both. Otherwise only recommend a swap when "
        "there's a real edge: the current "
        "starter is questionable/doubtful/out, or a bench player at an "
        "eligible position clearly out-projects them. Players whose "
        "injury_status changed since the last check are listed separately "
        "below -- focus there first. Don't reopen a call you already made "
        "this week for a player whose status hasn't changed; small "
        "rank/ownership movement alone is not a new data point. Respond "
        "with ONLY valid JSON, no other text: "
        '{"swaps": [{"start": "player name", "sit": "player name", '
        '"reason": "one line"}], '
        '"reasoning": "2-3 sentences overall, in team voice"}'
        ' If no changes are needed, "swaps" must be an empty list.'
    )

    user_payload = json.dumps({
        "roster": roster,
        "changed_since_last_check": changed if last_state else "first check this week, no prior state",
        "starters_on_bye": bye_starters,
    })

    decision = call_for_json(client, system_prompt, user_payload, max_tokens=2000)
    _save_lineup_state(roster)

    decision["swaps"], rejected = _validate_swaps(roster, decision.get("swaps") or [])
    by_name = {p["name"]: p for p in roster}
    for swap in decision["swaps"]:
        sit = by_name[swap["sit"]]
        if _is_starter(sit) and sit["on_bye"]:
            swap["alternatives"] = _bye_alternatives(roster, sit, swap["start"])
    # Backstop: if the model missed a bye starter that can be replaced, do it.
    backstop = _bye_fixes(
        roster,
        skip_sit={s["sit"] for s in decision["swaps"]},
        skip_start={s["start"] for s in decision["swaps"]},
    )
    decision["swaps"] += backstop
    covered = {s["sit"] for s in decision["swaps"]}
    uncovered = [n for n in bye_starters if n not in covered]
    if uncovered:
        decision["reasoning"] += (
            f' {", ".join(uncovered)} on a bye with no eligible bench player to '
            "cover -- needs a waiver add."
        )

    if decision["swaps"]:
        headline = "; ".join(f'Start {s["start"]} over {s["sit"]}' for s in decision["swaps"])
    else:
        headline = "No lineup changes needed"

    entry = append_entry(
        kind="lineup",
        headline=headline,
        reasoning=decision["reasoning"],
        meta={
            "swap_count": len(decision["swaps"]),
            "bye_starters": bye_starters,
            "bye_backstop_swaps": len(backstop),
            "rejected_swaps": rejected,
        },
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

    if "--verify-byes" in sys.argv:
        # Post-execution check for the lineup workflow: re-read the roster
        # from ESPN and report any bye starter that's still in the lineup
        # (e.g. a swap that failed) and can still be replaced. No model call,
        # no log entry.
        league = get_league()
        roster = get_roster_snapshot(get_my_team(league), league.current_week)
        print(json.dumps({"swaps": _bye_fixes(roster)}))
        sys.exit(0)
    if "--draft-queue" in sys.argv:
        decision, entry = build_draft_queue()
    else:
        kind = "lineup" if "--lineup" in sys.argv else "waiver"
        try:
            decision, entry = decide_lineup() if kind == "lineup" else decide_waiver_move(no_ir="--no-ir" in sys.argv)
        except Exception as e:
            # The execution-step alerting never covered this: a crash while
            # *deciding* (Sept 29's waiver run) left nothing on the dashboard
            # at all, indistinguishable from "no check ran". Log it as a red
            # alert (the workflow's if: always() commit step publishes it),
            # then re-raise so the job still shows red in Actions too.
            append_entry(
                kind=kind,
                headline="CHECK COULD NOT RUN",
                reasoning=(
                    f"This check crashed before it could decide anything "
                    f"({type(e).__name__}: {str(e)[:200]}). Nothing was changed "
                    "on ESPN. The next scheduled check will try again -- worth "
                    "a manual look if a game is close."
                ),
                meta={"execution_failed": True, "stage": "decision"},
            )
            raise
    print(json.dumps(decision, indent=2))
