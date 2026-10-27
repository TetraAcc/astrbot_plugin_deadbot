import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CHINA_TIME = timezone(timedelta(hours=8))

from .deadlock import hero_name, number


class Library:
    def __init__(self, path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("""CREATE TABLE IF NOT EXISTS player_history (
            account TEXT NOT NULL, match_id TEXT NOT NULL, start_time INTEGER,
            data TEXT NOT NULL, PRIMARY KEY(account,match_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS history_sync (
            account TEXT PRIMARY KEY, synced_at INTEGER NOT NULL, returned_count INTEGER NOT NULL)""")
        return db

    def last_sync(self, account):
        db = self.connect()
        try:
            row = db.execute(
                "SELECT synced_at FROM history_sync WHERE account=?", (account,)
            ).fetchone()
            return row[0] if row else 0
        finally:
            db.close()

    def latest_match(self, account):
        """Find the latest known participation without fetching remote history.

        Args:
            account: Game account ID to search in the local database.

        Returns:
            Latest match ID, or None when no local participation exists.
        """
        db = self.connect()
        try:
            query = "SELECT match_id,start_time FROM player_history WHERE account=?"
            params = [account]
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='deadlock_records'"
            ).fetchone():
                query += " UNION ALL SELECT match_id,start_time FROM deadlock_records WHERE account=?"
                params.append(account)
            row = db.execute(
                "SELECT match_id FROM ("
                + query
                + ") ORDER BY start_time DESC,CAST(match_id AS INTEGER) DESC LIMIT 1",
                params,
            ).fetchone()
            return row[0] if row else None
        finally:
            db.close()

    def import_history(self, account, payload):
        if not isinstance(payload, list):
            raise ValueError("历史接口返回格式异常")  # noqa: TRY004 - preserve command validation
        rows, seen = [], set()
        for row in payload:
            if not isinstance(row, dict) or str(row.get("account_id")) != account:
                raise ValueError("历史账号与查询账号不匹配")
            match_id = number(row.get("match_id"))
            if not match_id or match_id in seen:
                raise ValueError("无效或重复的历史比赛 ID")
            seen.add(match_id)
            rows.append(
                (
                    account,
                    str(match_id),
                    number(row.get("start_time")),
                    json.dumps(row, ensure_ascii=False),
                )
            )
        db = self.connect()
        try:
            with db:
                existing = {
                    r[0]
                    for r in db.execute(
                        "SELECT match_id FROM player_history WHERE account=?",
                        (account,),
                    )
                }
                db.executemany(
                    """INSERT INTO player_history VALUES(?,?,?,?)
                    ON CONFLICT(account,match_id) DO UPDATE SET start_time=excluded.start_time,data=excluded.data""",
                    rows,
                )
                db.execute(
                    "INSERT OR REPLACE INTO history_sync VALUES(?,?,?)",
                    (account, int(time.time()), len(rows)),
                )
                count = db.execute(
                    "SELECT COUNT(*) FROM player_history WHERE account=?", (account,)
                ).fetchone()[0]
            return {
                "returned": len(rows),
                "new": sum(r[1] not in existing for r in rows),
                "total": count,
            }
        finally:
            db.close()

    def stats(self, account, name, hero="", days=0, mode="1"):
        if days < 0 or days > 3650:
            raise ValueError("天数须为 0～3650；0 表示全部已同步历史。")
        if mode != "全部" and (not mode.isascii() or not mode.isdigit()):
            raise ValueError("模式请填写数字 ID 或 全部，默认 1。")
        db = self.connect()
        try:
            rows = db.execute(
                "SELECT data FROM player_history WHERE account=?", (account,)
            ).fetchall()
            sync = db.execute(
                "SELECT synced_at FROM history_sync WHERE account=?", (account,)
            ).fetchone()
        finally:
            db.close()
        if not rows:
            return f"{name} 暂无同步历史，请使用 /同步历史 {name}。"
        data = [json.loads(r[0]) for r in rows]
        cutoff = time.time() - days * 86400 if days else None
        filtered = [
            p
            for p in data
            if (
                cutoff is None
                or (
                    number(p.get("start_time")) is not None
                    and p["start_time"] >= cutoff
                )
            )
            and (mode == "全部" or str(p.get("game_mode")) == str(int(mode)))
            and (
                not hero or hero in (str(p.get("hero_id")), hero_name(p.get("hero_id")))
            )
        ]
        groups = defaultdict(list)
        excluded = 0
        for p in filtered:
            if (
                p.get("player_match_outcome") not in (1, 2)
                or type(p.get("player_match_outcome")) is not int
                or p.get("team_abandoned") is True
                or number(p.get("abandoned_time_s")) not in (None, 0)
            ):
                excluded += 1
                continue
            groups[(p.get("hero_id"), p.get("game_mode"), p.get("match_mode"))].append(
                p
            )
        lines = [
            f"{name} · 个人英雄统计",
            f"范围：{'全部已同步历史' if not days else str(days) + '天'}｜游戏模式 {mode}",
            f"本地 {len(data)} 场｜筛选 {len(filtered)} 场｜弃赛／非有效胜负排除 {excluded} 场",
        ]
        if sync:
            lines.append(
                "同步时间："
                + datetime.fromtimestamp(sync[0], CHINA_TIME).strftime(
                    "%Y-%m-%d %H:%M（中国时间）"
                )
            )
        ranked = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
        for (hid, gm, mm), entries in ranked[:8]:
            wins = sum(p["player_match_outcome"] == 1 for p in entries)

            def mean(key, entries=entries):
                values = [number(p.get(key)) for p in entries]
                values = [v for v in values if v is not None]
                return (
                    f"{sum(values) / len(values):.1f}(n={len(values)})"
                    if values
                    else "未知"
                )

            def rate(key, entries=entries):
                valid = [
                    (number(p.get(key)), number(p.get("match_duration_s")))
                    for p in entries
                ]
                valid = [
                    (v, s)
                    for v, s in valid
                    if v is not None and s is not None and s > 0
                ]
                return (
                    f"{sum(v for v, s in valid) * 60 / sum(s for v, s in valid):.1f}(n={len(valid)})"
                    if valid
                    else "未知"
                )

            lines.append(
                f"\n{hero_name(hid)}｜游戏模式 {gm}／匹配模式 {mm}\n{len(entries)} 场｜胜率 {wins / len(entries):.1%}（{wins}胜）"
            )
            lines.append(
                f"场均击杀/死亡/助攻：{mean('player_kills')}/{mean('player_deaths')}/{mean('player_assists')}"
            )
            lines.append(
                f"灵魂/分钟 {rate('net_worth')}｜补刀/分钟 {rate('last_hits')}"
            )
            if len(entries) < 10:
                lines.append("样本较少，暂不判断强弱或进步。")
        if not ranked:
            lines.append("没有符合条件的有效比赛；请检查英雄名、时间和模式。")
        if len(ranked) > 8:
            lines.append("展示场次最多的 8 组；指定英雄可查看其余记录。")
        lines.append(
            "按英雄与两种模式分组；分钟指标为总量÷有效总时长，括号 n 为样本数。仅代表 API 已收录数据，不作进步或评分结论。"
        )
        return "\n".join(lines)

    def profile(self, account, name):
        db = self.connect()
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM player_history WHERE account=?", (account,)
            ).fetchone()[0]
            recent = db.execute(
                "SELECT match_id,start_time,data FROM player_history WHERE account=? ORDER BY start_time DESC,CAST(match_id AS INTEGER) DESC LIMIT 5",
                (account,),
            ).fetchall()
        finally:
            db.close()
        lines = [f"DeadBot 画像 · {name}", f"已同步历史 {count} 场"]
        if not recent:
            lines.append(f"暂无同步历史，请使用 /同步历史 {name}。")
        else:
            lines.append("最近 5 场比赛（中国时间）：")
            for match_id, start, raw in recent:
                p = json.loads(raw)
                result = {1: "胜利", 2: "失败"}.get(
                    p.get("player_match_outcome"), "胜负未确认"
                )
                kda = "/".join(
                    str(p.get(k)) if number(p.get(k)) is not None else "?"
                    for k in ("player_kills", "player_deaths", "player_assists")
                )
                date = (
                    datetime.fromtimestamp(start, CHINA_TIME).strftime("%m-%d %H:%M")
                    if start is not None
                    else "时间未知"
                )
                lines.append(
                    f"{date}｜{match_id}\n{hero_name(p.get('hero_id'))}｜{result}｜K/D/A {kda}"
                )
        lines.append(
            f"英雄胜率与场均数据：/英雄统计 {name}\n当前是历史概览，尚未生成打法风格或进步结论。"
        )
        return "\n".join(lines)
