import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Keep these unit tests independent of optional runtime packages.
if "bs4" not in sys.modules:
    bs4 = types.ModuleType("bs4")
    bs4.BeautifulSoup = object
    sys.modules["bs4"] = bs4

MODULE_PATH = Path(__file__).with_name("football_bot.py")
SPEC = importlib.util.spec_from_file_location("football_bot", MODULE_PATH)
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


class FootballBotTests(unittest.TestCase):
    def test_structured_picks_are_parsed(self):
        report = """## MACHINE READABLE PICKS
```json
[
  {
    "team": "Juventus",
    "opponent": "Nice",
    "score": 8.5,
    "assessed_probability": 0.85
  }
]
```"""
        self.assertEqual(
            bot.parse_recommendations(report),
            [{
                "team": "Juventus",
                "opponent": "Nice",
                "score": 8.5,
                "assessed_probability": 0.85,
            }],
        )

    def test_validation_rejects_ai_pick_when_python_baseline_is_negative(self):
        candidates = [{
            "team": "PAOK",
            "score": 8.5,
            "assessed_probability": 0.583,
        }]
        matches = [{
            "team1": "PAOK",
            "team2": "Dynamo Kyiv",
            "home_odds": 1.8,
            "away_odds": 4.5,
            "draw_odds": 3.5,
            "home_form": "WWLLD",
            "away_form": "LLLDD",
            "home_record": "10-5-5",
            "away_record": "5-5-10",
        }]
        validated = bot.validate_recommendations(candidates, matches)

        self.assertEqual(validated, [])

    def test_validation_uses_verified_team_odds_and_grade(self):
        candidates = [{
            "team": "Rangers",
            "score": 8.3,
            "assessed_probability": 0.83,
        }]
        matches = [{
            "team1": "Dundee United",
            "team2": "Rangers",
            "home_odds": 5.5,
            "away_odds": 1.526,
            "draw_odds": 4.2,
            "home_form": "LLLLL",
            "away_form": "WWWWW",
            "home_record": "3-4-13",
            "away_record": "15-3-2",
            "tournament": "SPFL Premiership",
        }]
        validated = bot.validate_recommendations(candidates, matches)

        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["grade"], "Value Pick")
        self.assertEqual(validated[0]["odds"], 1.526)
        self.assertAlmostEqual(validated[0]["ev"], 0.052121)
        self.assertAlmostEqual(
            validated[0]["assessed_probability"],
            0.6894633,
        )

    def test_statistical_baseline_devigs_three_way_market(self):
        match = {
            "team1": "Home",
            "team2": "Away",
            "home_odds": 2.0,
            "draw_odds": 4.0,
            "away_odds": 4.0,
            "home_form": "WWWWW",
            "away_form": "LLLLL",
            "home_record": "8-1-1",
            "away_record": "1-1-8",
        }
        baseline = bot.calculate_team_baseline(match, "Home")

        self.assertAlmostEqual(baseline["market_probability"], 0.5)
        self.assertEqual(baseline["evidence_adjustment"], 0.08)
        self.assertAlmostEqual(baseline["assessed_probability"], 0.58)
        self.assertAlmostEqual(baseline["ev"], 0.16)

    def test_statistical_candidate_scan_cannot_be_omitted_by_ai(self):
        match = {
            "team1": "Home",
            "team2": "Away",
            "home_odds": 2.0,
            "draw_odds": 4.0,
            "away_odds": 4.0,
            "home_form": "WWWWW",
            "away_form": "LLLLL",
            "home_record": "8-1-1",
            "away_record": "1-1-8",
        }
        candidates = bot.build_statistical_candidates([match], 1.5, 3.0)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["team"], "Home")
        self.assertAlmostEqual(candidates[0]["assessed_probability"], 0.58)

    def test_team_normalization_handles_missing_accents(self):
        self.assertEqual(
            bot.normalize_team_name("Atlético de San Luis"),
            bot.normalize_team_name("Atletico de San Luis"),
        )

    def test_log_bets_deduplicates_team_and_date(self):
        match = {
            "team1": "Dundee United",
            "team2": "Rangers",
            "tournament": "SPFL Premiership",
        }
        recommendation = {
            "team": "Rangers",
            "grade": "Top Pick",
            "odds": 1.541,
            "match": match,
        }
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "bets-log.csv"
            with patch.object(bot, "LOG_FILE", log_path):
                first = bot.log_bets(
                    "2026-07-31",
                    [recommendation],
                    [match],
                    100.0,
                )
                second = bot.log_bets(
                    "2026-07-31",
                    [recommendation],
                    [match],
                    97.0,
                )
            rows = log_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(first, 3.0)
        self.assertEqual(second, 0.0)
        self.assertEqual(len(rows), 2)

    def test_validation_footer_marks_no_bets_authoritatively(self):
        result = bot.add_validation_summary("## TOP PICKS\nCandidate", 1, [])
        self.assertIn("Python accepted 0 bet(s)", result)
        self.assertIn("Final betting decision: NO BETS", result)

    def test_completion_limit_fits_groq_tpm_budget(self):
        self.assertLessEqual(bot.MAX_COMPLETION_TOKENS, 4096)

    def test_american_to_decimal(self):
        self.assertEqual(bot.american_to_decimal(115), 2.15)
        self.assertEqual(bot.american_to_decimal(-200), 1.5)
        self.assertIsNone(bot.american_to_decimal(None))

    def test_parser_accepts_colon_after_odds(self):
        report = """## TOP PICKS
1. **Liverpool vs. Chelsea (EPL)**
   - Odds: 2.2

## VALUE PICKS
1. **Bayern Munich vs. Borussia Dortmund (Bundesliga)**
   - Odds: 2.0

## PICKS TO AVOID
"""
        picks = bot.parse_recommendations(report)
        self.assertEqual(
            [(p["team"], p["odds"], p["grade"]) for p in picks],
            [
                ("Liverpool", 2.2, "Top Pick"),
                ("Bayern Munich", 2.0, "Value Pick"),
            ],
        )

    def test_parser_accepts_team_name_before_odds_and_parentheses(self):
        report = """## TOP PICKS
1. **Utah Royals vs Washington Spirit**
   - Tournament: NWSL
   - Odds: Utah Royals 2.75

2. **Cienciano del Cusco vs Lanús**
   - Tournament: CONMEBOL Sudamericana
   - Odds: Cienciano del Cusco 2.20

## VALUE PICKS
1. **Instituto (Córdoba) vs Platense**
   - Tournament: Argentine LPF
   - Odds: Instituto (Córdoba) 2.05
"""
        picks = bot.parse_recommendations(report)
        self.assertEqual(
            [
                (p["team"], p["opponent"], p["tournament"], p["odds"], p["grade"])
                for p in picks
            ],
            [
                ("Utah Royals", "Washington Spirit", "NWSL", 2.75, "Top Pick"),
                (
                    "Cienciano del Cusco",
                    "Lanús",
                    "CONMEBOL Sudamericana",
                    2.2,
                    "Top Pick",
                ),
                (
                    "Instituto (Córdoba)",
                    "Platense",
                    "Argentine LPF",
                    2.05,
                    "Value Pick",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
