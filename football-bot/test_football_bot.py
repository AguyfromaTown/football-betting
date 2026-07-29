import importlib.util
import sys
import types
import unittest
from pathlib import Path


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
