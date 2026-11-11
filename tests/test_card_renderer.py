import asyncio
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = str(Path(__file__).resolve().parent)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
PACKAGE = "deadbot_card_tests"
if PACKAGE not in sys.modules:
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = pkg

card_renderer = importlib.import_module(PACKAGE + ".card_renderer")
render_report_image = card_renderer.render_report_image
render_recent_matches_image = card_renderer.render_recent_matches_image
_wrap = card_renderer._wrap
_strip_emoji = card_renderer._strip_emoji
_n = card_renderer._n

try:
    from .test_statlocker_service import fixture
except ImportError:
    from test_statlocker_service import fixture


def sample_raw_match():
    return {
        "match_info": {
            "match_id": 123456,
            "duration_s": 1845,
            "winning_team": 0,
            "players": [
                {
                    "account_id": 1,
                    "hero_id": 1,
                    "team": 0,
                    "kills": 10,
                    "deaths": 2,
                    "assists": 15,
                    "net_worth": 24000,
                    "last_hits": 180,
                    "denies": 12,
                    "stats": [
                        {
                            "time_stamp_s": 1800,
                            "player_damage": 35000,
                            "player_damage_taken": 15000,
                            "boss_damage": 8000,
                            "creep_kills": 220,
                            "damage_mitigated": 5000,
                            "damage_absorbed": 2000,
                            "teammate_healing": 4500,
                            "teammate_barriering": 1200,
                        }
                    ],
                },
                {"account_id": 2, "hero_id": 2, "team": 0, "kills": 5, "deaths": 3, "assists": 8},
                {"account_id": 3, "hero_id": 3, "team": 1, "kills": 3, "deaths": 5, "assists": 2},
            ],
        }
    }


class CardRendererUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_files = []

    def tearDown(self):
        for f in self.temp_files:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def track(self, path):
        if path:
            self.temp_files.append(path)
        return path

    def sample_data(self):
        data = fixture("123456")
        comp = {
            "DAMAGE DEALER": 41.4,
            "SUPPORT": 14.3,
            "PUSHER": 12.2,
            "TEAM FARM BONUS": 6.0,
            "TANK": 5.6,
            "EARLY IMPACT": 5.3,
            "DEATHS": -13.6,
        }
        data["highlights"] = {
            "1": dict(
                account_id="1",
                match_id="123456",
                score=71.3,
                components=comp,
                facts=[
                    dict(
                        title="Killed every enemy",
                        value="6/6",
                        description="Player1 killed every enemy hero at least once",
                    ),
                    dict(
                        title="Most low-hp kills",
                        value="3",
                        description="of their 8 kills came while they were on 20% health or less",
                    ),
                ],
            )
        }
        data["farm_impact"] = {
            "1": dict(label="LOW FARM", farm="38k", bonus="+7.8", before_grade="B-")
        }
        return data

    def test_full_match_renders_valid_png(self):
        data = self.sample_data()
        raw = sample_raw_match()
        roster = {"1": "Tetra", "2": "Teammate"}
        path = self.track(render_report_image(data, "1", raw_match=raw, roster=roster))
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".png"))
        self.assertGreater(os.path.getsize(path), 5000)

    def test_api_only_with_stat_error_renders_valid_png(self):
        raw = sample_raw_match()
        roster = {"1": "Tetra"}
        path = self.track(
            render_report_image(None, "1", raw_match=raw, roster=roster, stat_error="StatLocker采集失败")
        )
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 5000)

    def test_statlocker_only_renders_valid_png(self):
        data = self.sample_data()
        path = self.track(render_report_image(data, "1"))
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 5000)

    def test_unknown_account_returns_none(self):
        data = self.sample_data()
        raw = sample_raw_match()
        path = self.track(render_report_image(data, "999999", raw_match=raw))
        self.assertIsNone(path)

    def test_no_data_returns_none(self):
        path = self.track(render_report_image(None, "1", raw_match=None))
        self.assertIsNone(path)

    def test_text_helpers(self):
        self.assertEqual(_strip_emoji("🎯 命中率：60% 🔥"), "命中率：60%")
        self.assertEqual(_n(1234.5), "1,234.5")
        self.assertEqual(_n(100), "100")
        self.assertEqual(_n(None), "—")
        self.assertEqual(_n(-5), "—")

    def test_wrap_splits_long_lines(self):
        mock_draw = Mock()
        mock_draw.textlength = lambda s, font=None: len(s) * 10
        lines = _wrap(mock_draw, "1234567890", None, max_w=40)
        self.assertEqual(lines, ["1234", "5678", "90"])


class CardRendererIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.images_to_clean = []

    async def asyncTearDown(self):
        for img in self.images_to_clean:
            if img and os.path.exists(img):
                try:
                    os.remove(img)
                except OSError:
                    pass

    async def test_report_yields_image_when_event_supports_image(self):
        main_mod = importlib.import_module(PACKAGE + ".main")
        DeadBot = main_mod.DeadBot

        class MockEvent:
            def __init__(self):
                self.images = []
                self.texts = []

            def plain_result(self, text):
                self.texts.append(text)
                return {"type": "text", "content": text}

            def image_result(self, path):
                self.images.append(path)
                return {"type": "image", "path": path}

        db_path = Path(self.temp_dir.name) / "test.db"
        with patch.object(main_mod, "History") as MockHistory:
            mock_hist = Mock()
            mock_hist.path = db_path
            mock_hist.cached_detail.return_value = sample_raw_match()
            MockHistory.return_value = mock_hist

            bot = DeadBot(Mock(), {"roster": ["1=测试玩家"], "render_image": True, "statlocker_python": sys.executable})
            bot.statlocker = Mock(get=AsyncMock(return_value=(fixture("123456"), False)))

            event = MockEvent()
            async for _ in bot._report(event, "123456", focus_account="1"):
                pass

            self.assertIn("正在查询 StatLocker", event.texts[0])
            self.assertEqual(len(event.images), 1)
            self.images_to_clean.append(event.images[0])
            self.assertTrue(os.path.exists(event.images[0]))

    async def test_report_yields_text_when_render_image_disabled(self):
        main_mod = importlib.import_module(PACKAGE + ".main")
        DeadBot = main_mod.DeadBot

        class MockEvent:
            def __init__(self):
                self.images = []
                self.texts = []

            def plain_result(self, text):
                self.texts.append(text)
                return {"type": "text", "content": text}

            def image_result(self, path):
                self.images.append(path)
                return {"type": "image", "path": path}

        db_path = Path(self.temp_dir.name) / "test.db"
        with patch.object(main_mod, "History") as MockHistory:
            mock_hist = Mock()
            mock_hist.path = db_path
            mock_hist.cached_detail.return_value = sample_raw_match()
            MockHistory.return_value = mock_hist

            bot = DeadBot(Mock(), {"roster": ["1=测试玩家"], "render_image": False, "statlocker_python": sys.executable})
            bot.statlocker = Mock(get=AsyncMock(return_value=(fixture("123456"), False)))

            event = MockEvent()
            async for _ in bot._report(event, "123456", focus_account="1"):
                pass

            self.assertEqual(len(event.images), 0)
            self.assertTrue(any("StatLocker 战报" in t for t in event.texts))

    async def test_recent_yields_image_when_enabled(self):
        main_mod = importlib.import_module(PACKAGE + ".main")
        DeadBot = main_mod.DeadBot

        class MockEvent:
            def __init__(self):
                self.images = []
                self.texts = []

            def plain_result(self, text):
                self.texts.append(text)
                return {"type": "text", "content": text}

            def image_result(self, path):
                self.images.append(path)
                return {"type": "image", "path": path}

        sample_matches = [
            {
                "match_id": "1001",
                "start_time": 1700000000,
                "hero_id": 1,
                "kills": 10,
                "deaths": 2,
                "assists": 15,
                "outcome": 1,
                "duration_s": 1800,
                "net_worth": 30000,
                "score": 75.0,
                "grade": "A",
            }
        ]

        bot = DeadBot(Mock(), {"roster": ["1=测试玩家"], "render_image": True})
        bot.latest_history = Mock(refresh=AsyncMock(return_value="已刷新"))
        bot.library = Mock(
            recent_matches_data=Mock(return_value=sample_matches),
            recent_report=Mock(return_value="文本战绩"),
        )
        bot._resolve_account = AsyncMock(return_value="1")

        event = MockEvent()
        async for res in bot.recent(event, name="测试玩家"):
            pass

        self.assertEqual(len(event.images), 1)
        self.images_to_clean.append(event.images[0])
        self.assertTrue(os.path.exists(event.images[0]))

    async def test_recent_yields_text_when_disabled(self):
        main_mod = importlib.import_module(PACKAGE + ".main")
        DeadBot = main_mod.DeadBot

        class MockEvent:
            def __init__(self):
                self.images = []
                self.texts = []

            def plain_result(self, text):
                self.texts.append(text)
                return {"type": "text", "content": text}

            def image_result(self, path):
                self.images.append(path)
                return {"type": "image", "path": path}

        bot = DeadBot(Mock(), {"roster": ["1=测试玩家"], "render_image": False})
        bot.latest_history = Mock(refresh=AsyncMock(return_value="已刷新"))
        bot.library = Mock(
            recent_matches_data=Mock(return_value=[]),
            recent_report=Mock(return_value="文本战绩"),
        )
        bot._resolve_account = AsyncMock(return_value="1")

        event = MockEvent()
        async for res in bot.recent(event, name="测试玩家"):
            pass

        self.assertEqual(len(event.images), 0)
        self.assertTrue(any("文本战绩" in t for t in event.texts))


class RecentMatchesRendererUnitTests(unittest.TestCase):
    def setUp(self):
        self.images = []

    def tearDown(self):
        for p in self.images:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_render_recent_matches_valid_png(self):
        matches = [
            {
                "match_id": "133654220",
                "start_time": 1794145771,
                "hero_id": 67,
                "kills": 7,
                "deaths": 3,
                "assists": 15,
                "outcome": 1,
                "duration_s": 2009,
                "net_worth": 35750,
                "last_hits": 114,
                "denies": 3,
                "match_mode": 4,
                "score": 65.4,
                "grade": "B",
            },
            {
                "match_id": "133634716",
                "start_time": 1794143163,
                "hero_id": 20,
                "kills": 2,
                "deaths": 8,
                "assists": 10,
                "outcome": 2,
                "duration_s": 2469,
                "net_worth": 37660,
                "last_hits": 131,
                "denies": 1,
                "match_mode": 4,
                "score": None,
                "grade": None,
            },
        ]
        path = render_recent_matches_image("417459099", "Tetra", matches)
        self.assertIsNotNone(path)
        self.images.append(path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            header = f.read(8)
            self.assertEqual(header, b"\x89PNG\r\n\x1a\n")

    def test_render_recent_matches_empty_returns_none(self):
        self.assertIsNone(render_recent_matches_image("417459099", "Tetra", []))
        self.assertIsNone(render_recent_matches_image("417459099", "Tetra", None))
