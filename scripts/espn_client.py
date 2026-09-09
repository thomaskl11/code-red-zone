"""Thin read-only wrapper around the ESPN fantasy football API.

Uses the community `espn_api` package, authenticated via session cookies.
ESPN has no official write API -- this file only ever reads data. All
actual roster/lineup/waiver changes happen in browser_actions.py.
"""
import os
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


def get_roster_snapshot(team):
    """Plain-data snapshot of the roster, safe to hand to Claude as JSON."""
    return [
        {
            "name": p.name,
            "position": p.position,
            "pro_team": p.proTeam,
            "lineup_slot": getattr(p, "lineupSlot", None),
            "injury_status": getattr(p, "injuryStatus", "ACTIVE"),
            "projected_points": getattr(p, "projected_points", None),
            "percent_owned": getattr(p, "percent_owned", None),
            "percent_started": getattr(p, "percent_started", None),
            "espn_pos_rank": getattr(p, "posRank", None),
        }
        for p in team.roster
    ]


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
