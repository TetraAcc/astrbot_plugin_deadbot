import asyncio
import copy
from datetime import datetime, timezone
import importlib
import tempfile
import unittest
from pathlib import Path
from test_history_consistency import PACKAGE

module = importlib.import_module(PACKAGE + ".statlocker_service")


def fixture(mid="123"):
    parts = [f"{label}\nExample:\n+1.0 pts\nCATEGORY TOTAL: 1.0 PTS\nTotal Points: 6.0 pts"
             for label in ["DAMAGE DEALER", "EARLY IMPACT", "PUSHER", "TANK", "SUPPORT", "DEATHS"]]
    return dict(match_id=mid, source="https://statlocker.gg", captured_at=datetime.now(timezone.utc).isoformat(),
                players=[dict(account_id=str(i), name=f"Player{i}", hero="Hero", rank=f"{i}th",
                              score=71.3, grade="B+", breakdown=parts[:]) for i in range(1, 13)])


class StatLockerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        async def fetch(mid, account):
            self.calls.append((mid, account))
            await asyncio.sleep(.01)
            return fixture(mid)
        self.fetch = fetch
        self.service = module.StatLockerService(self.temp.name, fetch=fetch, delay=0)

    async def asyncTearDown(self):
        await self.service.close()

    async def test_same_match_coalesces_and_persistent_cache_is_shared(self):
        results = await asyncio.gather(*(self.service.get("123", str(i)) for i in range(1, 6)))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r[0]["match_id"] == "123" for r in results))
        other = module.StatLockerService(self.temp.name, fetch=self.fetch, delay=0)
        _, cached = await other.get("123", "2")
        self.assertTrue(cached)
        self.assertEqual(len(self.calls), 1)
        await other.close()

    async def test_refresh_failure_never_returns_old_or_local_score(self):
        await self.service.get("123", "1")
        async def failed(*args):
            raise RuntimeError("site failed")
        self.service.fetch = failed
        with self.assertRaises(module.StatLockerError):
            await self.service.get("123", "1", refresh=True)
        with self.assertRaisesRegex(module.StatLockerError, "一分钟"):
            await self.service.get("123", "1", refresh=True)

    async def test_queue_is_bounded_and_distinct_matches_run_serially(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        async def blocked(mid, account):
            nonlocal active
            active += 1
            self.assertEqual(active, 1)
            entered.set()
            await release.wait()
            active -= 1
            return fixture(mid)
        self.service.fetch = blocked
        self.service.capacity = 1
        first = asyncio.create_task(self.service.get("123", "1"))
        await entered.wait()
        try:
            with self.assertRaisesRegex(module.StatLockerError, "队列已满"):
                await self.service.get("124", "1")
        finally:
            release.set()
        await first

    async def test_invalid_page_never_enters_cache(self):
        async def incomplete(mid, account):
            data = fixture(mid)
            data["players"].pop()
            return data
        self.service.fetch = incomplete
        with self.assertRaises(module.StatLockerError):
            await self.service.get("123", "1")
        self.assertFalse((Path(self.temp.name) / "123.json").exists())

    async def test_expired_or_corrupt_cache_is_refetched(self):
        await self.service.get("123", "1")
        self.service.ttl = 0
        await self.service.get("123", "1")
        self.assertEqual(len(self.calls), 2)
        (Path(self.temp.name) / "123.json").write_text("broken", encoding="utf-8")
        await self.service.get("123", "1")
        self.assertEqual(len(self.calls), 3)

    def test_render_uses_website_grade_and_only_requested_player(self):
        data = fixture()
        text = module.render(data, "1", True)
        self.assertIn("71.3 / B+", text)
        self.assertIn("Player1｜", text)
        self.assertNotIn("Player2｜", text)
        self.assertIn("缓存", text)
        self.assertIn("没有查询账号", module.render(data, "99", False))

    def test_validation_rejects_wrong_match_duplicates_and_mixed_tooltips(self):
        for change in ("match", "duplicate", "tooltip", "nan"):
            data = copy.deepcopy(fixture())
            if change == "match": data["match_id"] = "999"
            if change == "duplicate": data["players"][1]["account_id"] = "1"
            if change == "tooltip": data["players"][0]["breakdown"][0] = data["players"][0]["breakdown"][0].replace("6.0 pts", "9.0 pts")
            if change == "nan": data["players"][0]["score"] = float("nan")
            with self.subTest(change=change), self.assertRaises(ValueError):
                module.validate(data, "123")
