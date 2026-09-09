"""Consensus Arbitrage signal, v1.

The strategy calls for comparing multiple rankings and finding the gap.
A truly free, reliable second public rankings source is the fiddly part --
most (FantasyPros ECR, etc.) don't have an open API. This v1 uses a
self-contained proxy instead: compare a player's projected output against
how the market is actually treating them (ownership/start rate). A player
projected well above their ownership rate is the underpriced signal Code
Red Zone is looking for.

Swap in a real second source later (another site's API, or a rankings CSV
you update by hand) -- the shape of arbitrage_gap() stays the same either
way, this just changes what "the market" means.
"""


def arbitrage_gap(player):
    """Higher = more underpriced by the market. None if data's missing."""
    proj = player.get("projected_points")
    owned = player.get("percent_owned")
    if proj is None or owned is None:
        return None
    proj_scaled = min(proj * 4, 100)  # rough scaling onto a 0-100 band
    return proj_scaled - owned


def rank_by_arbitrage(players):
    scored = [(p, arbitrage_gap(p)) for p in players]
    scored = [(p, g) for p, g in scored if g is not None]
    return sorted(scored, key=lambda pg: pg[1], reverse=True)


def is_loose_tie(gap_a, gap_b, tier_width=8):
    """Loose tie check for the Home Turf Clause -- within ~1 tier of each other."""
    return abs(gap_a - gap_b) <= tier_width
