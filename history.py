import gzip
import json
import math
import sqlite3
import time
import zlib
from pathlib import Path

from .deadlock import hero_name


class History:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("""CREATE TABLE IF NOT EXISTS performances (
            match_id TEXT NOT NULL, account TEXT NOT NULL, score REAL,
            abandoned INTEGER NOT NULL, collected TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(match_id, account))""")
        db.execute("""CREATE TABLE IF NOT EXISTS deadlock_records (
            match_id TEXT NOT NULL, account TEXT NOT NULL, start_time INTEGER,
            data TEXT NOT NULL, PRIMARY KEY(match_id,account))""")
        db.execute("""CREATE TABLE IF NOT EXISTS match_detail_cache (
            match_id TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL,
            compressed_json BLOB NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS match_score_cache (
            match_id TEXT NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(match_id,version))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sender_bindings (
            platform TEXT NOT NULL, sender TEXT NOT NULL, account TEXT NOT NULL,
            PRIMARY KEY(platform,sender))""")
        return db

    def bind_sender(self, platform, sender, account):
        """Persist an explicitly selected game account for a chat sender.

        Args:
            platform: Unique platform adapter ID.
            sender: Platform sender ID.
            account: Validated roster account ID.
        """
        if not platform or not sender:
            raise ValueError("无法识别发送者，请在 QQ 私聊或群聊中绑定。")
        db = self.connect()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO sender_bindings VALUES(?,?,?)",
                    (platform, sender, account),
                )
        finally:
            db.close()

    def unbind_account(self, account: str) -> int:
        """Remove sender bindings associated with a removed account.

        Args:
            account: Validated roster account ID.

        Returns:
            Number of cleared sender bindings.
        """
        db = self.connect()
        try:
            with db:
                cursor = db.execute(
                    "DELETE FROM sender_bindings WHERE account=?",
                    (str(account),),
                )
                return cursor.rowcount
        finally:
            db.close()

    def sender_account(self, platform, sender):
        """Resolve a sender using an adapter-scoped persistent binding.

        Args:
            platform: Unique platform adapter ID.
            sender: Platform sender ID.

        Returns:
            Bound account ID, or None when no binding exists.
        """
        db = self.connect()
        try:
            row = db.execute(
                "SELECT account FROM sender_bindings WHERE platform=? AND sender=?",
                (platform, sender),
            ).fetchone()
            return row[0] if row else None
        finally:
            db.close()

    def cached_detail(self, match_id):
        db = self.connect()
        try:
            row = db.execute(
                "SELECT compressed_json FROM match_detail_cache WHERE match_id=?",
                (match_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        try:
            raw = json.loads(gzip.decompress(row[0]))
            if str(raw.get("match_info", {}).get("match_id")) != match_id:
                return None
            return raw
        except (ValueError, OSError, TypeError, AttributeError, EOFError, zlib.error):
            return None

    def cache_detail(self, match_id, raw):
        if (
            not isinstance(raw, dict)
            or str(raw.get("match_info", {}).get("match_id")) != match_id
        ):
            raise ValueError("比赛 ID 不匹配，未保存详情")
        packed = gzip.compress(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
        db = self.connect()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO match_detail_cache VALUES(?,?,?)",
                    (match_id, int(time.time()), packed),
                )
        finally:
            db.close()

    def cached_score(self, match_id, version):
        db = self.connect()
        try:
            row = db.execute(
                "SELECT data FROM match_score_cache WHERE match_id=? AND version=?",
                (match_id, version),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        try:
            result = json.loads(row[0])
            if (
                result.get("version") != version
                or str(result.get("match_id")) != match_id
            ):
                return None
            return result
        except (ValueError, AttributeError):
            return None

    def record(self, payload, roster):
        if payload.get("source") == "deadlock-api":
            db = self.connect()
            try:
                with db:
                    db.executemany(
                        "INSERT OR REPLACE INTO deadlock_records VALUES(?,?,?,?)",
                        [
                            (
                                payload["matchId"],
                                p["account_id"],
                                payload.get("start_time"),
                                json.dumps(p),
                            )
                            for p in payload["players"]
                            if p["account_id"] in roster
                        ],
                    )
                    score = payload.get("scoring")
                    if isinstance(score, dict):
                        db.execute(
                            "INSERT OR REPLACE INTO match_score_cache VALUES(?,?,?)",
                            (
                                payload["matchId"],
                                score["version"],
                                json.dumps(score, ensure_ascii=False),
                            ),
                        )
            finally:
                db.close()
            return
        players = payload.get("players")
        if not isinstance(players, list) or any(
            not isinstance(p, dict) for p in players
        ):
            raise ValueError("比赛玩家数据异常")
        accounts = [str(p.get("account_id")) for p in players]
        if len(set(accounts)) != len(accounts):
            raise ValueError("比赛账号重复")
        rows = []
        for p in players:
            account = str(p.get("account_id"))
            if account not in roster:
                continue
            score = p.get("mvpScore")
            if type(score) not in (int, float) or not math.isfinite(score):
                score = None
            rows.append(
                (
                    str(payload["matchId"]),
                    account,
                    score,
                    int(payload.get("matchWasAbandoned") is True),
                )
            )
        db = self.connect()
        try:
            with db:
                db.executemany(
                    """INSERT INTO performances(match_id,account,score,abandoned)
                    VALUES(?,?,?,?) ON CONFLICT(match_id,account) DO UPDATE SET
                    score=excluded.score, abandoned=excluded.abandoned""",
                    rows,
                )
        finally:
            db.close()

    def profile(self, account, name):
        db = self.connect()
        try:
            rows = db.execute(
                "SELECT match_id,score,abandoned FROM performances WHERE account=? ORDER BY collected DESC,match_id DESC",
                (account,),
            ).fetchall()
            new_rows = db.execute(
                "SELECT match_id,data FROM deadlock_records WHERE account=? ORDER BY start_time DESC,match_id DESC",
                (account,),
            ).fetchall()
        finally:
            db.close()
        lines = [
            f"DeadBot 画像 · {name}",
            f"已收录 {len(rows)} 场（仅手动查询过的比赛，不代表全部战绩）。",
        ]
        if new_rows:
            lines = [
                f"DeadBot 画像 · {name}",
                f"Deadlock API 已收录 {len(new_rows)} 场（仅已查询样本）。",
            ]
            for match_id, data in new_rows[:5]:
                p = json.loads(data)
                kda = "/".join(
                    str(p[k]) if p[k] is not None else "?"
                    for k in ("kills", "deaths", "assists")
                )
                lines.append(
                    f"比赛 {match_id}｜{hero_name(p['hero_id'])}｜K/D/A {kda}"
                    + ("｜中途离开标记" if p["abandoned"] else "")
                )
            lines.append(f"旧 StatLocker 记录 {len(rows)} 场，独立保留，不混合评分。")
            lines.append(
                "按比赛时间倒序展示，时间缺失排在末尾。当前不判定打法、进步或 MVP。"
            )
            return "\n".join(lines)
        valid = [(m, s) for m, s, a in rows if s is not None and not a]
        if not rows:
            lines.append("暂无战绩。成功使用 /战报 查询将自动积累记录，无需 API 密钥。")
        elif valid:
            scores = [s for _, s in valid]
            lines.append(
                f"有效评分 {len(scores)} 场｜均值 {sum(scores) / len(scores):.4g}｜范围 {min(scores):.4g}～{max(scores):.4g}"
            )
            lines.append(
                "最近收录的有效样本（不是比赛时间排序）："
                + "、".join(f"{m}（{s:.4g}）" for m, s in valid[:5])
            )
            lines.append(f"缺分或弃赛排除 {len(rows) - len(valid)} 场。")
        else:
            lines.append("现有记录均缺分或标记弃赛，暂不统计评分。")
        lines.append(
            "当前仅展示历史数据，不判定打法或进步：英雄、版本、比赛时间等字段尚待真实 API 核对。不同英雄和版本的评分不能直接作为进步依据。"
        )
        return "\n".join(lines)
