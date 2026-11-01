"""Regression coverage using real SQLite consumers, without AstrBot."""
import copy
from contextlib import closing
import gzip
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "deadbot_history_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
History = importlib.import_module(PACKAGE + ".history").History
Library = importlib.import_module(PACKAGE + ".library").Library
PlayerAnalysis = importlib.import_module(PACKAGE + ".analysis").PlayerAnalysis
PersonalInsights = importlib.import_module(PACKAGE + ".insights").PersonalInsights


class HistoryConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "history.db"
        self.history = History(path)
        self.library = Library(path)
        self.analysis = PlayerAnalysis(path)
        self.insights = PersonalInsights(path)
        self.row = dict(account_id=1, match_id=123, start_time=1000,
                        match_duration_s=1800, game_mode=1, match_mode=1,
                        player_match_outcome=1, hero_id=19, player_kills=9,
                        player_deaths=2, player_assists=8, net_worth=10000)
        self.library.import_history("1", [self.row])
        self.raw = {"match_info": dict(match_id=123, start_time=1000,
                    duration_s=1800, game_mode=1, match_mode=1, winning_team=1,
                    players=[dict(account_id=1, team=0, hero_id=19, kills=1,
                                  deaths=3, assists=4, net_worth=20000, stats=[])])}

    def assert_counts(self, count, wins, mode=1):
        analysis = self.analysis.calculate("1", mode)
        rows = [r for r in self.insights.rows("1") if r["match_mode"] == mode]
        self.assertEqual((analysis["count"], analysis["wins"]), (count, wins))
        self.assertEqual((len(rows), sum(r["player_match_outcome"] == 1 for r in rows)),
                         (count, wins))
        return analysis, rows

    def test_detail_overrides_history_and_refresh_invalidates_caches(self):
        self.assert_counts(1, 1)
        self.history.cache_detail("123", self.raw)
        analysis, rows = self.assert_counts(1, 0)
        self.assertEqual(analysis["kda_means"], [1, 3, 4])
        self.assertEqual(rows[0]["net_worth"], 20000)
        recap = self.insights.daily({"1": "Player"}, "1970-01-01")
        self.assertIn("0胜1负", recap)
        self.raw["match_info"]["winning_team"] = 0
        self.history.cache_detail("123", self.raw)
        self.assert_counts(1, 1)

    def test_exclusion_rules_are_shared(self):
        for change in ("abandon", "not_scored", "unknown_winner", "unknown_team",
                       "missing_player", "duplicate_player"):
            with self.subTest(change=change):
                raw = copy.deepcopy(self.raw)
                info = raw["match_info"]
                if change == "abandon":
                    info["players"][0]["abandon_match_time_s"] = 20
                elif change == "not_scored":
                    info["not_scored"] = True
                elif change == "unknown_winner":
                    info["winning_team"] = None
                elif change == "unknown_team":
                    info["players"][0]["team"] = None
                elif change == "missing_player":
                    info["players"] = []
                else:
                    info["players"] *= 2
                self.history.cache_detail("123", raw)
                self.assert_counts(0, 0)

    def test_detail_can_clear_stale_abandon_and_change_mode(self):
        self.row.update(abandoned_time_s=20, team_abandoned=True, not_scored=True)
        self.library.import_history("1", [self.row])
        self.assert_counts(0, 0)
        self.raw["match_info"]["match_mode"] = 4
        self.history.cache_detail("123", self.raw)
        self.assert_counts(0, 0)
        self.assert_counts(1, 0, mode=4)

    def test_corrupt_or_mismatched_detail_falls_back_to_history(self):
        self.history.cache_detail("123", self.raw)
        self.replace_blob(b"broken")
        self.assert_counts(1, 1)
        self.raw["match_info"]["match_id"] = 999
        self.replace_blob(gzip.compress(json.dumps(self.raw).encode("utf-8")))
        self.assert_counts(1, 1)

    def replace_blob(self, blob):
        with closing(self.history.connect()) as db, db:
            db.execute("UPDATE match_detail_cache SET compressed_json=?", (blob,))

    def test_detail_only_participation(self):
        with closing(self.library.connect()) as db, db:
            db.execute("DELETE FROM player_history")
        with closing(self.history.connect()) as db, db:
            db.execute("INSERT INTO deadlock_records VALUES(?,?,?,?)", ("123", "1", 1000, "{}"))
        self.history.cache_detail("123", self.raw)
        self.assert_counts(1, 0)

    def test_corrected_timestamp_is_used_before_latest_fifty_selection(self):
        rows = [dict(self.row, match_id=mid, start_time=mid)
                for mid in range(200, 250)]
        self.library.import_history("1", rows)
        self.raw["match_info"]["start_time"] = 1
        self.history.cache_detail("123", self.raw)
        result = self.analysis.calculate("1", 1)
        self.assertEqual(result["count"], 50)
        self.assertNotIn("123", result["match_ids"])
        self.assertEqual(self.insights.rows("1")[-1]["match_id"], "123")


if __name__ == "__main__":
    unittest.main()
