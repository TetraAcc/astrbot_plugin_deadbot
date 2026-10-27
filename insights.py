"""Local daily recaps and hero-matched personal observations, separate from MVP."""

import gzip
import hashlib
import json
import math
import statistics
import zlib
from collections import defaultdict
from datetime import datetime, timedelta

from .achievements import earned_achievements, relative_achievements
from .deadlock import hero_name, normalize
from .history import History
from .library import CHINA_TIME, Library
from .scoring import _cumulative, _fun_stats, _gold, _number, _snapshots

VERSION = "deadbot-insights-v1"
AXES = {
    "输出": {"damage": 0.8, "kills": 0.2},
    "发育": {"farm": 1},
    "承伤": {"taken": 0.7, "mitigated": 0.2, "absorbed": 0.1},
    "辅助": {"assists": 0.6, "healing": 0.2, "barriers": 0.1, "prevented": 0.1},
    "前期": {"early_damage": 0.7, "early_assists": 0.3},
    "推进": {"building": 0.8, "creeps": 0.2},
}


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
        if str(raw["match_info"]["match_id"]) == str(mid):
            return raw
    except (ValueError, OSError, EOFError, KeyError, TypeError, zlib.error):
        pass
    return None


def detail_metrics(raw, account):
    """Missing fields stay missing; source gold never falls back to total souls."""
    info = raw.get("match_info", {})
    duration = _number(info.get("duration_s"))
    players = info.get("players", [])
    if not duration or not isinstance(players, list):
        return {}
    if info.get("not_scored") is True or any(
        (_number(p.get("abandon_match_time_s")) or 0) > 0 for p in players
    ):
        return {}
    player = next((p for p in players if str(p.get("account_id")) == account), None)
    if not player:
        return {}
    snapshots = _snapshots(player, duration)
    if not snapshots or duration - max(snapshots) > 120:
        return {}
    final = snapshots[max(snapshots)]
    minutes = duration / 60
    rates = {}
    for key, field in {
        "damage": "player_damage",
        "taken": "player_damage_taken",
        "mitigated": "damage_mitigated",
        "absorbed": "damage_absorbed",
        "building": "boss_damage",
        "creeps": "creep_kills",
        "healing": "teammate_healing",
        "barriers": "teammate_barriering",
        "prevented": "heal_prevented",
    }.items():
        value = _number(final.get(field))
        if value is not None and _cumulative(snapshots, field):
            rates[key] = value / minutes
    for field in ("kills", "deaths", "assists"):
        value = _number(player.get(field))
        if value is not None:
            rates[field] = value / minutes
    lane, jungle = _gold(final, 2), _gold(final, 3)
    if lane is not None and jungle is not None:
        rates["farm"] = (lane + jungle) / minutes
    candidates = [s for s in snapshots if abs(s - 1200) <= 120]
    boundary = (
        max(snapshots)
        if duration <= 1200
        else (
            min(candidates, key=lambda s: (abs(s - 1200), s > 1200, s))
            if candidates
            else None
        )
    )
    if boundary and boundary <= duration:
        for key, field in (
            ("early_damage", "player_damage"),
            ("early_assists", "assists"),
        ):
            value = _number(snapshots[boundary].get(field))
            if value is not None and _cumulative(snapshots, field):
                rates[key] = value * 60 / boundary
    team_kills = [
        _number(p.get("kills")) for p in players if p.get("team") == player.get("team")
    ]
    fun = _fun_stats(
        player,
        final,
        info.get("custom_user_stats"),
        sum(team_kills)
        if team_kills and all(v is not None for v in team_kills)
        else None,
    )
    for key, value in (
        ("participation", fun.get("kill_participation")),
        ("accuracy", fun.get("hero_accuracy")),
    ):
        if value is not None:
            rates[key] = value
    return rates


class PersonalInsights:
    def __init__(self, path):
        self.history = History(path)
        self.library = Library(path)

    def rows(self, account):
        """Merge history and queried details; detail metadata resolves stale outcomes."""
        self.library.connect().close()
        db = self.history.connect()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS personal_match_cache (
                account TEXT NOT NULL, match_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(account,match_id))""")
            cached_rows = {
                mid: (source_hash, data)
                for mid, source_hash, data in db.execute(
                    "SELECT match_id,source_hash,data FROM personal_match_cache WHERE account=?",
                    (account,),
                )
            }
            updates = []
            entries = {
                str(mid): json.loads(text)
                for mid, text in db.execute(
                    "SELECT match_id,data FROM player_history WHERE account=?",
                    (account,),
                )
            }
            known = set(entries)
            known.update(
                str(r[0])
                for r in db.execute(
                    "SELECT match_id FROM deadlock_records WHERE account=?", (account,)
                )
            )
            result = []
            for mid in known:
                row = dict(entries.get(mid, {}))
                packed = db.execute(
                    "SELECT compressed_json FROM match_detail_cache WHERE match_id=?",
                    (mid,),
                ).fetchone()
                blob = packed[0] if packed else None
                detail_hash = hashlib.sha256(blob).hexdigest() if blob else ""
                source_hash = hashlib.sha256(
                    (VERSION + json.dumps(row, sort_keys=True) + detail_hash).encode()
                ).hexdigest()
                cached = cached_rows.get(mid)
                if cached and cached[0] == source_hash:
                    saved = json.loads(cached[1])
                    if saved:
                        result.append(saved)
                    continue
                raw = read_detail(blob, mid) if blob else None
                if raw:
                    info = raw["match_info"]
                    players = info.get("players", [])
                    player = next(
                        (p for p in players if str(p.get("account_id")) == account),
                        None,
                    )
                    if not player:
                        updates.append((account, mid, source_hash, "null"))
                        continue
                    won = info.get("winning_team")
                    row.update(
                        {
                            "match_id": mid,
                            "start_time": info.get("start_time"),
                            "match_duration_s": info.get("duration_s"),
                            "hero_id": player.get("hero_id"),
                            "game_mode": info.get("game_mode"),
                            "match_mode": info.get("match_mode"),
                            "player_match_outcome": (
                                1 if won == player.get("team") else 2
                            )
                            if type(won) is int and won in (0, 1)
                            else None,
                            "player_kills": player.get("kills"),
                            "player_deaths": player.get("deaths"),
                            "player_assists": player.get("assists"),
                            "team_abandoned": any(
                                (_number(p.get("abandon_match_time_s")) or 0) > 0
                                for p in players
                            ),
                            "not_scored": info.get("not_scored"),
                        }
                    )
                if not valid_history(row):
                    updates.append((account, mid, source_hash, "null"))
                    continue
                row["match_id"] = mid
                row["rates"] = detail_metrics(raw, account) if raw else {}
                row["detail_hash"] = detail_hash
                updates.append(
                    (account, mid, source_hash, json.dumps(row, ensure_ascii=False))
                )
                result.append(row)
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO personal_match_cache VALUES(?,?,?,?)",
                    updates,
                )
            return sorted(
                result,
                key=lambda r: (_number(r.get("start_time")) or 0, int(r["match_id"])),
                reverse=True,
            )
        finally:
            db.close()

    @staticmethod
    def _comparison(rows, field):
        prior = defaultdict(list)
        for row in rows[10:50]:
            value = row["rates"].get(field)
            if value is not None:
                prior[row.get("hero_id")].append(value)
        pairs = []
        for row in rows[:10]:
            value = row["rates"].get(field)
            base = prior.get(row.get("hero_id"), [])
            if value is not None and len(base) >= 5:
                pairs.append((value, statistics.mean(base)))
        if len(pairs) < 5:
            return None
        before, recent = (
            statistics.mean(p[1] for p in pairs),
            statistics.mean(p[0] for p in pairs),
        )
        ratios = [a / b for a, b in pairs if b > 0]
        return {
            "before": before,
            "recent": recent,
            "count": len(pairs),
            "ratio": statistics.median(ratios) if len(ratios) >= 5 else None,
        }

    def profile(self, account, mode):
        rows = [r for r in self.rows(account) if r["match_mode"] == mode][:50]
        fingerprint = hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        db = self.history.connect()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS personal_insights_cache (
                account TEXT NOT NULL, mode INTEGER NOT NULL, version TEXT NOT NULL,
                source_hash TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(account,mode,version))""")
            cached = db.execute(
                "SELECT source_hash,data FROM personal_insights_cache WHERE account=? AND mode=? AND version=?",
                (account, mode, VERSION),
            ).fetchone()
            if cached and cached[0] == fingerprint:
                return json.loads(cached[1])
            comparisons = {
                field: self._comparison(rows, field)
                for field in {key for fields in AXES.values() for key in fields}
                | {"deaths", "participation", "accuracy"}
            }
            axes = {}
            for name, fields in AXES.items():
                active = [
                    (comparisons[k]["ratio"], w)
                    for k, w in fields.items()
                    if comparisons[k] and comparisons[k]["ratio"] is not None
                ]
                # A missing primary field must not turn a tiny secondary field into a full axis.
                primary = next(iter(fields))
                if comparisons[primary] and comparisons[primary]["ratio"] is not None:
                    change = sum(math.log2(max(r, 0.01)) * w for r, w in active) / sum(
                        w for _, w in active
                    )
                    axes[name] = round(max(0, min(100, 50 + 25 * change)), 1)
                else:
                    axes[name] = None
            result = {
                "count": len(rows),
                "details": sum(bool(r["rates"]) for r in rows),
                "axes": axes,
                "comparisons": comparisons,
                "match_ids": [r["match_id"] for r in rows],
                "review_ids": [
                    r["match_id"]
                    for r in sorted(
                        rows[:10],
                        key=lambda r: r["rates"].get("deaths", -1),
                        reverse=True,
                    )[:2]
                    if r["rates"].get("deaths") is not None
                ],
            }
            result["activities"] = {}
            for axis, fields in AXES.items():
                primary = next(iter(fields))
                values = [
                    (r["rates"][primary], r.get("match_duration_s", 0))
                    for r in rows
                    if primary in r["rates"]
                ]
                result["activities"][axis] = (
                    sum(value * duration for value, duration in values)
                    / sum(duration for _, duration in values)
                    if values and sum(duration for _, duration in values)
                    else None
                )
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO personal_insights_cache VALUES(?,?,?,?,?)",
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

    def advice(self, account, name, mode="全部"):
        if mode not in ("全部", "标准", "常规", "排位"):
            raise ValueError("用法：/建议 [群友称呼] [标准或排位或全部]")
        modes = (1, 4) if mode == "全部" else ((4,) if mode == "排位" else (1,))
        lines = [f"🧭 DeadBot 打法建议 · {name}"]
        for selected in modes:
            data = self.profile(account, selected)
            if not data["count"]:
                continue
            lines.append(
                f"\n🎮 {'排位' if selected == 4 else '标准'} · 最近{data['count']}场"
            )
            units = {
                "输出": "英雄伤害/分",
                "发育": "兵野灵魂/分",
                "承伤": "承伤/分",
                "辅助": "助攻/分",
                "前期": "前期伤害/分",
                "推进": "建筑伤害/分",
            }
            for axis, value in data["activities"].items():
                if value is not None:
                    lines.append(
                        f"{axis}：{value:,.0f} {units[axis]}"
                        if axis != "辅助"
                        else f"辅助：{value:.2f} {units[axis]}"
                    )
            known = [
                f"{axis} {value:.0f}"
                for axis, value in data["axes"].items()
                if value is not None
            ]
            lines.append(
                "📈 个人基准：" + " / ".join(known)
                if known
                else "📈 先积累同英雄比赛，再比较六维变化。"
            )
            comparisons = data["comparisons"]
            tips = []
            death = comparisons.get("deaths")
            if (
                death
                and death["recent"] > death["before"] * 1.25
                and death["recent"] - death["before"] >= 0.05
            ):
                tips.append(
                    f"死亡/10分钟 {death['before'] * 10:.1f} → {death['recent'] * 10:.1f}。优先复盘后期关键死亡，检查进场时机和退路。"
                )
            damage, farm = comparisons.get("damage"), comparisons.get("farm")
            if (
                damage
                and farm
                and farm["ratio"] is not None
                and damage["ratio"] is not None
                and farm["ratio"] > 1.2
                and damage["ratio"] < 0.9
            ):
                tips.append(
                    "兵野收入增长，输出没有同步增长。复盘刷完资源后是否及时参与战斗或推进。"
                )
            participation = comparisons.get("participation")
            if (
                participation
                and participation["recent"] < participation["before"] - 0.1
            ):
                tips.append(
                    f"参团 {participation['before']:.0%} → {participation['recent']:.0%}。检查是否错过能参与的团战；分推局结合建筑贡献看。"
                )
            building = comparisons.get("building")
            if (
                damage
                and building
                and damage["ratio"] is not None
                and building["ratio"] is not None
                and damage["ratio"] >= 1
                and building["ratio"] < 0.8
            ):
                tips.append("输出保持住了，建筑贡献回落。复盘团战获胜后是否及时推进。")
            accuracy = comparisons.get("accuracy")
            if accuracy and accuracy["recent"] - accuracy["before"] >= 0.03:
                tips.append(
                    f"同英雄命中率 {accuracy['before']:.1%} → {accuracy['recent']:.1%}，保持当前射击练习。"
                )
            if (
                death
                and death["before"] > 0
                and death["recent"] < death["before"] * 0.8
            ):
                tips.append(
                    f"死亡/10分钟 {death['before'] * 10:.1f} → {death['recent'] * 10:.1f}，生存更稳定；继续检查关键团战的参与情况。"
                )
            if not tips:
                tips.append(
                    "先复盘一场自己的比赛：找两次关键死亡和一次团战后决策，下一场只练一个具体改进点。"
                )
            lines.extend("💡 " + tip for tip in tips[:2])
            if data["review_ids"]:
                lines.append("🔎 可复盘：" + "、".join(data["review_ids"]))
            if data["details"] < data["count"]:
                lines.append(
                    f"详情 {data['details']}/{data['count']}场，可用 /补全 {name}。"
                )
        if len(lines) == 1:
            lines.append("暂无标准或排位历史，请先 /同步。")
        lines.append(
            "\n六维50是自己的同英雄历史基准；发育、承伤描述打法，不代表越高越好。建议是复盘方向。"
        )
        return "\n".join(lines)

    def daily(self, roster, day="今天", now=None):
        now = now or datetime.now(CHINA_TIME)
        local_now = now.astimezone(CHINA_TIME)
        if day in ("今天", "昨天"):
            date = local_now.date() - timedelta(days=day == "昨天")
        else:
            try:
                date = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("日期填 今天、昨天 或 YYYY-MM-DD。") from exc
        start = datetime.combine(date, datetime.min.time(), CHINA_TIME).timestamp()
        matches = set()
        payloads = {}
        blocks = []
        missing = 0
        for account, name in roster.items():
            rows = [
                r
                for r in self.rows(account)
                if _number(r.get("start_time")) is not None
                and _number(r.get("match_duration_s")) is not None
                and start <= r["start_time"] + r["match_duration_s"] < start + 86400
            ]
            if not rows:
                continue
            matches.update(r["match_id"] for r in rows)
            blocks.append(f"\n👤 {name}")
            for mode in (1, 4):
                selected = [r for r in rows if r["match_mode"] == mode]
                if not selected:
                    continue
                wins = sum(r["player_match_outcome"] == 1 for r in selected)
                triples = [
                    [
                        _number(r.get("player_" + k))
                        for k in ("kills", "deaths", "assists")
                    ]
                    for r in selected
                ]
                triples = [t for t in triples if all(v is not None for v in t)]
                line = f"{'排位' if mode == 4 else '标准'} {wins}胜{len(selected) - wins}负"
                if triples:
                    line += " | 场均 " + "/".join(
                        f"{statistics.mean(t[i] for t in triples):.1f}"
                        for i in range(3)
                    )
                blocks.append(line)
            best = None
            awards = []
            for row in rows:
                mid = row["match_id"]
                raw = self.history.cached_detail(mid) if mid not in payloads else None
                if mid not in payloads:
                    payloads[mid] = normalize(raw, mid) if raw else None
                payload = payloads[mid]
                if not payload:
                    missing += 1
                    continue
                if not payload["scoring"].get("eligible"):
                    continue
                player = next(
                    p for p in payload["players"] if p["account_id"] == account
                )
                scored = next(
                    p
                    for p in payload["scoring"]["players"]
                    if str(p["account_id"]) == account
                )
                if best is None or scored["score"] > best[0]:
                    best = (scored["score"], row["match_id"], player["hero_id"])
                awards.extend(
                    a["name"]
                    for a in earned_achievements(player, scored, payload["duration_s"])
                )
                awards.extend(
                    a["name"]
                    for a in relative_achievements(payload, roster).get(account, [])
                )
            if best:
                blocks.append(
                    f"🌟 今日最佳 {hero_name(best[2])} {best[0]:.1f}分 | {best[1]}"
                )
            if awards:
                blocks.append("🎖 " + " | ".join(list(dict.fromkeys(awards))[:2]))
        lines = [f"📅 DeadBot 日报 · {date.isoformat()}"]
        if not matches:
            lines.append("当天暂无本地标准或排位比赛。新比赛未收录时先 /同步。")
        else:
            lines.append(f"🎮 共{len(matches)}场比赛")
            lines.extend(blocks)
            if missing:
                lines.append("\n部分比赛待补详情，使用 /补全 后可补上评分和成就。")
        lines.append("\n按中国时间的比赛结束日期统计。")
        return "\n".join(lines)
