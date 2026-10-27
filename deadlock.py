import hashlib
import json
import math
from pathlib import Path

from .scoring import VERSION, score_match

HEROES = json.loads(
    Path(__file__).with_name("heroes_zh.json").read_text(encoding="utf-8")
)


def hero_name(hero_id):
    return HEROES.get(str(hero_id), f"未知英雄（ID {hero_id}）")


def display(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return f"{value:,.0f}" if int(value) == value else f"{value:,.1f}"
    return "未知"


def number(value):
    return value if type(value) is int and value >= 0 else None


def normalize(raw, match_id, cached_score=None):
    info = raw.get("match_info") if isinstance(raw, dict) else None
    if not isinstance(info, dict) or str(info.get("match_id")) != match_id:
        raise ValueError("比赛 ID 不匹配")
    players = info.get("players")
    if not isinstance(players, list) or not players:
        raise ValueError("缺少玩家")
    result = {
        "source": "deadlock-api",
        "matchId": match_id,
        "start_time": number(info.get("start_time")),
        "duration_s": number(info.get("duration_s")),
        "players": [],
    }
    seen = set()
    for p in players:
        if not isinstance(p, dict) or not number(p.get("account_id")):
            raise ValueError("无效账号")
        account = str(p["account_id"])
        if account in seen:
            raise ValueError("账号重复")
        seen.add(account)
        item = {
            k: number(p.get(k))
            for k in ("hero_id", "kills", "deaths", "assists", "net_worth", "team")
        }
        item["account_id"] = account
        item["won"] = (
            (p["team"] == info["winning_team"])
            if type(p.get("team")) is int
            and p["team"] in (0, 1)
            and type(info.get("winning_team")) is int
            and info["winning_team"] in (0, 1)
            else None
        )
        item["abandoned"] = bool(number(p.get("abandon_match_time_s")))
        for key in ("last_hits", "denies", "level"):
            item[key] = number(p.get(key))
        stats = p.get("stats")
        snapshots = (
            [
                s
                for s in stats
                if isinstance(s, dict)
                and number(s.get("time_stamp_s")) is not None
                and (
                    result["duration_s"] is None
                    or s["time_stamp_s"] <= result["duration_s"]
                )
            ]
            if isinstance(stats, list)
            else []
        )
        latest = max(snapshots, key=lambda s: s["time_stamp_s"]) if snapshots else {}
        item["snapshot_time"] = latest.get("time_stamp_s")
        for key in (
            "player_damage",
            "player_damage_taken",
            "self_healing",
            "teammate_healing",
            "damage_mitigated",
            "damage_absorbed",
            "boss_damage",
            "creep_kills",
            "teammate_barriering",
            "heal_prevented",
        ):
            item[key] = number(latest.get(key))
        result["players"].append(item)
    fingerprint = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if (
        isinstance(cached_score, dict)
        and cached_score.get("version") == VERSION
        and cached_score.get("source_hash") == fingerprint
    ):
        result["scoring"] = cached_score
    else:
        result["scoring"] = score_match(raw)
        result["scoring"]["source_hash"] = fingerprint
    return result


def make_report(payload, roster, markdown=False):
    if isinstance(payload.get("scoring"), dict):
        from .scored_report import make_scored_report

        return make_scored_report(payload, roster, markdown=markdown)
    lines = [f"DeadBot 战报 · {payload['matchId']}"]
    duration = payload.get("duration_s")
    if duration is not None:
        lines.append(f"时长：{duration // 60} 分 {duration % 60} 秒")
    friends = [p for p in payload["players"] if p["account_id"] in roster]
    lines.append(
        f"本局匹配 {len(friends)}/{len(roster)} 位名单群友（共 {len(payload['players'])} 名玩家）"
    )
    for p in friends:
        result = (
            "胜利" if p["won"] is True else "失败" if p["won"] is False else "胜负未知"
        )
        kda = "/".join(
            str(p[k]) if p[k] is not None else "?"
            for k in ("kills", "deaths", "assists")
        )
        lines.append(
            f"\n{roster[p['account_id']]}｜{result}｜{hero_name(p['hero_id'])}\nK/D/A {kda}｜灵魂 {display(p['net_worth'])}"
            + ("｜中途离开标记" if p["abandoned"] else "")
        )
        lines.append(
            f"等级 {display(p.get('level'))}｜补刀 {display(p.get('last_hits'))}｜反补 {display(p.get('denies'))}"
        )
        stamp = p.get("snapshot_time")
        if stamp is not None:
            lines.append(f"战斗统计快照（{stamp // 60}:{stamp % 60:02d}）：")
            lines.append(
                f"对英雄伤害 {display(p.get('player_damage'))}｜承受玩家伤害 {display(p.get('player_damage_taken'))}"
            )
            lines.append(
                f"自身治疗 {display(p.get('self_healing'))}｜队友治疗 {display(p.get('teammate_healing'))}"
            )
        else:
            lines.append("战斗统计快照缺失，暂不展示伤害／治疗。")
    if not friends:
        lines.append("本局没有名单内群友。")
    lines.append(
        "当前为数据战报，未接入表现评分，不评选 MVP／背锅位。\n数据来源：Deadlock API"
    )
    return "\n".join(lines)
