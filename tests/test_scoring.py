"""Pure unittest coverage of the existing scoring model; no AstrBot or I/O."""
import copy
import importlib.util
import math
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deadbot_scoring_test", ROOT / "scoring.py")
scoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring)


def make_match(duration=1800, game_mode=1, match_mode=1, snapshots=None,
               teams=None, player_overrides=None, final_overrides=None):
    """Build twelve fictional players; all mutable fixtures are independent.

    Overrides are indexed by player position, not real account identities.
    snapshots replaces the common snapshot timestamps for all players.
    """
    if duration <= 0:
        raise ValueError("The normal fixture requires a positive duration")
    stamps = sorted(set(snapshots if snapshots is not None else
                        ([600, 1200, duration] if duration > 1200 else [min(600, duration / 2), duration])))
    teams = teams if teams is not None else [0] * 6 + [1] * 6
    # Damage, farming, tanking, support, pushing and balanced profiles. Each
    # side has 12 kills and 12 deaths; metrics vary independently by role.
    profiles = [
        (4, 3, 30000, 2500, 140, 18000, 2500, 500, 700, 300, 1000, 9000),
        (1, 7, 14000, 8000, 240, 15000, 1800, 1200, 1800, 500, 800, 13000),
        (2, 2, 10000, 900, 100, 40000, 15000, 7000, 1300, 3500, 1200, 7000),
        (0, 8, 8000, 1800, 150, 22000, 5000, 2000, 12000, 7000, 3000, 6000),
        (3, 4, 16000, 11000, 220, 25000, 6000, 2500, 1500, 1200, 800, 12000),
        (2, 5, 20000, 4000, 180, 20000, 3500, 1000, 3000, 1500, 2200, 10000),
    ]
    role_order = [0, 1, 2, 3, 4, 5, 3, 0, 5, 1, 2, 4]
    players = []
    for index, role in enumerate(role_order):
        (kills, assists, damage, boss, creeps, taken, mitigated, absorbed,
         healing, barrier, prevented, farm) = profiles[role]
        deaths = [
            {"game_time_s": duration / 6, "death_duration_s": min(8 + role, duration / 12)},
            {"game_time_s": duration / 2, "death_duration_s": min(16 + role, duration / 12)},
        ]
        stats = []
        for stamp in stamps:
            scale = stamp / duration
            row = {
                "time_stamp_s": stamp,
                "player_damage": damage * scale,
                "assists": int(assists * scale),
                "boss_damage": boss * scale,
                "creep_kills": int(creeps * scale),
                "player_damage_taken": taken * scale,
                "damage_mitigated": mitigated * scale,
                "damage_absorbed": absorbed * scale,
                "teammate_healing": healing * scale,
                "teammate_barriering": barrier * scale,
                "heal_prevented": prevented * scale,
                "deaths": sum(row["game_time_s"] <= stamp for row in deaths),
                "gold_sources": [
                    {"source": 2, "gold": farm * 2 / 3 * scale, "gold_orbs": None},
                    {"source": 3, "gold": farm / 3 * scale, "gold_orbs": 0},
                ],
            }
            stats.append(row)
        if stats:
            stats[-1].update(copy.deepcopy((final_overrides or {}).get(index, {})))
        player = {
            "account_id": 1001 + index, "hero_id": index + 1,
            "team": teams[index], "kills": kills, "assists": assists,
            "deaths": len(deaths), "denies": role, "abandon_match_time_s": 0,
            "stats": stats, "death_details": deaths,
        }
        player.update(copy.deepcopy((player_overrides or {}).get(index, {})))
        players.append(player)
    return {"match_info": {"match_id": 123456, "game_mode": game_mode,
                           "match_mode": match_mode, "duration_s": duration,
                           "winning_team": 0, "players": players}}


def make_equal_match():
    """Hand-calculable 30-minute match, independent of scoring constants.

    Every metric has ratio 1. Non-auxiliary weights total 90; healing,
    barriers and prevention have weights 4/3/3 and attenuation exactly 1/2.
    Thus each base score is 90*0.6 + 10*0.6/2 = 57.
    """
    raw = make_match()
    for player in raw["match_info"]["players"]:
        player.update(kills=2, assists=4, deaths=2)
        player["death_details"] = [
            {"game_time_s": 200, "death_duration_s": 8},
            {"game_time_s": 600, "death_duration_s": 32},
        ]
        for row in player["stats"]:
            stamp = row["time_stamp_s"]
            scale = stamp / 1800
            row.update(
                player_damage={600: 500, 1200: 1000, 1800: 2000}[stamp],
                assists={600: 1, 1200: 2, 1800: 4}[stamp],
                boss_damage=100 * scale, creep_kills=int(120 * scale),
                player_damage_taken=100 * scale, damage_mitigated=100 * scale,
                damage_absorbed=100 * scale, teammate_healing=1500 * scale,
                teammate_barriering=600 * scale, heal_prevented=1500 * scale,
                deaths=sum(death["game_time_s"] <= stamp for death in player["death_details"]),
                gold_sources=[{"source": 2, "gold": 6000 * scale, "gold_orbs": None},
                              {"source": 3, "gold": 3000 * scale, "gold_orbs": 0}],
            )
    return raw


class ScoringTests(unittest.TestCase):
    def assert_scored(self, result):
        self.assertTrue(result["eligible"])
        self.assertEqual(len(result["players"]), 12)
        for team in result["teams"]:
            self.assertTrue(team["eligible"])
            self.assertEqual(len(team["players"]), 6)
        for player in result["players"]:
            self.assertTrue(math.isfinite(player["score"]))
            self.assertGreaterEqual(player["score"], 0)
            self.assertLessEqual(player["score"], 100)

    def assert_unscored(self, result, note):
        self.assertFalse(result["eligible"])
        self.assertFalse(result["normalization"])
        for team in result["teams"]:
            self.assertFalse(team["eligible"])
            self.assertIn(note, team["notes"])
        self.assertTrue(all(p["score"] is None for p in result["players"]))

    def test_complete_standard_and_ranked_matches_are_scored(self):
        for mode in (1, 4):
            with self.subTest(mode=mode):
                self.assert_scored(scoring.score_match(make_match(match_mode=mode)))

    def test_repeated_input_is_deterministic_and_not_mutated(self):
        raw = make_match()
        before = copy.deepcopy(raw)
        first = scoring.score_match(raw)
        self.assertEqual(first, scoring.score_match(raw))
        self.assertEqual(raw, before)

    def test_extreme_valid_contribution_stays_finite_and_bounded(self):
        raw = make_match(final_overrides={0: {"player_damage": 10 ** 12}})
        result = scoring.score_match(raw)
        self.assert_scored(result)
        self.assertGreater(result["players"][0]["score"], 90)

    def test_unsupported_modes_do_not_score(self):
        for game_mode, match_mode in [(2, 1), (1, 2), (None, 1), (1, None)]:
            with self.subTest(game_mode=game_mode, match_mode=match_mode):
                self.assert_unscored(scoring.score_match(make_match(game_mode=game_mode, match_mode=match_mode)), "unsupported_match_mode")

    def test_explicit_not_scored_marker_is_respected(self):
        raw = make_match()
        raw["match_info"]["not_scored"] = True
        self.assert_unscored(scoring.score_match(raw), "match_not_scored")

    def test_missing_player_or_unbalanced_teams_are_rejected(self):
        short = make_match()
        short["match_info"]["players"].pop()
        for raw in (short, make_match(teams=[0] * 7 + [1] * 5),
                    make_match(teams=[0] * 6 + [1] * 5 + [2])):
            with self.subTest(teams=[p["team"] for p in raw["match_info"]["players"]]):
                self.assert_unscored(scoring.score_match(raw), "invalid_human_teams")

    def test_duplicate_accounts_and_boolean_team_are_rejected(self):
        for override in ({"account_id": 1002}, {"team": False}):
            with self.subTest(override=override):
                self.assert_unscored(scoring.score_match(make_match(player_overrides={0: override})), "invalid_human_teams")

    def test_abandonment_disqualifies_the_whole_match(self):
        for index in (0, 6):
            with self.subTest(team=index // 6):
                self.assert_unscored(scoring.score_match(make_match(player_overrides={index: {"abandon_match_time_s": 900}})), "abandoned_player")

    def test_missing_player_snapshots_are_rejected(self):
        self.assert_unscored(scoring.score_match(make_match(player_overrides={0: {"stats": []}})), "missing_snapshots")

    def test_stale_final_snapshot_is_rejected(self):
        self.assert_unscored(scoring.score_match(make_match(snapshots=[600, 1200, 1679])), "stale_final_snapshot")

    def test_long_match_requires_common_twenty_minute_snapshot(self):
        self.assert_unscored(scoring.score_match(make_match(snapshots=[600, 1800])), "missing_common_20m_snapshot")
        raw = make_match()
        raw["match_info"]["players"][0]["stats"].pop(1)
        self.assert_unscored(scoring.score_match(raw), "missing_common_20m_snapshot")

    def test_1200_second_boundary_and_weight_transfer(self):
        for duration in (1199, 1200, 1201):
            with self.subTest(duration=duration):
                result = scoring.score_match(make_match(duration=duration))
                self.assert_scored(result)
                for team in result["teams"]:
                    weights = team["effective_weights"]
                    self.assertEqual(team["boundary_s"], min(duration, 1200))
                    self.assertAlmostEqual(sum(weights.values()), sum(scoring.WEIGHTS.values()))
                    if duration <= 1200:
                        self.assertIn("short_match_weights_transferred", team["notes"])
                        self.assertEqual(weights["late_damage"], 0)
                        self.assertEqual(weights["late_assists"], 0)
                        self.assertEqual(weights["early_damage"], scoring.WEIGHTS["early_damage"] + scoring.WEIGHTS["late_damage"])
                        self.assertEqual(weights["early_assists"], scoring.WEIGHTS["early_assists"] + scoring.WEIGHTS["late_assists"])
                    else:
                        self.assertNotIn("short_match_weights_transferred", team["notes"])
                        self.assertEqual(weights, scoring.WEIGHTS)

    def test_nearest_boundary_tie_prefers_pre_twenty_minutes(self):
        result = scoring.score_match(make_match(snapshots=[600, 1140, 1260, 1800]))
        self.assert_scored(result)
        for team in result["teams"]:
            self.assertEqual(team["boundary_s"], 1140)
            self.assertIn("approximate_boundary:1140", team["notes"])

    def test_missing_or_decreasing_core_metrics_are_rejected(self):
        for value in (None, -1, float("nan"), float("inf"), True, "12000", 1):
            with self.subTest(value=value):
                self.assert_unscored(scoring.score_match(make_match(final_overrides={0: {"player_damage": value}})), "missing_or_invalid_core_metrics")
        self.assert_unscored(scoring.score_match(make_match(final_overrides={0: {"assists": None}})), "missing_or_invalid_core_metrics")

    def test_missing_settlement_kills_are_rejected(self):
        self.assert_unscored(scoring.score_match(make_match(player_overrides={0: {"kills": None}})), "missing_settlement_kills")

    def test_optional_missing_metric_is_excluded_for_every_player(self):
        # A missing value is unknown for the entire comparison, not a zero for
        # one player. Support weight transfers to remaining support metrics.
        for value in (None, -1, float("nan")):
            with self.subTest(value=value):
                result = scoring.score_match(make_match(final_overrides={0: {"teammate_healing": value}}))
                self.assert_scored(result)
                for team in result["teams"]:
                    self.assertEqual(team["missing_metrics"], ["teammate_healing"])
                    weights = team["effective_weights"]
                    self.assertEqual(weights["teammate_healing"], 0)
                    self.assertAlmostEqual(sum(weights.values()), sum(scoring.WEIGHTS.values()))
                    self.assertGreater(weights["late_assists"], scoring.WEIGHTS["late_assists"])
                    self.assertEqual(weights["early_damage"], scoring.WEIGHTS["early_damage"])
                for player in result["players"]:
                    self.assertIsNone(player["metrics"]["teammate_healing"])
                    self.assertIsNone(player["components"]["teammate_healing"]["value"])
                    self.assertEqual(player["components"]["teammate_healing"]["points"], 0)

    def test_absent_optional_field_has_same_rules_as_explicit_null(self):
        raw = make_match()
        del raw["match_info"]["players"][0]["stats"][-1]["teammate_healing"]
        self.assertEqual(scoring.score_match(raw), scoring.score_match(make_match(final_overrides={0: {"teammate_healing": None}})))

    def test_missing_entire_category_redistributes_to_other_categories(self):
        result = scoring.score_match(make_match(final_overrides={0: {"boss_damage": None, "creep_kills": None}}))
        self.assert_scored(result)
        for team in result["teams"]:
            weights = team["effective_weights"]
            self.assertEqual(weights["boss_damage"], 0)
            self.assertEqual(weights["creep_kills"], 0)
            self.assertGreater(weights["early_damage"], scoring.WEIGHTS["early_damage"])
            self.assertAlmostEqual(sum(weights.values()), sum(scoring.WEIGHTS.values()))

    def test_excessive_missing_optional_weight_rejects_scoring(self):
        missing = {"boss_damage": None, "creep_kills": None, "player_damage_taken": None, "damage_mitigated": None}
        self.assert_unscored(scoring.score_match(make_match(final_overrides={0: missing})), "insufficient_metric_coverage")

    def test_unknown_farm_sources_disable_bonus_in_that_team_only(self):
        result = scoring.score_match(make_match(final_overrides={0: {"gold_sources": None}}))
        self.assert_scored(result)
        self.assertIn("missing_farm_sources", result["teams"][0]["notes"])
        for player in result["teams"][0]["players"]:
            self.assertIsNone(player["farm_share"])
            self.assertEqual(player["economy_bonus"], 0)
        self.assertTrue(all(p["farm_share"] is not None for p in result["teams"][1]["players"]))

    def test_missing_or_invalid_death_details_are_rejected(self):
        for details in (None, [], [{"game_time_s": 200, "death_duration_s": -1}]):
            with self.subTest(details=details):
                self.assert_unscored(scoring.score_match(make_match(player_overrides={0: {"death_details": details}})), "missing_death_details")

    def test_invalid_duration_is_rejected(self):
        for duration in (0, -1, None, float("nan")):
            with self.subTest(duration=duration):
                raw = make_match()
                raw["match_info"]["duration_s"] = duration
                self.assert_unscored(scoring.score_match(raw), "invalid_duration")


class FixtureTests(unittest.TestCase):
    def test_normal_fixtures_have_consistent_teams_kills_and_deaths(self):
        for raw in [make_match(duration=d) for d in (300, 1199, 1200, 1201, 1800)] + [make_equal_match()]:
            info = raw["match_info"]
            players = info["players"]
            with self.subTest(duration=info["duration_s"]):
                self.assertEqual(len({p["account_id"] for p in players}), 12)
                for team in (0, 1):
                    members = [p for p in players if p["team"] == team]
                    enemies = [p for p in players if p["team"] != team]
                    self.assertEqual(len(members), 6)
                    self.assertEqual(sum(p["kills"] for p in members), sum(p["deaths"] for p in enemies))
                for player in players:
                    self.assertEqual(player["deaths"], len(player["death_details"]))
                    previous_respawn = 0
                    for death in player["death_details"]:
                        self.assertGreaterEqual(death["game_time_s"], previous_respawn)
                        previous_respawn = death["game_time_s"] + death["death_duration_s"]
                        self.assertLessEqual(previous_respawn, info["duration_s"])
                    for row in player["stats"]:
                        self.assertLessEqual(row["time_stamp_s"], info["duration_s"])
                        self.assertEqual(row["deaths"], sum(d["game_time_s"] <= row["time_stamp_s"] for d in player["death_details"]))

    def test_normal_fixture_has_different_contribution_profiles(self):
        players = make_match()["match_info"]["players"][:6]
        for field, expected_index in [("player_damage", 0), ("player_damage_taken", 2),
                                      ("teammate_healing", 3), ("boss_damage", 4)]:
            with self.subTest(field=field):
                self.assertEqual(max(range(6), key=lambda i: players[i]["stats"][-1][field]), expected_index)


class NumericScoringTests(unittest.TestCase):
    def test_ratio_points_fixed_examples(self):
        # Literal values from 0.6 * value / mean, not the tested implementation.
        for value, mean, expected in [(0, 10, 0), (5, 10, 0.3),
                                      (10, 10, 0.6), (20, 10, 1.2)]:
            with self.subTest(value=value, mean=mean):
                self.assertAlmostEqual(scoring._ratio_points(value, mean), expected, places=12)

    def test_equal_contributions_have_expected_component_and_base_scores(self):
        result = scoring.score_match(make_equal_match())
        self.assertTrue(result["eligible"])
        expected = {"early_damage": 7.2, "early_assists": 3.6, "late_damage": 7.2,
                    "kills": 12, "boss_damage": 7.2, "creep_kills": 1.8,
                    "player_damage_taken": 6, "damage_mitigated": 1.8,
                    "damage_absorbed": 1.2, "late_assists": 6,
                    "teammate_healing": 1.2, "teammate_barriering": 0.9, "heal_prevented": 0.9}
        for player in result["players"]:
            self.assertAlmostEqual(player["base_score"], 57, places=10)
            for field, points in expected.items():
                self.assertAlmostEqual(player["components"][field]["points"], points, places=10)

    def test_death_time_cost_penalty_and_raw_score(self):
        result = scoring.score_match(make_equal_match())
        # Cost: 135*sqrt(8/8) + 135*sqrt(32/8) = 405.
        # Equal costs/base scores -> penalty = 40*40/1800 + 5 = 53/9.
        for player in result["players"]:
            self.assertAlmostEqual(player["dead_seconds"], 40, places=10)
            self.assertAlmostEqual(player["death_cost"], 405, places=10)
            self.assertAlmostEqual(player["death_penalty"], 53 / 9, places=10)
            self.assertAlmostEqual(player["raw_score"], 460 / 9, places=10)

    def test_death_time_is_clipped_at_match_end_but_cost_is_not(self):
        raw = make_equal_match()
        raw["match_info"]["players"][0]["death_details"][1]["game_time_s"] = 1790
        # Keep snapshot death counters consistent with the later death.
        for row in raw["match_info"]["players"][0]["stats"]:
            row["deaths"] = 2 if row["time_stamp_s"] >= 1790 else 1
        player = scoring.score_match(raw)["players"][0]
        self.assertAlmostEqual(player["dead_seconds"], 18, places=10)  # 8 + min(32, 10)
        self.assertAlmostEqual(player["death_cost"], 405, places=10)
        self.assertAlmostEqual(player["death_penalty"], 5.4, places=10)  # 40*18/1800 + 5

    @staticmethod
    def half_farm_match():
        raw = make_equal_match()
        for row in raw["match_info"]["players"][0]["stats"]:
            for source in row["gold_sources"]:
                source["gold"] /= 2
        return raw

    def test_economy_reward_uses_independent_contribution_and_farm_shares(self):
        result = scoring.score_match(self.half_farm_match())
        player = result["players"][0]
        # Equal base scores -> share 1/6. 4500/(4500+5*9000) = 1/11.
        # Bonus = 5*(1-(1/11)/(1/6)) = 25/11.
        self.assertAlmostEqual(player["farm_souls"], 4500, places=10)
        self.assertAlmostEqual(player["contribution_share"], 1 / 6, places=10)
        self.assertAlmostEqual(player["farm_share"], 1 / 11, places=10)
        self.assertAlmostEqual(player["economy_bonus"], 25 / 11, places=10)
        self.assertAlmostEqual(player["raw_score"], 460 / 9 + 25 / 11, places=10)
        for other in result["players"][1:]:
            self.assertAlmostEqual(other["economy_bonus"], 0, places=10)

    def test_equal_raw_scores_normalize_to_fifty(self):
        result = scoring.score_match(make_equal_match())
        self.assertAlmostEqual(result["normalization"]["mean"], 460 / 9, places=10)
        self.assertAlmostEqual(result["normalization"]["std"], 0, places=10)
        self.assertEqual(result["normalization"]["effective_std"], 12)
        for player in result["players"]:
            self.assertAlmostEqual(player["z_score"], 0, places=10)
            self.assertAlmostEqual(player["score"], 50, places=10)

    def test_normalization_floor_with_one_economy_reward(self):
        result = scoring.score_match(self.half_farm_match())
        self.assertEqual(result["normalization"]["effective_std"], 12)
        # One raw score gains 25/11. Its centered delta is (25/11)*11/12.
        # With std floor 12, normalized top = 50 + 625/144.
        self.assertAlmostEqual(result["players"][0]["score"], 50 + 625 / 144, places=10)
        for player in result["players"][1:]:
            self.assertAlmostEqual(player["score"], 50 - 625 / 1584, places=10)

    def test_population_std_and_high_score_tail(self):
        raw = make_equal_match()
        raw["match_info"]["players"][0]["stats"][-1]["player_damage"] = 23000
        # Isolate normalization from farm rewards using the supported missing
        # farm-data rule. All other metrics/costs remain identical.
        for player in raw["match_info"]["players"]:
            for row in player["stats"]:
                row["gold_sources"] = None
        result = scoring.score_match(raw)
        # Late damage ratios are 8 (top) and 4/11 (others); base scores
        # become 107.4 and 2883/55. Top contribution factor is clamped to .75.
        top_raw = 107.4 - 8 / 9 - 3.75
        other_raw = 2883 / 55 - 8 / 9 - 5 * math.sqrt(57 / (2883 / 55))
        expected_mean = (top_raw + 11 * other_raw) / 12
        expected_std = (top_raw - other_raw) * math.sqrt(11) / 12
        self.assertGreater(expected_std, 12)
        self.assertAlmostEqual(result["normalization"]["mean"], expected_mean, places=10)
        self.assertAlmostEqual(result["normalization"]["std"], expected_std, places=10)
        # A population of one high + eleven equal scores has z=sqrt(11)
        # for the high score and z=-1/sqrt(11) for each other score.
        expected_top = 100 - 10 * math.exp(-(25 * math.sqrt(11) - 40) / 10)
        self.assertAlmostEqual(result["players"][0]["score"], expected_top, places=10)
        for player in result["players"][1:]:
            self.assertAlmostEqual(player["score"], 50 - 25 / math.sqrt(11), places=10)

    def test_numeric_base_anchor_detects_ratio_scale_mutation(self):
        original = scoring._ratio_points
        with patch.object(scoring, "_ratio_points", side_effect=lambda value, mean: original(value, mean) * 1.5):
            changed = scoring.score_match(make_equal_match())
            # This is the same independent assertion as the base-score test;
            # a 50% numerical mutation must cause it to fail.
            with self.assertRaises(AssertionError):
                self.assertAlmostEqual(changed["players"][0]["base_score"], 57, places=10)


if __name__ == "__main__":
    unittest.main()
