"""Local, mode-separated recent-match statistics with fingerprinted caching."""

import gzip
import hashlib
import json
import math
import zlib
from collections import Counter

from .deadlock import display, hero_name, number
from .history import History
from .library import Library
from .scoring import _fun_stats, _snapshots

VERSION = "deadbot-analysis-v1"
MODES = {1: "标准比赛", 4: "排位"}


class PlayerAnalysis:
    def __init__(self, path):
        self.history = History(path)
        self.library = Library(path)

    def calculate(self, account, mode):
        """Aggregate up to 50 latest valid matches of one mode from local data.

        Args:
            account: Game account ID.
            mode: Match mode 1 for standard or 4 for ranked.

        Returns:
            Structured metrics and trends, with field-specific sample counts.
        """
        self.library.connect().close()
        db = self.history.connect()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS player_analysis_cache (
                account TEXT NOT NULL, mode INTEGER NOT NULL, version TEXT NOT NULL,
                source_hash TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account,mode,version))""")
            entries = {
                mid: json.loads(data)
                for mid, data in db.execute(
                    "SELECT match_id,data FROM player_history WHERE account=?",
                    (account,),
                )
            }
            # Include queried details newer than the last history-list sync.
            for mid, data in db.execute(
                "SELECT match_id,data FROM deadlock_records WHERE account=?", (account,)
            ):
                if mid in entries:
                    continue
                packed = db.execute(
                    "SELECT compressed_json FROM match_detail_cache WHERE match_id=?",
                    (mid,),
                ).fetchone()
                if not packed:
                    continue
                try:
                    info = json.loads(gzip.decompress(packed[0]))["match_info"]
                    player = next(
                        p
                        for p in info["players"]
                        if str(p.get("account_id")) == account
                    )
                except (
                    ValueError,
                    OSError,
                    EOFError,
                    KeyError,
                    StopIteration,
                    TypeError,
                    zlib.error,
                ):
                    continue
                outcome = 1 if info.get("winning_team") == player.get("team") else 2
                entries[mid] = {
                    "match_id": int(mid),
                    "hero_id": player.get("hero_id"),
                    "start_time": info.get("start_time"),
                    "game_mode": info.get("game_mode"),
                    "match_mode": info.get("match_mode"),
                    "player_match_outcome": outcome
                    if info.get("winning_team") in (0, 1)
                    else None,
                    "player_kills": player.get("kills"),
                    "player_deaths": player.get("deaths"),
                    "player_assists": player.get("assists"),
                    "net_worth": player.get("net_worth"),
                    "match_duration_s": info.get("duration_s"),
                    "team_abandoned": any(
                        (number(p.get("abandon_match_time_s")) or 0) > 0
                        for p in info["players"]
                    ),
                    "abandoned_time_s": player.get("abandon_match_time_s"),
                    "not_scored": info.get("not_scored"),
                }
            valid = [
                p
                for p in entries.values()
                if type(p.get("game_mode")) is int
                and p["game_mode"] == 1
                and type(p.get("match_mode")) is int
                and p["match_mode"] == mode
                and type(p.get("player_match_outcome")) is int
                and p["player_match_outcome"] in (1, 2)
                and p.get("team_abandoned") is not True
                and p.get("not_scored") is not True
                and (number(p.get("abandoned_time_s")) or 0) == 0
            ]
            selected = sorted(
                valid,
                key=lambda p: (
                    number(p.get("start_time")) or -1,
                    number(p.get("match_id")) or 0,
                ),
                reverse=True,
            )[:50]
            blobs = {}
            for p in selected:
                mid = str(p["match_id"])
                row = db.execute(
                    "SELECT compressed_json FROM match_detail_cache WHERE match_id=?",
                    (mid,),
                ).fetchone()
                blobs[mid] = row[0] if row else None
            fingerprint = hashlib.sha256(
                json.dumps(selected, sort_keys=True).encode()
                + b"".join(
                    hashlib.sha256(blob or b"").digest() for blob in blobs.values()
                )
            ).hexdigest()
            row = db.execute(
                "SELECT source_hash,data FROM player_analysis_cache WHERE account=? AND mode=? AND version=?",
                (account, mode, VERSION),
            ).fetchone()
            if row and row[0] == fingerprint:
                return json.loads(row[1])
            count = len(selected)
            result = {
                "mode": mode,
                "count": count,
                "match_ids": [str(p["match_id"]) for p in selected],
                "wins": sum(p["player_match_outcome"] == 1 for p in selected),
                "metrics": {},
                "heroes": [],
                "recent_win_rate": None,
                "previous_win_rate": None,
                "streak": None,
                "observations": [],
            }
            triples = [
                (
                    number(p.get("player_kills")),
                    number(p.get("player_deaths")),
                    number(p.get("player_assists")),
                )
                for p in selected
            ]
            triples = [t for t in triples if all(v is not None for v in t)]
            result["kda_count"] = len(triples)
            result["kda_means"] = (
                [sum(t[i] for t in triples) / len(triples) for i in range(3)]
                if triples
                else None
            )
            total_deaths = sum(t[1] for t in triples)
            result["kda_ratio"] = (
                sum(t[0] + t[2] for t in triples) / total_deaths
                if total_deaths
                else None
            )
            result["no_deaths"] = bool(triples) and not total_deaths
            if count:
                streak_outcome = selected[0]["player_match_outcome"]
                streak = 0
                for p in selected:
                    if p["player_match_outcome"] != streak_outcome:
                        break
                    streak += 1
                result["streak"] = {"outcome": streak_outcome, "count": streak}
            if count >= 20:
                result["recent_win_rate"] = (
                    sum(p["player_match_outcome"] == 1 for p in selected[:10]) / 10
                )
                result["previous_win_rate"] = (
                    sum(p["player_match_outcome"] == 1 for p in selected[10:20]) / 10
                )
            for hid, hero_count in Counter(
                p.get("hero_id") for p in selected
            ).most_common(3):
                group = [p for p in selected if p.get("hero_id") == hid]
                result["heroes"].append(
                    {
                        "hero_id": hid,
                        "count": hero_count,
                        "win_rate": sum(p["player_match_outcome"] == 1 for p in group)
                        / hero_count,
                    }
                )
            detail_values = {
                key: []
                for key in (
                    "damage",
                    "damage_per_minute",
                    "taken",
                    "building",
                    "participation",
                )
            }
            detail_by_match = {}
            shots = hits = heads = accuracy_count = headshot_count = 0
            headshot_hits = 0
            for p in selected:
                blob = blobs[str(p["match_id"])]
                if not blob:
                    continue
                try:
                    info = json.loads(gzip.decompress(blob))["match_info"]
                    if str(info.get("match_id")) != str(p["match_id"]):
                        continue
                    player = next(
                        x
                        for x in info["players"]
                        if str(x.get("account_id")) == account
                    )
                    duration = number(info.get("duration_s"))
                    if not duration:
                        continue
                    snapshots = _snapshots(player, duration)
                    if not snapshots or duration - max(snapshots) > 120:
                        continue
                    final = snapshots[max(snapshots)]
                    teammate_kills = [
                        number(x.get("kills"))
                        for x in info["players"]
                        if x.get("team") == player.get("team")
                    ]
                    team_kills = (
                        sum(teammate_kills)
                        if all(v is not None for v in teammate_kills)
                        else None
                    )
                    fun = _fun_stats(
                        player, final, info.get("custom_user_stats"), team_kills
                    )
                except (
                    ValueError,
                    OSError,
                    EOFError,
                    KeyError,
                    StopIteration,
                    TypeError,
                    zlib.error,
                ):
                    continue
                row_values = {}
                for key, field in (
                    ("damage", "player_damage"),
                    ("taken", "player_damage_taken"),
                    ("building", "boss_damage"),
                ):
                    value = number(final.get(field))
                    if value is not None:
                        detail_values[key].append(value)
                        row_values[key] = value
                        if key == "damage":
                            detail_values["damage_per_minute"].append((value, duration))
                participation = fun.get("kill_participation")
                if participation is not None:
                    detail_values["participation"].append(participation)
                if fun.get("hero_accuracy") is not None:
                    shots += fun["hero_shots"]
                    hits += fun["hero_hits"]
                    accuracy_count += 1
                    row_values["shots"] = fun["hero_shots"]
                    row_values["hits"] = fun["hero_hits"]
                if fun.get("hero_headshot_rate") is not None:
                    heads += fun["hero_headshots"]
                    headshot_hits += fun["hero_hits"]
                    headshot_count += 1
                detail_by_match[str(p["match_id"])] = row_values
            for key, values in detail_values.items():
                result["metrics"][key] = {
                    "value": (
                        sum(v for v, d in values) * 60 / sum(d for v, d in values)
                        if key == "damage_per_minute"
                        else sum(values) / len(values)
                    )
                    if values
                    else None,
                    "count": len(values),
                }
            result["metrics"]["accuracy"] = {
                "value": hits / shots if shots else None,
                "count": accuracy_count,
            }
            result["metrics"]["headshots"] = {
                "value": heads / headshot_hits if headshot_hits else None,
                "count": headshot_count,
            }
            soul_values = [
                (number(p.get("net_worth")), number(p.get("match_duration_s")))
                for p in selected
            ]
            soul_values = [
                (v, d)
                for v, d in soul_values
                if v is not None and d is not None and d > 0
            ]
            result["metrics"]["souls_per_minute"] = {
                "value": sum(v for v, d in soul_values)
                * 60
                / sum(d for v, d in soul_values)
                if soul_values
                else None,
                "count": len(soul_values),
            }
            if count >= 20:
                change = result["recent_win_rate"] - result["previous_win_rate"]
                result["observations"].append(
                    "最近10场胜率"
                    + ("上升" if change > 0 else "下降" if change < 0 else "持平")
                    + (f" {abs(change) * 100:.0f} 个百分点。" if change else "。")
                )
                newer = [number(p.get("player_deaths")) for p in selected[:10]]
                older = [number(p.get("player_deaths")) for p in selected[10:20]]
                if all(v is not None for v in newer + older):
                    delta = sum(newer) / 10 - sum(older) / 10
                    result["observations"].append(
                        "最近10场场均死亡"
                        + ("增加" if delta > 0 else "减少" if delta < 0 else "持平")
                        + (f" {abs(delta):.1f} 次。" if delta else "。")
                    )
                trend = []
                for window in (selected[10:20], selected[:10]):
                    rows = [detail_by_match.get(str(p["match_id"]), {}) for p in window]
                    rows = [r for r in rows if r.get("shots")]
                    trend.append(
                        sum(r["hits"] for r in rows) / sum(r["shots"] for r in rows)
                        if len(rows) == 10
                        else None
                    )
                if all(value is not None for value in trend):
                    result["observations"].append(
                        f"命中率：此前10场 {trend[0]:.1%} → 最近10场 {trend[1]:.1%}。"
                    )
                kda_trend = []
                for window in (selected[10:20], selected[:10]):
                    values = [
                        (
                            number(p.get("player_kills")),
                            number(p.get("player_deaths")),
                            number(p.get("player_assists")),
                        )
                        for p in window
                    ]
                    if all(all(v is not None for v in row) for row in values):
                        deaths = sum(row[1] for row in values)
                        kda_trend.append(
                            f"{sum(row[0] + row[2] for row in values) / deaths:.2f}"
                            if deaths
                            else "无死亡"
                        )
                if len(kda_trend) == 2:
                    result["observations"].append(
                        f"KDA：此前10场 {kda_trend[0]} → 最近10场 {kda_trend[1]}。"
                    )
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO player_analysis_cache VALUES(?,?,?,?,?)",
                    (
                        account,
                        mode,
                        VERSION,
                        fingerprint,
                        json.dumps(result, ensure_ascii=False),
                    ),
                )
            return result
        finally:
            db.close()

    def report(self, account, name, mode="全部"):
        """Render independent standard and ranked recent-match summaries.

        Args:
            account: Game account ID.
            name: Current roster display name.
            mode: 全部, 标准 or 排位.

        Returns:
            Data-backed Chinese summary without speculative playstyle labels.
        """
        if mode not in ("全部", "标准", "常规", "排位"):
            raise ValueError("模式请填写 标准、排位 或 全部：/分析 群友称呼 排位")
        modes = (1, 4) if mode == "全部" else (4,) if mode == "排位" else (1,)
        lines = [
            f"📊 DeadBot 打法分析 · {name}",
            "标准、排位分别取最近50场；乱斗与弃赛不计。",
        ]
        for match_mode in modes:
            data = self.calculate(account, match_mode)
            n = data["count"]
            lines.append(f"\n🎮 {MODES[match_mode]} · 最近{n}场")
            if not n:
                lines.append("本地暂无有效比赛。")
                continue
            wins = data["wins"]
            lines.append(f"🏁 胜率 {wins / n:.1%} | {wins}胜{n - wins}负")
            if data["recent_win_rate"] is not None:
                lines.append(
                    f"📈 胜率走向：此前10场 {data['previous_win_rate']:.0%} → 最近10场 {data['recent_win_rate']:.0%}"
                )
            streak = data["streak"]
            lines.append(
                f"🔥 当前{streak['count']}连"
                + ("胜" if streak["outcome"] == 1 else "败")
            )
            means = data["kda_means"]
            if means:
                ratio = (
                    f"{data['kda_ratio']:.2f}"
                    if data["kda_ratio"] is not None
                    else "无死亡"
                )
                lines.append(
                    "⚔️ 场均K/D/A "
                    + "/".join(f"{v:.1f}" for v in means)
                    + f" | KDA {ratio}"
                )
            lines.append(
                "🕹️ 常用英雄："
                + " | ".join(
                    f"{hero_name(h['hero_id'])} {h['count']}场/{h['win_rate']:.0%}胜率"
                    for h in data["heroes"]
                )
            )
            for key, label, icon, percent in (
                ("damage", "场均英雄伤害", "💥", False),
                ("damage_per_minute", "英雄伤害/分钟", "💥", False),
                ("taken", "场均承伤", "🛡️", False),
                ("building", "场均建筑伤害", "🚩", False),
                ("participation", "场均参团率", "⚔️", True),
                ("accuracy", "对英雄命中率", "🎯", True),
                ("headshots", "爆头率", "🎯", True),
                ("souls_per_minute", "灵魂/分钟", "💰", False),
            ):
                metric = data["metrics"][key]
                value = metric["value"]
                if value is not None and math.isfinite(value):
                    text = f"{value:.1%}" if percent else display(round(value, 1))
                    coverage = f" ({metric['count']}场)" if metric["count"] < n else ""
                    lines.append(f"{icon} {label} {text}{coverage}")
            if not data["metrics"]["accuracy"]["count"]:
                lines.append("🎯 命中率：暂无本地射击数据")
            if data["observations"]:
                lines.append("💡 " + " ".join(data["observations"]))
        lines.append("\n数据来源：本地已收录比赛；/同步 更新历史，/最新 查看最新战报。")
        return "\n".join(lines)
