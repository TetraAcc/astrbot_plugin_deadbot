import importlib
import unittest
from test_history_consistency import PACKAGE
from test_scoring import scoring, make_match

grade = importlib.import_module(PACKAGE + ".scored_report").grade


class ModelV4Tests(unittest.TestCase):
    def test_specialist_support_is_bounded_without_erasing_contribution(self):
        result = scoring.score_match(make_match(final_overrides={0: {
            "teammate_healing": 10**9}}))
        self.assertTrue(result["eligible"])
        player = result["players"][0]
        component = player["components"]["teammate_healing"]
        self.assertGreater(component["points"], 0)
        self.assertLess(component["points"], 1.2 * component["weight"])

    def test_zero_metric_budget_does_not_only_boost_support(self):
        weights = {"late_assists": 10, "teammate_healing": 10,
                   "teammate_barriering": 10, "kills": 10}
        adjusted = scoring._redistribute(weights, {"late_assists", "teammate_healing", "kills"},
                                         {"teammate_barriering"})
        self.assertEqual(adjusted["teammate_barriering"], 0)
        self.assertAlmostEqual(adjusted["kills"], 40 / 3)
        self.assertAlmostEqual(adjusted["teammate_healing"], 40 / 3)
        self.assertAlmostEqual(sum(adjusted.values()), 40)

    def test_reference_budget_and_reduced_kill_dependence(self):
        self.assertAlmostEqual(sum(scoring.REFERENCE_WEIGHTS.values()), 78.2)
        self.assertAlmostEqual(sum(scoring.WEIGHTS.values()), 100)
        self.assertLess(scoring.WEIGHTS["kills"], 20)
        self.assertGreater(scoring.WEIGHTS["teammate_healing"], 4)
        self.assertEqual(scoring.WEIGHTS["damage_mitigated"], 0)
        self.assertEqual(scoring.WEIGHTS["damage_absorbed"], 0)

    def test_grade_boundaries_and_display_rounding(self):
        boundaries = [(96.6,"S+"),(93.3,"S"),(90,"S-"),(85,"A+"),
                      (80,"A"),(75,"A-"),(70,"B+"),(65,"B"),(57.5,"B-"),
                      (50,"C+"),(45,"C"),(40,"C-"),(35,"D+"),(30,"D"),
                      (25,"D-"),(20,"E+"),(15,"E"),(10,"E-")]
        for i, (threshold, label) in enumerate(boundaries):
            with self.subTest(label=label):
                self.assertEqual(grade(threshold), label)
                self.assertEqual(grade(threshold - .1),
                                 boundaries[i+1][1] if i+1<len(boundaries) else "F")
        self.assertEqual(grade(96.56), "S+")
        self.assertEqual(grade(100), "S+")
        self.assertEqual(grade(0), "F")
        for value in (None, float("nan"), float("inf"), True):
            self.assertEqual(grade(value), "未知")

    def test_zero_weight_fields_cannot_inflate_score(self):
        baseline = scoring.score_match(make_match())
        changed = scoring.score_match(make_match(final_overrides={0: {
            "damage_mitigated": 10**12, "damage_absorbed": 10**12}}))
        self.assertEqual([p["score"] for p in baseline["players"]],
                         [p["score"] for p in changed["players"]])

    def test_version_invalidates_previous_score_cache(self):
        self.assertNotEqual(scoring.VERSION, "deadbot-contribution-v3")
        self.assertEqual(scoring.score_match(make_match())["version"], scoring.VERSION)
