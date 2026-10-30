"""Real AstrBot registration/filter tests in a temporary subprocess sandbox.

The live plugin constructor, real history DB and network are never used. This
exercises decorators and command filters, not the platform wake/dispatch stack.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def registration_probe():
    import asyncio
    import importlib
    import inspect
    import math
    import sqlite3
    import time
    import types
    from unittest.mock import AsyncMock, Mock, patch

    # Protect against accidental access to any real database during framework
    # import. AstrBot may initialize its own empty DB under ASTRBOT_ROOT.
    sandbox = Path(os.environ["ASTRBOT_ROOT"]).resolve()

    def guard_database(event, args):
        if event == "sqlite3.connect" and str(args[0]) != ":memory:":
            if not Path(args[0]).resolve().is_relative_to(sandbox):
                raise AssertionError("Database access outside test sandbox")
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise AssertionError("Network access forbidden in command tests")

    sys.addaudithook(guard_database)
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.core.agent.message import TextPart
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    from test_scoring import make_match

    package = types.ModuleType("deadbot_command_fixture")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    modules = {name: importlib.import_module(package.__name__ + "." + name)
               for name in ("deadlock", "commands", "report", "achievements")}
    tree = ast.parse((ROOT / "main.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DeadBot")
    # Retain every actual handler and decorator. Only exclude initialization so
    # registration cannot construct History/Library or open live SQLite files.
    cls.bases = []
    cls.decorator_list = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name != "__init__"]
    namespace = {
        "__name__": "deadbot_isolated_registration", "filter": filter,
        "AstrMessageEvent": AstrMessageEvent, "TextPart": TextPart,
        "asyncio": asyncio, "json": json,
        "math": math, "sqlite3": sqlite3, "time": time,
        "parse_roster": modules["report"].parse_roster,
        "make_report": modules["deadlock"].make_report,
        "normalize": modules["deadlock"].normalize,
        "parse_match_id": importlib.import_module(package.__name__ + ".input").parse_match_id,
        "make_achievement_report": modules["achievements"].make_achievement_report,
        "text_pages": modules["commands"].text_pages,
        "VERSION": importlib.import_module(package.__name__ + ".scoring").VERSION,
    }
    import aiohttp
    namespace["aiohttp"] = aiohttp
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(ROOT / "main.py"), "exec"), namespace)
    plugin = namespace["DeadBot"]()
    handlers = [md for md in star_handlers_registry._handlers
                if md.handler_module_path == namespace["__name__"]]
    commands = [(md, f) for md in handlers for f in md.event_filters
                if isinstance(f, CommandFilter)]
    registered = [{"handler": md.handler_name, "command": f.command_name,
                   "aliases": sorted(f.alias), "params": list(f.handler_params)}
                  for md, f in commands]

    class Event:
        is_at_or_wake_command = True

        def __init__(self, message):
            self.message = message
            self.extra = {}

        def get_message_str(self):
            return self.message

        def set_extra(self, key, value):
            self.extra[key] = value

        def plain_result(self, text):
            return text

    async def dispatch(message):
        event = Event(message)
        matches = [(md, f) for md, f in commands if f.filter(event, {})]
        if len(matches) != 1:
            raise AssertionError(f"Expected one command handler, got {len(matches)}")
        md, _ = matches[0]
        params = event.extra["parsed_params"]
        call_start = len(private_calls)
        output = [item async for item in md.handler(plugin, event, **params)]
        return {"handler": md.handler_name, "params": params, "output": output,
                "private_calls": private_calls[call_start:]}

    private_calls = []
    async def exercise():
        raw = make_match()
        payload = modules["deadlock"].normalize(raw, "123456")
        plugin.config = {"roster": ["1001=测试玩家"]}
        plugin.cache = {"123456": (time.monotonic() + 300, payload)}
        plugin.lock = asyncio.Lock()
        plugin.next_request = 0
        plugin.history = Mock()
        plugin.library = Mock()
        plugin.library.latest_match.return_value = "123456"
        plugin._resolve_account = AsyncMock(return_value="1001")
        private_signature = inspect.signature(plugin._report)
        original_private_report = plugin._report

        async def tracked_report(event, match_id="", refresh="", *, achievements_only=False):
            private_calls.append({"match_id": match_id, "refresh": refresh,
                                  "achievements_only": achievements_only})
            async for response in original_private_report(event, match_id, refresh,
                                                          achievements_only=achievements_only):
                yield response

        plugin._report = tracked_report
        # Cached routes must not create an HTTP session; every history method
        # here is a Mock, so no real SQLite file can be opened.
        with patch.object(aiohttp, "ClientSession", side_effect=AssertionError("Unexpected HTTP request")):
            canonical = await dispatch("战报 123456")
            alias = await dispatch("评分详情 123456")
            latest = await dispatch("最新")
            achievements = await dispatch("成就 123456")
            linked = await dispatch("评分详情 https://statlocker.gg/profile/1001/matches/123456/summary")
            bad_refresh = await dispatch("战报 123456 invalid-refresh")
            extra_canonical = await dispatch("战报 123456 刷新 true")
            extra_alias = await dispatch("评分详情 123456 刷新 true")
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        response = Mock(status=200)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        response.json = AsyncMock(return_value=raw)
        session.get.return_value = response
        with patch.object(aiohttp, "ClientSession", return_value=session):
            refreshed = await dispatch("战报 123456 刷新")
            plugin.next_request = 0  # Independent refresh scenario, no real wait.
            alias_refreshed = await dispatch("评分详情 123456 刷新")
            plugin.next_request = 0
            achievement_refreshed = await dispatch("成就 123456 刷新")
        return {"canonical": canonical, "alias": alias, "latest": latest,
                "achievements": achievements, "linked": linked,
                "bad_refresh": bad_refresh, "refreshed": refreshed,
                "alias_refreshed": alias_refreshed,
                "achievement_refreshed": achievement_refreshed,
                "extra_canonical": extra_canonical, "extra_alias": extra_alias,
                "private_mode_kind": private_signature.parameters["achievements_only"].kind.name,
                "private_registered": any(md.handler_name == "_report" for md in handlers),
                "refresh_request_count": session.get.call_count,
                "refresh_request": {"url": session.get.call_args.args[0],
                                    "kwargs": session.get.call_args.kwargs},
                "expected_achievements": modules["commands"].text_pages(
                    modules["achievements"].make_achievement_report(payload, {"1001": "测试玩家"}), "成就"),
                "latest_resolved": plugin._resolve_account.await_count,
                "registered": registered,
                "roster_hooks": sum(md.handler_name == "current_roster_context"
                                    and md.event_type == EventType.OnLLMRequestEvent for md in handlers)}

    print("DEADBOT_PROBE=" + json.dumps(asyncio.run(exercise()), ensure_ascii=False))


@unittest.skipUnless(importlib.util.find_spec("astrbot"), "需要已有 AstrBot 环境验证真实命令注册")
class CommandRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="deadbot-command-tests-") as directory:
            env = os.environ.copy()
            env.update(ASTRBOT_ROOT=directory, PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe"],
                                    cwd=directory, env=env, text=True, capture_output=True, timeout=60)
            if result.returncode:
                raise AssertionError("Isolated AstrBot command test failed:\n" + result.stdout + result.stderr)
            line = next(line for line in result.stdout.splitlines() if line.startswith("DEADBOT_PROBE="))
            cls.probe = json.loads(line.split("=", 1)[1])

    def test_report_is_the_only_registered_report_handler(self):
        entries = [item for item in self.probe["registered"]
                   if item["command"] in {"战报", "评分详情"} or "评分详情" in item["aliases"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["command"], "战报")
        self.assertEqual(entries[0]["handler"], "report")
        self.assertEqual(entries[0]["aliases"], ["评分详情"])
        self.assertEqual(entries[0]["params"], ["match_id", "refresh"])

    def test_alias_and_canonical_use_identical_flow_and_output(self):
        self.assertEqual(self.probe["canonical"], self.probe["alias"])
        self.assertIn("DeadBot 战报", "\n".join(self.probe["canonical"]["output"]))

    def test_refresh_is_parsed_and_keeps_existing_request_options(self):
        for name in ("refreshed", "alias_refreshed"):
            with self.subTest(entry=name):
                refreshed = self.probe[name]
                self.assertEqual(refreshed["handler"], "report")
                self.assertEqual(refreshed["params"], {"match_id": "123456", "refresh": "刷新"})
                self.assertEqual(refreshed["output"][-1], self.probe["canonical"]["output"][-1])
        self.assertEqual(self.probe["refresh_request"], {"url": "https://api.deadlock-api.com/v1/matches/123456/metadata", "kwargs": {"params": {"disable_steam": "true"}, "allow_redirects": False}})
        self.assertEqual(self.probe["refresh_request_count"], 3)

    def test_requested_public_parameter_matrix(self):
        # Eight requested command forms, parsed by the actual CommandFilter.
        for name, handler, refresh in [
            ("canonical", "report", ""), ("refreshed", "report", "刷新"),
            ("alias", "report", ""), ("alias_refreshed", "report", "刷新"),
            ("extra_canonical", "report", "刷新"), ("extra_alias", "report", "刷新"),
            ("achievements", "award_details", ""), ("achievement_refreshed", "award_details", "刷新"),
        ]:
            with self.subTest(entry=name):
                result = self.probe[name]
                self.assertEqual(result["handler"], handler)
                self.assertEqual(result["params"], {"match_id": "123456", "refresh": refresh})
                if name.startswith("extra_"):
                    self.assertEqual(result["private_calls"], [])
                else:
                    self.assertEqual(result["private_calls"], [{"match_id": "123456", "refresh": refresh,
                                                               "achievements_only": handler == "award_details"}])

    def test_surplus_parameters_are_rejected_before_query_or_rendering(self):
        # AstrBot discards surplus tokens; the public wrapper must reject the
        # original input, rather than accepting the filter's shortened params.
        for name in ("extra_canonical", "extra_alias"):
            with self.subTest(entry=name):
                result = self.probe[name]
                self.assertEqual(len(result["output"]), 1)
                self.assertIn("不接受额外参数", result["output"][0])
                self.assertEqual(result["private_calls"], [])

    def test_private_mode_is_keyword_only_and_not_registered(self):
        self.assertEqual(self.probe["private_mode_kind"], "KEYWORD_ONLY")
        self.assertFalse(self.probe["private_registered"])

    def test_statlocker_link_keeps_report_compatibility(self):
        self.assertEqual(self.probe["linked"]["handler"], "report")
        self.assertEqual(self.probe["linked"]["output"], self.probe["canonical"]["output"])

    def test_invalid_refresh_is_still_rejected(self):
        self.assertIn("用法：/战报", self.probe["bad_refresh"]["output"][0])

    def test_latest_still_resolves_account_and_uses_report(self):
        self.assertEqual(self.probe["latest"]["handler"], "latest")
        self.assertEqual(self.probe["latest_resolved"], 1)
        self.assertEqual(self.probe["latest"]["output"], self.probe["canonical"]["output"])
        self.assertEqual(self.probe["latest"]["private_calls"], [{"match_id": "123456", "refresh": "", "achievements_only": False}])

    def test_achievements_still_use_achievement_only_output(self):
        expected = self.probe["expected_achievements"]
        for name in ("achievements", "achievement_refreshed"):
            with self.subTest(entry=name):
                self.assertEqual(self.probe[name]["handler"], "award_details")
                self.assertEqual(self.probe[name]["output"][-len(expected):], expected)
                self.assertNotEqual(self.probe[name]["output"][-1], self.probe["canonical"]["output"][-1])

    def test_roster_hook_remains_registered(self):
        self.assertEqual(self.probe["roster_hooks"], 1)


class CommandSourceTests(unittest.TestCase):
    def test_dead_detailed_parameter_and_duplicate_handler_are_removed(self):
        tree = ast.parse((ROOT / "main.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DeadBot")
        self.assertFalse(any(getattr(n, "name", "") == "score_details" for n in cls.body))
        renderer = ast.parse((ROOT / "scored_report.py").read_text())
        render = next(n for n in renderer.body if isinstance(n, ast.FunctionDef) and n.name == "make_scored_report")
        self.assertNotIn("detailed", [arg.arg for arg in render.args.args])


if __name__ == "__main__":
    if "--probe" in sys.argv:
        registration_probe()
    else:
        unittest.main()
