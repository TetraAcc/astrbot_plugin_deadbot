"""Data-backed, localized match awards independent of contribution scoring."""

import json
import operator
from pathlib import Path

from .scoring import _number

CATALOG = json.loads(
    Path(__file__).with_name("achievements.json").read_text(encoding="utf-8")
)
TIERS = ("史诗", "稀有", "少见", "常见", "整活")
OPERATORS = {"ge": operator.ge, "eq": operator.eq, "lt": operator.lt, "le": operator.le}


def earned_achievements(player, scored, duration):
    """Evaluate known fields and keep the highest earned tier in each family.

    Args:
        player: Normalized match participant with final cumulative fields.
        scored: Accepted contribution result and named shooting statistics.
        duration: Actual match duration in seconds.

    Returns:
        All earned family winners in deterministic rarity and catalog order.
    """
    duration = _number(duration)
    if not duration or _number(scored.get("score")) is None or player.get("abandoned"):
        return []
    values = {key: _number(value) for key, value in player.items()}
    values.update({key: _number(value) for key, value in scored.get("fun", {}).items()})
    for field, rate in (
        ("player_damage", "damage_per_minute"),
        ("net_worth", "souls_per_minute"),
        ("boss_damage", "building_per_minute"),
        ("teammate_healing", "ally_heal_per_minute"),
        ("player_damage_taken", "taken_per_minute"),
    ):
        value = values.get(field)
        values[rate] = value * 60 / duration if value is not None else None
    kills, assists, deaths = (values.get(k) for k in ("kills", "assists", "deaths"))
    values["participations"] = (
        kills + assists if kills is not None and assists is not None else None
    )
    # Multiples remain meaningful at zero kills, subject to the absolute minimum.
    for field, numerator in (("assist_ratio", assists), ("death_ratio", deaths)):
        values[field] = (
            (numerator / kills if kills else float("inf") if numerator else 0.0)
            if kills is not None and numerator is not None
            else None
        )
    success, miss = (values.get(k) for k in ("parry_success", "parry_miss"))
    values["parry_attempts"] = (
        success + miss if success is not None and miss is not None else None
    )
    # Sample counts and valid ratios must agree; stale or partial fields earn nothing.
    shots, hits, heads = (
        values.get(k) for k in ("hero_shots", "hero_hits", "hero_headshots")
    )
    values["hero_accuracy"] = (
        hits / shots if shots and hits is not None and hits <= shots else None
    )
    values["hero_headshot_rate"] = (
        heads / hits
        if shots and hits and heads is not None and heads <= hits <= shots
        else None
    )
    earned, families = [], set()
    for award in sorted(CATALOG, key=lambda item: TIERS.index(item["tier"])):
        if award["family"] in families or not award["rules"]:
            continue
        if any(
            all(
                values.get(key) is not None and OPERATORS[op](values[key], target)
                for key, op, target in alternative
            )
            for alternative in award["rules"]
        ):
            earned.append(award)
            families.add(award["family"])
    return earned


def achievement_rules(selection="全部"):
    """Describe only achievements backed by implemented rules.

    Args:
        selection: All rules, a rarity label, or an exact localized award name.

    Returns:
        Chinese rule catalog or a short usage hint for an unknown selection.
    """
    enabled = [a for a in CATALOG if a["rules"]]
    selected = [a for a in enabled if selection in ("全部", a["tier"], a["name"])]
    if not selected:
        return "用法：/成就规则 [全部、史诗、稀有、少见、常见、整活或成就名称]"
    lines = [
        f"🏆 DeadBot 成就规则 · {len(enabled)}项",
        "独立于MVP。同系列保留最高档，战报每人显示最多3项；/成就详情 比赛ID 查看本局全部成就。",
        "灵魂/分钟=最终灵魂÷比赛分钟，治疗只统计队友治疗。命中率=英雄命中/射击，爆头率=英雄爆头/命中。",
    ]
    for tier in TIERS:
        rows = [a for a in selected if a["tier"] == tier]
        if not rows:
            continue
        lines.append("\n" + tier)
        for award in rows:
            lines.append(f"✅ {award['name']}：{award['condition']}")
    return "\n".join(lines)


def relative_achievements(payload, roster):
    """Find complete, positive roster-relative deny and parry leaders.

    Args:
        payload: Normalized match with named fun statistics for all participants.
        roster: Current display names keyed by account ID.

    Returns:
        Award lists keyed by roster participants; all known ties are retained.
    """
    friends = [p["account_id"] for p in payload["players"] if p["account_id"] in roster]
    scored = {str(p["account_id"]): p for p in payload["scoring"].get("players", [])}
    result = {account: [] for account in friends}
    if len(friends) < 2:
        return result
    for key, name, action in (
        ("denies", "反补王", "反补"),
        ("parry_success", "振刀王", "成功招架"),
    ):
        values = {
            account: _number(scored.get(account, {}).get("fun", {}).get(key))
            for account in friends
        }
        if any(value is None for value in values.values()) or max(values.values()) <= 0:
            continue
        maximum = max(values.values())
        for account, value in values.items():
            if value == maximum:
                result[account].append(
                    {"name": name, "condition": f"本局群友{action}最多，{maximum:g}次"}
                )
    return result


def make_achievement_report(payload, roster):
    """Render all earned family winners for roster participants in a match.

    Args:
        payload: Normalized full match, including the current scoring result.
        roster: Current display names keyed by account ID.

    Returns:
        A compact Chinese list of earned names and their rule thresholds.
    """
    scored = {str(p["account_id"]): p for p in payload["scoring"].get("players", [])}
    friends = [p for p in payload["players"] if p["account_id"] in roster]
    relative = relative_achievements(payload, roster)
    lines = [f"🏆 DeadBot 本场成就 · {payload['matchId']}"]
    for player in friends:
        lines.append("\n👤 " + roster[player["account_id"]])
        result = scored.get(player["account_id"], {})
        awards = relative.get(player["account_id"], []) + earned_achievements(
            player, result, payload.get("duration_s")
        )
        lines.extend(f"{a['name']}：{a['condition']}" for a in awards)
        if not awards:
            lines.append(
                "本场未获得成就。"
                if result.get("score") is not None
                else "本场数据暂不足以判定成就。"
            )
    if not friends:
        lines.append("本局没有名单内群友。")
    return "\n".join(lines)
