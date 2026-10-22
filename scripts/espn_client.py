"""Thin read-only wrapper around the ESPN fantasy football API.

Uses the community `espn_api` package, authenticated via session cookies.
ESPN has no official write API -- this file only ever reads data. All
actual roster/lineup/waiver changes happen in browser_actions.py.
"""
import os
from datetime import datetime, timezone
from espn_api.football import League


def get_league():
    return League(
        league_id=int(os.environ["ESPN_LEAGUE_ID"]),
        year=int(os.environ.get("ESPN_SEASON_YEAR", 2026)),
        espn_s2=os.environ["ESPN_S2"],
        swid=os.environ["ESPN_SWID"],
    )


def get_my_team(league):
    team_id = int(os.environ["ESPN_TEAM_ID"])
    for team in league.teams:
        if team.team_id == team_id:
            return team
    raise ValueError(f"No team with id {team_id} found in league {league.league_id}")


def _game_info(player, week):
    """(on_bye, kickoff_utc_iso, locked) for a player in the given week.

    espn_api keys player.schedule by the week number as a *string*, and a
    week with no key is a bye. Kickoff times come back as naive datetimes in
    the machine's local zone, so astimezone() (which assumes local for a naive
    value) turns them into real UTC on any machine, including the Actions
    runner. ESPN locks a player at his own kickoff, so locked == kickoff passed.
    """
    if week is None:
        return None, None, None
    game = (getattr(player, "schedule", None) or {}).get(str(week))
    if not game:
        return True, None, False
    kickoff = game.get("date")
    if kickoff is None:
        return False, None, False
    kickoff_utc = kickoff.astimezone(timezone.utc)
    return False, kickoff_utc.isoformat(), kickoff_utc <= datetime.now(timezone.utc)


def get_roster_snapshot(team, week=None):
    """Plain-data snapshot of the roster, safe to hand to Claude as JSON.

    Pass `week` (league.current_week) to include bye/kickoff/lock info and the
    ESPN projection for that specific week; without it those fields are None.
    """
    snapshot = []
    for p in team.roster:
        on_bye, kickoff, locked = _game_info(p, week)
        week_stats = (getattr(p, "stats", None) or {}).get(week) or {}
        snapshot.append({
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "lineup_slot": getattr(p, "lineupSlot", None),
            "eligible_slots": list(getattr(p, "eligibleSlots", None) or []),
            "injury_status": getattr(p, "injuryStatus", "ACTIVE"),
            "projected_points": getattr(p, "projected_points", None),
            "week_projected_points": week_stats.get("projected_points"),
            "on_bye": on_bye,
            "kickoff_utc": kickoff,
            "locked": locked,
            "percent_owned": getattr(p, "percent_owned", None),
            "percent_started": getattr(p, "percent_started", None),
            "espn_pos_rank": getattr(p, "posRank", None),
        })
    return snapshot


def get_free_agents(league, size=50, position=None):
    fas = (
        league.free_agents(size=size, position=position)
        if position
        else league.free_agents(size=size)
    )
    return [
        {
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "injury_status": getattr(p, "injuryStatus", "ACTIVE"),
            "projected_points": getattr(p, "projected_points", None),
            "percent_owned": getattr(p, "percent_owned", None),
            "percent_started": getattr(p, "percent_started", None),
            "espn_pos_rank": getattr(p, "posRank", None),
        }
        for p in fas
    ]
