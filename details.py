"""Deduplicated, bounded detail backfill with durable retry state."""

import time

import aiohttp

from .deadlock import normalize


class DetailBackfill:
    def __init__(self, history, library, analysis):
        self.history = history
        self.library = library
        self.analysis = analysis

    def targets(self, roster):
        """Each player's latest 50 per mode, never replace missing matches with older ones."""
        targets = {}
        for account in roster:
            for mode in (1, 4):
                data = self.analysis.calculate(account, mode)
                for mid in data["match_ids"]:
                    targets.setdefault(mid, set()).add(account)
        return targets

    def pending(self, roster, now=None):
        now = int(time.time()) if now is None else now
        targets = self.targets(roster)
        db = self.history.connect()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS detail_backfill_status (
                match_id TEXT PRIMARY KEY, status TEXT NOT NULL, retry_at INTEGER NOT NULL)""")
            db.commit()
            statuses = {
                mid: retry
                for mid, retry in db.execute(
                    "SELECT match_id,retry_at FROM detail_backfill_status"
                )
            }
        finally:
            db.close()
        missing = {}
        for mid, expected in targets.items():
            raw = self.history.cached_detail(mid)
            if raw:
                try:
                    payload = normalize(raw, mid)
                    if expected.issubset({p["account_id"] for p in payload["players"]}):
                        continue
                except ValueError:
                    pass
            missing[mid] = expected
        ready = {
            mid: expected
            for mid, expected in missing.items()
            if statuses.get(mid, 0) <= now
        }
        return {
            "total": len(targets),
            "missing": len(missing),
            "ready": ready,
            "deferred": len(missing) - len(ready),
        }

    def mark(self, mid, status, delay=0):
        db = self.history.connect()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO detail_backfill_status VALUES(?,?,?)",
                    (mid, status, int(time.time()) + delay),
                )
        finally:
            db.close()

    async def fetch(self, session, mid, expected, roster):
        """Do not overwrite saved data on mismatched IDs, modes or participation."""
        try:
            async with session.get(
                f"https://api.deadlock-api.com/v1/matches/{mid}/metadata",
                params={"disable_steam": "true"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    self.mark(
                        mid,
                        f"HTTP_{response.status}",
                        3600 if response.status == 404 else 60,
                    )
                    return response.status
                raw = await response.json()
            payload = normalize(raw, mid)
            info = raw["match_info"]
            if info.get("game_mode") != 1 or info.get("match_mode") not in (1, 4):
                raise ValueError("wrong mode")
            if not expected.issubset({p["account_id"] for p in payload["players"]}):
                raise ValueError("wrong participants")
            self.history.cache_detail(mid, raw)
            self.history.record(payload, roster)
            self.mark(mid, "saved")
            return 200
        except (TimeoutError, aiohttp.ClientError, ValueError, KeyError, TypeError):
            self.mark(mid, "unavailable", 60)
            return 0
