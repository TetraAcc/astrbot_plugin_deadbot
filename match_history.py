"""Shared history/detail precedence and eligibility for local statistics."""
import gzip
import json
import zlib
from .scoring import _number


def valid_history(row):
    return (
        type(row.get("game_mode")) is int
        and row["game_mode"] == 1
        and type(row.get("match_mode")) is int
        and row["match_mode"] in (1, 4)
        and type(row.get("player_match_outcome")) is int
        and row["player_match_outcome"] in (1, 2)
        and row.get("team_abandoned") is not True
        and row.get("not_scored") is not True
        and (_number(row.get("abandoned_time_s")) or 0) == 0
    )


def read_detail(blob, mid):
    try:
        raw = json.loads(gzip.decompress(blob))
        if (isinstance(raw, dict)
                and isinstance(raw.get("match_info"), dict)
                and str(raw["match_info"].get("match_id")) == str(mid)):
            return raw
    except (ValueError, OSError, EOFError, KeyError, TypeError, zlib.error):
        pass
    return None


def merge_history(row, raw, account, mid):
    """A verified detail overrides history; unreadable detail falls back to history.

    Valid detail with absent/ambiguous participation is excluded. Unknown outcome
    stays unknown rather than retaining a stale win or assuming a loss.
    """
    row = dict(row)
    if raw is None:
        return row
    info = raw["match_info"]
    players = info.get("players")
    if not isinstance(players, list) or any(not isinstance(p, dict) for p in players):
        return None
    matches = [p for p in players if str(p.get("account_id")) == account]
    if len(matches) != 1:
        return None
    player = matches[0]
    won, team = info.get("winning_team"), player.get("team")
    outcome = None
    if type(won) is int and won in (0, 1) and type(team) is int and team in (0, 1):
        outcome = 1 if won == team else 2
    row.update({
        "match_id": mid, "start_time": info.get("start_time"),
        "match_duration_s": info.get("duration_s"), "hero_id": player.get("hero_id"),
        "game_mode": info.get("game_mode"), "match_mode": info.get("match_mode"),
        "player_match_outcome": outcome,
        "player_kills": player.get("kills"), "player_deaths": player.get("deaths"),
        "player_assists": player.get("assists"), "net_worth": player.get("net_worth"),
        "abandoned_time_s": player.get("abandon_match_time_s"),
        "team_abandoned": any((_number(p.get("abandon_match_time_s")) or 0) > 0 for p in players),
        "not_scored": info.get("not_scored"),
    })
    return row
