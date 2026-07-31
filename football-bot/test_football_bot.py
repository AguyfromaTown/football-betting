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
    def test_dashboard_uses_automated_data_sources_and_wl_results(self):
        html = (MODULE_PATH.parent.parent / "docs" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('predictions-log.csv', html)
        self.assertIn('performance-summary.md', html)
        self.assertIn('bankroll.txt', html)
        self.assertIn('["w","win","won"]', html)
        self.assertIn('id="audit-body"', html)
        self.assertIn('id="backtest-body"', html)
        self.assertNotIn('Click any <b', html)
        self.assertNotIn('copy-csv-btn', html)

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

    def test_validation_rejects_selected_teams_own_out_of_range_odds(self):
        matches = [{
            "team1": "Longshot",
            "team2": "Favourite",
            "home_odds": 5.5,
            "away_odds": 1.6,
            "draw_odds": 4.0,
            "home_form": "WWWWW",
            "away_form": "LLLLL",
            "home_record": "10-0-0",
            "away_record": "0-0-10",
        }]
        baseline = bot.calculate_team_baseline(matches[0], "Longshot")
        candidates = [{
            "team": "Longshot",
            "score": baseline["score"],
            "assessed_probability": baseline["assessed_probability"],
        }]

        validated = bot.validate_recommendations(candidates, matches, 1.5, 3.0)

        self.assertEqual(validated, [])

    def test_analysis_match_selection_caps_and_prioritizes_best_ev(self):
        matches = []
        for index in range(25):
            matches.append({
                "team1": f"Home {index}",
                "team2": f"Away {index}",
                "home_odds": 2.0,
                "away_odds": 4.0,
                "draw_odds": 4.0,
                "home_form": "WWWWW" if index == 24 else "DDDDD",
                "away_form": "LLLLL" if index == 24 else "DDDDD",
                "home_record": "10-0-0" if index == 24 else "5-5-5",
                "away_record": "0-0-10" if index == 24 else "5-5-5",
            })

        selected = bot.select_analysis_matches(matches)

        self.assertEqual(len(selected), bot.MAX_AI_MATCHES)
        self.assertEqual(selected[0]["team1"], "Home 24")

    def test_deterministic_report_contains_machine_readable_candidates(self):
        match = {
            "team1": "Home",
            "team2": "Away",
            "home_odds": 2.0,
            "away_odds": 4.0,
            "draw_odds": 4.0,
            "home_form": "WWWWW",
            "away_form": "LLLLL",
            "home_record": "10-0-0",
            "away_record": "0-0-10",
            "tournament": "Test League",
        }
        candidates = bot.build_statistical_candidates([match], 1.5, 3.0)

        report = bot.build_deterministic_report(
            "2026-08-01", [match], candidates, 100.0
        )

        self.assertIn("## MACHINE READABLE PICKS", report)
        self.assertEqual(bot.parse_recommendations(report), candidates)

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

    def test_dixon_coles_probabilities_are_normalized(self):
        result = bot.dixon_coles_probabilities(1.6, 1.1)

        self.assertAlmostEqual(result["home_probability"] + result["draw_probability"] + result["away_probability"], 1.0)
        self.assertEqual(len(result["top_scorelines"]), 3)
        self.assertGreater(result["home_probability"], result["away_probability"])

    def test_goal_model_builds_attack_defence_xg_without_future_matches(self):
        rows = []
        for index in range(100):
            rows.append({
                "Date": f"{(index % 28) + 1:02d}/0{(index % 6) + 1}/2026",
                "HomeTeam": "Home" if index < 12 else f"League Home {index % 10}",
                "AwayTeam": "Away" if 12 <= index < 24 else f"League Away {index % 10}",
                "FTHG": "2" if index < 12 else "1",
                "FTAG": "2" if 12 <= index < 24 else "1",
            })

        model = bot.calculate_goal_model(
            {"team1": "Home", "team2": "Away"}, rows, "2026-07-31"
        )

        self.assertIsNotNone(model)
        self.assertGreaterEqual(model["home_sample"], 6)
        self.assertGreaterEqual(model["away_sample"], 6)
        self.assertGreater(model["home_xg"], 0)
        self.assertAlmostEqual(model["home_probability"] + model["draw_probability"] + model["away_probability"], 1.0)

    def test_baseline_blends_goal_model_at_forty_percent(self):
        match = {
            "team1": "Home", "team2": "Away", "home_odds": 2.0,
            "draw_odds": 4.0, "away_odds": 4.0, "home_form": "WWWWW",
            "away_form": "LLLLL", "home_record": "8-1-1", "away_record": "1-1-8",
            "goal_model": {"home_probability": 0.60, "away_probability": 0.20, "draw_probability": 0.20, "home_xg": 1.8, "away_xg": 0.8, "home_sample": 12, "away_sample": 12, "top_scorelines": [((1, 0), 0.2)]},
        }

        baseline = bot.calculate_team_baseline(match, "Home")
        expected = 0.40 * 0.60 + 0.35 * 0.50 + 0.25 * 0.66

        self.assertAlmostEqual(baseline["assessed_probability"], expected)
        self.assertEqual(baseline["component_weights"], "goals=.40;market=.35;form=.25")

    def test_evidence_quality_rewards_complete_consistent_match_data(self):
        match = {
            "event_id": "123", "level": "Premier League",
            "home_form": "WWDWW", "away_form": "LDLLD",
            "home_record": "8-1-1", "away_record": "1-2-7",
        }
        baseline = {"complete_evidence": True, "signals_agree": True, "market_overround": 1.05}

        score, grade = bot.evidence_quality(match, baseline)

        self.assertEqual(score, 10)
        self.assertEqual(grade, "A")

    def test_backtest_summary_segments_football_context(self):
        rows = [
            {"DATE": "2026-07-01", "OPENING_ODDS": "2.10", "MODEL_PROBABILITY": "0.52", "EV": "0.09", "RESULT": "W", "CLV": "0.03", "COMPETITION": "EPL", "LEVEL": "Premier League", "SIDE": "home", "QUALITY_GRADE": "A"},
            {"DATE": "2026-07-02", "OPENING_ODDS": "2.20", "MODEL_PROBABILITY": "0.50", "EV": "0.10", "RESULT": "L", "CLV": "-0.01", "COMPETITION": "EPL", "LEVEL": "Premier League", "SIDE": "away", "QUALITY_GRADE": "B"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backtest-summary.md"
            with patch.object(bot, "BACKTEST_FILE", output):
                bot.generate_backtest_summary(rows)
            report = output.read_text(encoding="utf-8")

        self.assertIn("## Odds bands", report)
        self.assertIn("2.0–2.5 | 2 | 50.0%", report)
        self.assertIn("## Home and away", report)
        self.assertIn("## Competition", report)

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

    def test_candidate_scan_rejects_conflicting_form_and_season_signals(self):
        match = {
            "team1": "Home", "team2": "Away",
            "home_odds": 2.0, "draw_odds": 4.0, "away_odds": 4.0,
            "home_form": "WWWWW", "away_form": "LLLLL",
            "home_record": "1-1-8", "away_record": "8-1-1",
        }
        self.assertEqual(bot.build_statistical_candidates([match], 1.5, 3.0), [])

    def test_candidate_scan_requires_complete_evidence(self):
        match = {
            "team1": "Home", "team2": "Away",
            "home_odds": 2.0, "draw_odds": 4.0, "away_odds": 4.0,
            "home_form": "WWWWW", "away_form": "LLLLL",
            "home_record": None, "away_record": None,
        }
        self.assertEqual(bot.build_statistical_candidates([match], 1.5, 3.0), [])

    def test_portfolio_prioritizes_ev_and_caps_daily_exposure(self):
        recommendations = [{
            "team": f"Team {index}", "grade": "Top Pick", "ev": ev,
            "score": 9.0,
            "match": {"team1": f"Team {index}", "team2": f"Other {index}"},
        } for index, ev in enumerate((0.20, 0.18, 0.16, 0.14, 0.12))]

        selected = bot.select_portfolio(recommendations)

        self.assertEqual([item["ev"] for item in selected], [0.20, 0.18])

    def test_portfolio_never_selects_both_sides_of_same_match(self):
        match = {"team1": "Home", "team2": "Away"}
        recommendations = [
            {"team": "Home", "grade": "Value Pick", "ev": 0.10, "score": 8, "match": match},
            {"team": "Away", "grade": "Value Pick", "ev": 0.08, "score": 8, "match": match},
        ]

        selected = bot.select_portfolio(recommendations)

        self.assertEqual([item["team"] for item in selected], ["Home"])

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

    def test_settlement_credits_full_winning_return(self):
        completed = [{
            "team1": "Home", "team2": "Away", "completed": True,
            "home_winner": True, "away_winner": False,
            "home_odds": 1.8, "away_odds": 4.0,
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path, bankroll_path = root / "bets-log.csv", root / "bankroll.txt"
            log_path.write_text(
                "DATE,MATCH,BET,ODDS,STAKE,RESULT,RETURN,STARTING BALANCE\n"
                "2026-08-01,Home vs Away (League),Home to win,2.00,3.00,,,100.00\n",
                encoding="utf-8",
            )
            bankroll_path.write_text("97.00", encoding="utf-8")
            with (
                patch.object(bot, "LOG_FILE", log_path),
                patch.object(bot, "BANKROLL_FILE", bankroll_path),
                patch.object(bot, "fetch_matches_from_espn_api", return_value=completed),
                patch.object(bot, "update_audit_result"),
            ):
                settled = bot.settle_pending_bets()

            with log_path.open(encoding="utf-8") as handle:
                rows = list(__import__("csv").DictReader(handle))
            self.assertEqual(settled, 1)
            self.assertEqual(rows[0]["RESULT"], "W")
            self.assertEqual(rows[0]["RETURN"], "6.00")
            self.assertEqual(bankroll_path.read_text(), "103.00")

    def test_completion_limit_fits_groq_tpm_budget(self):
        self.assertLessEqual(bot.MAX_COMPLETION_TOKENS, 4096)

    def test_american_to_decimal(self):
        self.assertEqual(bot.american_to_decimal(115), 2.15)
        self.assertEqual(bot.american_to_decimal(-200), 1.5)
        self.assertIsNone(bot.american_to_decimal(None))

    def test_three_way_market_uses_best_prices_and_median_consensus(self):
        payload = {"bookmakers": {
            "A": [{"name": "1X2", "odds": [{"home": "2.0", "draw": "3.2", "away": "4.0"}]}],
            "B": [{"name": "Match Winner", "odds": [{"home": "2.2", "draw": "3.0", "away": "3.8"}]}],
            "C": [{"name": "ML", "odds": [{"home": "2.1", "draw": "3.1", "away": "3.9"}]}],
        }}

        market = bot.extract_three_way_market(payload)

        self.assertEqual(market["best_home"], 2.2)
        self.assertEqual(market["best_draw"], 3.2)
        self.assertEqual(market["best_away"], 4.0)
        self.assertEqual(market["consensus_home"], 2.1)
        self.assertEqual(market["consensus_draw"], 3.1)
        self.assertEqual(market["bookmaker_count"], 3)

    def test_baseline_uses_consensus_for_probability_and_best_price_for_ev(self):
        match = {
            "team1": "Home", "team2": "Away",
            "home_odds": 2.2, "draw_odds": 3.2, "away_odds": 4.0,
            "consensus_home_odds": 2.0, "consensus_draw_odds": 3.0, "consensus_away_odds": 4.0,
            "home_form": "DDDDD", "away_form": "DDDDD",
            "home_record": "0-10-0", "away_record": "0-10-0",
        }

        baseline = bot.calculate_team_baseline(match, "Home")

        self.assertAlmostEqual(baseline["market_probability"], 0.461538, places=5)
        self.assertAlmostEqual(baseline["ev"], baseline["assessed_probability"] * 2.2 - 1)

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
