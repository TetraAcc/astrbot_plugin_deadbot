"""Deterministic contribution scoring from cached Deadlock match metadata."""

import math
from collections import Counter

VERSION = "deadbot-contribution-v3"
WEIGHTS = {
    "early_damage": 12.0,
    "early_assists": 6.0,
    "late_damage": 12.0,
    "kills": 20.0,
    "boss_damage": 12.0,
    "creep_kills": 3.0,
    "player_damage_taken": 10.0,
    "damage_mitigated": 3.0,
    "damage_absorbed": 2.0,
    "late_assists": 10.0,
    "teammate_healing": 4.0,
    "teammate_barriering": 3.0,
    "heal_prevented": 3.0,
}
CATEGORIES = {
    "early_damage": "early",
    "early_assists": "early",
    "late_damage": "late",
    "kills": "late",
    "boss_damage": "push",
    "creep_kills": "push",
    "player_damage_taken": "tank",
    "damage_mitigated": "tank",
    "damage_absorbed": "tank",
    "late_assists": "support",
    "teammate_healing": "support",
    "teammate_barriering": "support",
    "heal_prevented": "support",
}
FINAL_FIELDS = tuple(
    key
    for key in WEIGHTS
    if key
    not in {"early_damage", "early_assists", "late_damage", "late_assists", "kills"}
)
AUX_PER_MINUTE = {
    "teammate_healing": 50.0,
    "teammate_barriering": 20.0,
    "heal_prevented": 50.0,
}


def _number(value):
    """Return a finite nonnegative number, preserving missing values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _ratio_points(value, mean):
    ratio = value / mean
    return 0.6 * ratio


def _snapshots(player, duration):
    rows = {}
    stats = player.get("stats")
    if not isinstance(stats, list):
        return rows
    for row in stats:
        if not isinstance(row, dict):
            continue
        stamp = _number(row.get("time_stamp_s"))
        if stamp is not None and stamp <= duration:
            rows[stamp] = row
    return rows


def _cumulative(rows, field):
    previous = None
    for stamp in sorted(rows):
        value = _number(rows[stamp].get(field))
        if value is None:
            if field in rows[stamp] and rows[stamp][field] is not None:
                return False
            continue
        if previous is not None and value < previous:
            return False
        previous = value
    return True


def _gold(final, source):
    entries = final.get("gold_sources")
    if not isinstance(entries, list):
        return None
    entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("source") == source
    ]
    if len(entries) != 1:
        return None
    gold = _number(entries[0].get("gold"))
    # Nullable orb values denote sources without an orb payout in the API.
    orbs = entries[0].get("gold_orbs")
    orbs = 0.0 if orbs is None else _number(orbs)
    return gold + orbs if gold is not None and orbs is not None else None


def _death_cost(player, duration):
    rows = player.get("death_details")
    if not isinstance(rows, list):
        return None
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            return None
        stamp = _number(row.get("game_time_s"))
        respawn = _number(row.get("death_duration_s"))
        if stamp is None or stamp > duration or respawn is None:
            return None
        total += 135.0 * math.sqrt(respawn / 8.0)
    return total


def _fun_stats(player, final, definitions, team_kills):
    names = {}
    for entry in definitions if isinstance(definitions, list) else []:
        if (
            isinstance(entry, dict)
            and type(entry.get("id")) is int
            and isinstance(entry.get("name"), str)
        ):
            names[entry.get("id")] = entry["name"]
    values = {}
    custom = final.get("custom_user_stats")
    for entry in custom if isinstance(custom, list) else []:
        if (
            isinstance(entry, dict)
            and type(entry.get("id")) is int
            and entry.get("id") in names
        ):
            values[names[entry["id"]]] = _number(entry.get("value"))
    success = values.get("Parry Success")
    miss = values.get("Parry Miss")
    shots = values.get("Enemy Hero Accuracy##Shots")
    hits = values.get("Enemy Hero Accuracy##Hits")
    heads = values.get("Enemy Hero Accuracy##Headshots")
    attempts = success + miss if success is not None and miss is not None else 0
    accuracy = hits / shots if shots and hits is not None and hits <= shots else None
    headshot_rate = (
        heads / hits if hits and heads is not None and heads <= hits else None
    )
    kills, assists = _number(player.get("kills")), _number(player.get("assists"))
    kp = None
    if team_kills and kills is not None and assists is not None:
        candidate = (kills + assists) / team_kills
        if candidate <= 1:
            kp = candidate
    return {
        "denies": _number(player.get("denies")),
        "deny_souls": _gold(final, 7),
        "parry_success": success,
        "parry_miss": miss,
        "parry_success_rate": success / attempts if attempts else None,
        "hero_shots": shots,
        "hero_hits": hits,
        "hero_headshots": heads,
        "hero_accuracy": accuracy,
        "hero_headshot_rate": headshot_rate,
        "kill_participation": kp,
    }


def _redistribute(weights, active):
    adjusted = {
        key: weight if key in active else 0.0 for key, weight in weights.items()
    }
    global_pool = 0.0
    for key, weight in weights.items():
        if key in active or not weight:
            continue
        peers = [other for other in active if CATEGORIES[other] == CATEGORIES[key]]
        subtotal = sum(weights[other] for other in peers)
        if subtotal:
            for other in peers:
                adjusted[other] += weight * weights[other] / subtotal
        else:
            global_pool += weight
    subtotal = sum(adjusted.values())
    if subtotal:
        for key in active:
            adjusted[key] += global_pool * adjusted[key] / subtotal
    return adjusted


def _empty_player(player, team):
    return {
        "account_id": player.get("account_id"),
        "hero_id": player.get("hero_id"),
        "team": team,
        "score": None,
        "raw_score": None,
        "z_score": None,
        "base_score": None,
        "death_cost": None,
        "death_penalty": None,
        "farm_souls": None,
        "farm_share": None,
        "contribution_share": None,
        "economy_bonus": None,
        "metrics": {},
        "components": {},
        "category_scores": {},
        "fun": {},
        "notes": [],
    }


def _score_team(players, team_id, info, structural_notes):
    duration = _number(info.get("duration_s")) or 0.0
    team = {
        "team": team_id,
        "eligible": False,
        "notes": list(structural_notes),
        "missing_metrics": [],
        "boundary_s": None,
        "effective_weights": {},
        "players": [_empty_player(player, team_id) for player in players],
    }
    rows = [_snapshots(player, duration) for player in players]
    definitions = info.get("custom_user_stats") or []
    for player, snapshots, result in zip(players, rows, team["players"]):
        final = snapshots[max(snapshots)] if snapshots else {}
        kill_values = [
            _number(p.get("kills"))
            for p in players
            if p.get("team") == player.get("team")
        ]
        team_kills = (
            sum(kill_values) if all(k is not None for k in kill_values) else None
        )
        result["fun"] = _fun_stats(player, final, definitions, team_kills)
    if structural_notes:
        return team
    if any(
        (_number(player.get("abandon_match_time_s")) or 0) > 0 for player in players
    ):
        team["notes"].append("abandoned_player")
        return team
    if any(not snapshots for snapshots in rows):
        team["notes"].append("missing_snapshots")
        return team
    ends = [max(snapshots) for snapshots in rows]
    if any(duration - stamp > 120 for stamp in ends):
        team["notes"].append("stale_final_snapshot")
        return team
    if len(set(ends)) > 1 or any(stamp != duration for stamp in ends):
        team["notes"].append("approximate_final_snapshot")
    finals = [snapshots[stamp] for snapshots, stamp in zip(rows, ends)]
    weights = dict(WEIGHTS)
    if duration <= 1200:
        weights["early_damage"] += weights.pop("late_damage")
        weights["early_assists"] += weights.pop("late_assists")
        weights["late_damage"] = weights["late_assists"] = 0.0
        boundary = duration
        early = finals
        team["notes"].append("short_match_weights_transferred")
    else:
        common = set(rows[0]).intersection(*(set(snapshots) for snapshots in rows[1:]))
        candidates = [
            stamp for stamp in common if abs(stamp - 1200) <= 120 and stamp < min(ends)
        ]
        if not candidates:
            team["notes"].append("missing_common_20m_snapshot")
            return team
        boundary = min(
            candidates, key=lambda stamp: (abs(stamp - 1200), stamp > 1200, stamp)
        )
        early = [snapshots[boundary] for snapshots in rows]
        if boundary != 1200:
            team["notes"].append(f"approximate_boundary:{boundary:g}")
    team["boundary_s"] = boundary
    metrics = []
    for source, snapshots, final, start in zip(players, rows, finals, early):
        damage, early_damage = (
            _number(final.get("player_damage")),
            _number(start.get("player_damage")),
        )
        assists, early_assists = (
            _number(final.get("assists")),
            _number(start.get("assists")),
        )
        if (
            damage is None
            or early_damage is None
            or assists is None
            or early_assists is None
            or damage < early_damage
            or assists < early_assists
            or not _cumulative(snapshots, "player_damage")
            or not _cumulative(snapshots, "assists")
        ):
            team["notes"].append("missing_or_invalid_core_metrics")
            return team
        values = {
            "early_damage": early_damage,
            "early_assists": early_assists,
            "late_damage": damage - early_damage,
            "late_assists": assists - early_assists,
            "kills": _number(source.get("kills")),
        }
        if values["kills"] is None:
            team["notes"].append("missing_settlement_kills")
            return team
        values.update(
            {
                field: _number(final.get(field))
                if _cumulative(snapshots, field)
                else None
                for field in FINAL_FIELDS
            }
        )
        metrics.append(values)
    missing = [
        key
        for key in weights
        if weights[key] and any(values[key] is None for values in metrics)
    ]
    team["missing_metrics"] = missing
    team["notes"].extend(f"missing_metric:{key}" for key in missing)
    if sum(weights[key] for key in missing) > 25:
        team["notes"].append("insufficient_metric_coverage")
        return team
    # A metric unavailable for one player is excluded for the full match.
    for values in metrics:
        for key in missing:
            values[key] = None
    means = {
        key: sum(values[key] for values in metrics) / len(players)
        for key in weights
        if key not in missing
    }
    active = {key for key, mean in means.items() if mean > 0 and weights[key] > 0}
    team["notes"].extend(
        f"zero_metric:{key}"
        for key, mean in means.items()
        if mean == 0 and weights[key] > 0
    )
    if not active:
        team["notes"].append("no_comparable_contribution")
        return team
    adjusted = _redistribute(weights, active)
    team["effective_weights"] = adjusted
    costs = [_death_cost(player, duration) for player in players]
    counts_complete = all(
        isinstance(player.get("death_details"), list)
        and len(player["death_details"])
        in {_number(player.get("deaths")), _number(final.get("deaths"))}
        for player, final in zip(players, finals)
    )
    if not all(cost is not None for cost in costs) or not counts_complete:
        team["notes"].append("missing_death_details")
        return team
    mean_cost = sum(costs) / len(players)
    for source, final, values, cost, result in zip(
        players, finals, metrics, costs, team["players"]
    ):
        result["team"] = source.get("team")
        result["metrics"] = values
        result["components"] = {
            key: {
                "value": values[key],
                "weight": adjusted[key],
                "match_mean": means.get(key),
                "points": adjusted[key] * _ratio_points(values[key], means[key])
                if key in active
                else 0.0,
            }
            for key in weights
        }
        for key, scale in AUX_PER_MINUTE.items():
            if key in active:
                result["components"][key]["points"] *= values[key] / (
                    values[key] + scale * duration / 60.0
                )
        categories = {category: 0.0 for category in set(CATEGORIES.values())}
        for key, component in result["components"].items():
            categories[CATEGORIES[key]] += component["points"]
        result["category_scores"] = categories
        result["base_score"] = sum(categories.values())
        result["death_cost"] = cost
        result["dead_seconds"] = sum(
            min(row["death_duration_s"], max(0.0, duration - row["game_time_s"]))
            for row in source["death_details"]
        )
        if cost is not None and len(source.get("death_details", [])) != source.get(
            "deaths"
        ):
            result["notes"].append("death_count_mismatch")
        lane, neutral = _gold(final, 2), _gold(final, 3)
        farm = lane + neutral if lane is not None and neutral is not None else None
        result["farm_souls"] = farm
        teammate_kills = [
            _number(p.get("kills"))
            for p in players
            if p.get("team") == source.get("team")
        ]
        actual_team_kills = (
            sum(teammate_kills) if all(k is not None for k in teammate_kills) else None
        )
        result["fun"] = _fun_stats(source, final, definitions, actual_team_kills)
    match_base_mean = sum(player["base_score"] for player in team["players"]) / len(
        players
    )
    for result in team["players"]:
        contribution_factor = max(
            0.75, min(1.5, math.sqrt(match_base_mean / max(result["base_score"], 1.0)))
        )
        result["death_penalty"] = min(
            25.0,
            40.0 * result["dead_seconds"] / duration
            + (
                5.0 * result["death_cost"] / mean_cost * contribution_factor
                if mean_cost
                else 0.0
            ),
        )
        teammates = [p for p in team["players"] if p["team"] == result["team"]]
        base_total = sum(p["base_score"] for p in teammates)
        base_mean = base_total / len(teammates)
        farms_complete = all(p["farm_souls"] is not None for p in teammates)
        farm_total = sum(p["farm_souls"] for p in teammates) if farms_complete else 0.0
        if not farms_complete and "missing_farm_sources" not in team["notes"]:
            team["notes"].append("missing_farm_sources")
        share = result["base_score"] / base_total if base_total else None
        farm_share = result["farm_souls"] / farm_total if farm_total else None
        bonus = 0.0
        if (
            share
            and farm_share is not None
            and (
                result["base_score"] >= base_mean
                or math.isclose(result["base_score"], base_mean, rel_tol=1e-12)
            )
            and farm_share < share
        ):
            bonus = 5.0 * max(0.0, 1.0 - farm_share / share)
        result["contribution_share"] = share
        result["farm_share"] = farm_share
        result["economy_bonus"] = bonus
        result["raw_score"] = result["base_score"] - result["death_penalty"] + bonus
    team["eligible"] = True
    return team


def score_match(raw):
    """Compute reproducible contribution scores without network or persistence.

    Args:
        raw: Full Deadlock API metadata response containing ``match_info``.

    Returns:
        JSON-compatible result with version, eligibility, team diagnostics, player
        component points, death and economy adjustments, and unscored fun stats.
        Ineligible matches retain player identities with ``score=None``.
    """
    info = raw.get("match_info", {}) if isinstance(raw, dict) else {}
    if not isinstance(info, dict):
        info = {}
    notes = []
    players = info.get("players")
    if not isinstance(players, list):
        players = []
    players = [player for player in players if isinstance(player, dict)]
    if info.get("game_mode") != 1 or info.get("match_mode") not in (1, 4):
        notes.append("unsupported_match_mode")
    if info.get("not_scored") is True:
        notes.append("match_not_scored")
    if not _number(info.get("duration_s")):
        notes.append("invalid_duration")
    teams = {
        team: [player for player in players if player.get("team") == team]
        for team in (0, 1)
    }
    accounts = [player.get("account_id") for player in players]
    if (
        len(players) != 12
        or any(len(team) != 6 for team in teams.values())
        or any(isinstance(player.get("team"), bool) for player in players)
        or any(not _number(account) for account in accounts)
        or (
            all(_number(account) for account in accounts)
            and any(count != 1 for count in Counter(accounts).values())
        )
    ):
        notes.append("invalid_human_teams")
    pooled = _score_team(players, None, info, notes)
    normalization = {}
    if pooled["eligible"]:
        raw_scores = [player["raw_score"] for player in pooled["players"]]
        mean = math.fsum(raw_scores) / len(raw_scores)
        std = math.sqrt(
            math.fsum((value - mean) ** 2 for value in raw_scores) / len(raw_scores)
        )
        effective_std = max(12.0, std)
        normalization = {
            "mean": mean,
            "std": std,
            "effective_std": effective_std,
            "center": 50.0,
            "scale": 25.0,
            "tail_start": 90.0,
        }
        # Normalize once after all twelve raw contributions and penalties exist.
        for player in pooled["players"]:
            z_score = (player["raw_score"] - mean) / effective_std
            mapped = 50.0 + 25.0 * z_score
            if mapped > 90.0:
                mapped = 90.0 + 10.0 * (1.0 - math.exp(-(mapped - 90.0) / 10.0))
            player["z_score"] = z_score
            player["score"] = max(0.0, min(100.0, mapped))
    results = []
    for team_id, members in teams.items():
        identities = {p["account_id"] for p in members}
        team_result = dict(pooled)
        team_result["team"] = team_id
        team_result["players"] = [
            p for p in pooled["players"] if p["account_id"] in identities
        ]
        for p in team_result["players"]:
            p["team"] = team_id
        results.append(team_result)
    return {
        "version": VERSION,
        "eligible": any(team["eligible"] for team in results),
        "match_id": info.get("match_id"),
        "duration_s": info.get("duration_s"),
        "teams": results,
        "players": [player for team in results for player in team["players"]],
        "notes": notes,
        "normalization": normalization,
    }
