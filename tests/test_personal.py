"""Personal commands use real handlers and SQLite, with network mocked."""
import ast
import asyncio
import importlib
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_history_consistency import PACKAGE, ROOT, History, Library
from test_scoring import make_match

accounts = importlib.import_module(PACKAGE + ".accounts")
latest = importlib.import_module(PACKAGE + ".latest")
report = importlib.import_module(PACKAGE + ".report")
deadlock = importlib.import_module(PACKAGE + ".deadlock")


def load_handlers():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DeadBot")
    cls.bases = []
    cls.decorator_list = []
    cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name in {"_resolve_account", "bind_account", "unbind_self", "latest", "_report"}]
    for node in cls.body:
        node.decorator_list = []
    namespace = dict(asyncio=asyncio, sqlite3=sqlite3, parse_account=accounts.parse_account,
                     identity=accounts.identity, parse_roster=report.parse_roster,
                     AstrMessageEvent=object)
    service = importlib.import_module(PACKAGE + ".statlocker_service")
    namespace.update(StatLockerError=service.StatLockerError, render_statlocker=service.render,
                     parse_match_id=importlib.import_module(PACKAGE + ".input").parse_match_id,
                     text_pages=importlib.import_module(PACKAGE + ".commands").text_pages)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(ROOT / "main.py"), "exec"), namespace)
    return namespace["DeadBot"]


class PersonalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plugin = load_handlers()()
        self.plugin.config = {"roster": []}
        path = Path(temp.name) / "history.db"
        self.plugin.history = History(path)
        self.plugin.library = Library(path)
        self.plugin.latest_history = SimpleNamespace(refresh=AsyncMock(return_value="已更新"))
        self.calls = []

        async def capture(event, match_id, **kwargs):
            self.calls.append((match_id, kwargs))
            yield "report"
        self.plugin._report = capture

    def event(self, sender="alice", platform="qq", group=""):
        return SimpleNamespace(get_sender_id=lambda: sender, get_platform_id=lambda: platform,
                               get_group_id=lambda: group, plain_result=lambda text: text)

    async def collect(self, method, event, *args):
        return [r async for r in method(event, *args)]

    async def test_bind_without_roster_and_same_identity_in_group_and_private(self):
        await self.collect(self.plugin.bind_account, self.event(group="group1"), "76561197960266729")
        for event in (self.event(), self.event(group="group2")):
            self.assertEqual(await self.plugin._resolve_account(event, "", {}), "1001")

    async def test_different_senders_and_adapters_do_not_share_binding(self):
        await self.collect(self.plugin.bind_account, self.event(), "1001")
        for event in (self.event("bob"), self.event(platform="other")):
            with self.assertRaises(ValueError):
                await self.plugin._resolve_account(event, "", {})

    async def test_unbind_only_affects_current_sender(self):
        for sender in ("alice", "bob"):
            await self.collect(self.plugin.bind_account, self.event(sender), "1001")
        await self.collect(self.plugin.unbind_self, self.event())
        self.assertIsNone(self.plugin.history.sender_account("qq", "alice"))
        self.assertEqual(self.plugin.history.sender_account("qq", "bob"), "1001")

    async def test_latest_queries_only_requested_player_and_handles_missing_history(self):
        await self.collect(self.plugin.bind_account, self.event(), "1001")
        result = await self.collect(self.plugin.latest, self.event())
        self.assertIn("暂无", result[-1])
        self.assertEqual(self.calls, [])
        self.plugin.library.import_history("1001", [dict(account_id=1001, match_id=123, start_time=100)])
        await self.collect(self.plugin.latest, self.event(group="large"))
        self.assertEqual(self.calls, [("123", {"focus_account": "1001"})])
        self.plugin.latest_history.refresh.assert_awaited_with("1001")

    async def test_unbound_latest_does_not_guess_from_roster(self):
        self.plugin.config["roster"] = ["1001=friend"]
        result = await self.collect(self.plugin.latest, self.event())
        self.assertIn("尚未绑定", result[0])
        self.plugin.latest_history.refresh.assert_not_awaited()

    async def test_report_failure_explicitly_refuses_local_fallback(self):
        service = importlib.import_module(PACKAGE + ".statlocker_service")
        self.plugin._report = load_handlers()._report.__get__(self.plugin)
        self.plugin.statlocker = SimpleNamespace(get=AsyncMock(side_effect=service.StatLockerError("页面未收录")))
        await self.collect(self.plugin.bind_account, self.event(), "1001")
        output = await self.collect(self.plugin._report, self.event(), "123")
        self.assertIn("不使用本地评分替代", output[-1])
        self.assertIn("页面未收录", output[-1])

    async def test_explicit_statlocker_mvp_link_works_without_binding(self):
        from test_statlocker_service import fixture
        self.plugin._report = load_handlers()._report.__get__(self.plugin)
        self.plugin.statlocker = SimpleNamespace(get=AsyncMock(return_value=(fixture(), True)))
        output = await self.collect(self.plugin._report, self.event(),
            "https://statlocker.gg/profile/1/matches/123/mvp/mvp-breakdown")
        self.assertIn("71.3 / B+", output[-1])
        self.plugin.statlocker.get.assert_awaited_once_with("123", "1", refresh=False)


class AccountAndReportTests(unittest.TestCase):
    def test_supported_account_inputs(self):
        for value in ("1001", "0001001", "[U:1:1001]", "76561197960266729",
                      "https://steamcommunity.com/profiles/76561197960266729/"):
            self.assertEqual(accounts.parse_account(value), "1001")

    def test_invalid_inputs_and_unresolved_vanity_are_rejected(self):
        for value in ("0", "-1", "4294967296", "76561197960265728", "１２３",
                      "https://evil.example/profiles/76561197960266729/",
                      "https://steamcommunity.com/id/somebody/", "[U:2:1001]"):
            with self.assertRaises(ValueError):
                accounts.parse_account(value)

    def test_personal_render_keeps_full_match_scores_and_hides_other_players(self):
        payload = deadlock.normalize(make_match(), "123456")
        before = repr(payload["scoring"])
        text = deadlock.make_report(payload, {"1001": "Self"}, personal=True)
        self.assertIn("个人复盘", text)
        self.assertIn("Self", text)
        self.assertNotIn("群友：", text)
        self.assertEqual(before, repr(payload["scoring"]))
        self.assertEqual(len(payload["players"]), 12)


class LatestRefreshTests(unittest.IsolatedAsyncioTestCase):
    def service(self, status=200, payload=None):
        library = Mock()
        library.last_sync.return_value = 0
        service = latest.LatestHistory(library)
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload or []))
        context = MagicContext(response)
        session = Mock(get=Mock(return_value=context))
        return service, library, session

    async def test_concurrent_same_player_reuses_one_request(self):
        service, library, session = self.service()
        with patch.object(latest.aiohttp, "ClientSession", return_value=MagicContext(session)):
            results = await asyncio.gather(*(service.refresh("1001") for _ in range(10)))
        self.assertEqual(len(set(results)), 1)
        session.get.assert_called_once()
        library.import_history.assert_called_once_with("1001", [])

    async def test_recent_history_avoids_network(self):
        service, library, session = self.service()
        library.last_sync.return_value = time.time()
        with patch.object(latest.aiohttp, "ClientSession", side_effect=AssertionError("network")):
            self.assertIn("缓存", await service.refresh("1001"))

    async def test_rate_limit_retains_history_and_cools_other_accounts(self):
        service, library, session = self.service(status=429)
        with patch.object(latest.aiohttp, "ClientSession", return_value=MagicContext(session)):
            self.assertIn("429", await service.refresh("1001"))
            self.assertIn("冷却", await service.refresh("1002"))
        library.import_history.assert_not_called()
        session.get.assert_called_once()

    async def test_bad_payload_keeps_existing_history(self):
        service, library, session = self.service()
        library.import_history.side_effect = ValueError("wrong account")
        with patch.object(latest.aiohttp, "ClientSession", return_value=MagicContext(session)):
            self.assertIn("刷新失败", await service.refresh("1001"))

    async def test_network_timeout_has_explicit_cache_fallback(self):
        service, library, session = self.service()
        session.get.side_effect = TimeoutError
        with patch.object(latest.aiohttp, "ClientSession", return_value=MagicContext(session)):
            self.assertIn("本地记录", await service.refresh("1001"))
        library.import_history.assert_not_called()

    async def test_report_slot_waits_and_releases_after_error(self):
        lock = asyncio.Lock()
        await lock.acquire()
        entered = asyncio.Event()

        async def waiting():
            async with latest.report_slot(lock) as acquired:
                self.assertTrue(acquired)
                entered.set()
                raise ValueError("renderer failed")

        task = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        lock.release()
        with self.assertRaises(ValueError):
            await task
        self.assertFalse(lock.locked())


class MagicContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False
