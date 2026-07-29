"""
Football Betting Bot — Automated daily picks pipeline.
Runs the 3-stage analysis via Google Gemini API and logs results.
Designed for GitHub Actions execution.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
BANKROLL_FILE = REPO_ROOT / "bankroll.txt"
LOG_FILE = REPO_ROOT / "bets-log.csv"
REPORTS_DIR = REPO_ROOT / "reports"

REQUEST_TIMEOUT = 30
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
}


# ─── Helpers ────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log(f"  Failed to fetch {url}: {e}")
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Football betting bot")
    parser.add_argument("--date", default=None, help="Match date (YYYY-MM-DD)")
    parser.add_argument("--odds-min", type=float, default=1.5, help="Min decimal odds")
    parser.add_argument("--odds-max", type=float, default=1.6, help="Max decimal odds")
    parser.add_argument("--bankroll", type=float, default=None, help="Override bankroll")
    parser.add_argument("--force", action="store_true", help="Run even if bets already logged for this date")
    parser.add_argument("--leagues", default=None, help="Comma-separated league filter (e.g., EPL,LaLiga,SerieA)")
    return parser.parse_args()


def resolve_date(raw: str | None) -> str:
    if raw:
        return raw
    return datetime.now().strftime("%Y-%m-%d")


def load_bankroll(args_bankroll: float | None) -> float | None:
    if args_bankroll is not None:
        with open(BANKROLL_FILE, "w") as f:
            f.write(str(args_bankroll))
        log(f"Bankroll overridden to €{args_bankroll:.2f}")
        return args_bankroll

    if BANKROLL_FILE.exists():
        content = BANKROLL_FILE.read_text().strip()
        if content:
            try:
                val = float(content)
                log(f"Loaded bankroll: €{val:.2f}")
                return val
            except ValueError:
                pass

    log("No bankroll found. Run with --bankroll <amount> to set it.")
    return None


def save_bankroll(bankroll: float | None, total_stake: float):
    if bankroll is None:
        return
    remaining = round(bankroll - total_stake, 2)
    with open(BANKROLL_FILE, "w") as f:
        f.write(str(remaining))
    log(f"Bankroll saved: €{remaining:.2f} (was €{bankroll:.2f}, staked €{total_stake:.2f})")


# ─── Stage 1: Data Collection ────────────────────────────────────────

def parse_league_level(url: str, name: str) -> str:
    name_lower = name.lower()
    url_lower = url.lower()
    if "champions league" in url_lower or "champions league" in name_lower or "uefa champions" in url_lower:
        return "UCL"
    if "europa league" in url_lower or "europa league" in name_lower:
        return "UEL"
    if "conference league" in url_lower or "europa conference" in name_lower:
        return "UECL"
    if "premier league" in url_lower or "premier league" in name_lower or "epl" in url_lower:
        return "EPL"
    if "la liga" in url_lower or "laliga" in name_lower:
        return "LaLiga"
    if "bundesliga" in url_lower or "bundesliga" in name_lower:
        return "Bundesliga"
    if "serie a" in url_lower or "serie-a" in url_lower or "seriea" in name_lower:
        return "SerieA"
    if "ligue 1" in url_lower or "ligue-1" in url_lower or "ligue1" in name_lower:
        return "Ligue1"
    if "eredivisie" in url_lower or "eredivisie" in name_lower:
        return "Eredivisie"
    if "primeira liga" in url_lower or "primeira" in name_lower:
        return "Primeira Liga"
    return "Other"


def fetch_matches_from_fbref(date_str: str) -> list[dict]:
    """Fetch football matches from FBref scores page."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    url = f"https://fbref.com/en/matches/{dt.year}-{dt.month:02d}-{dt.day:02d}"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    league_tables = soup.select("div.matchup, table.stats_table")
    for table in soup.select("div.matchup"):
        league_header = table.find_previous("h2")
        league_name = league_header.get_text(strip=True) if league_header else "Unknown League"

        teams = table.select("td.team, div.team, a.team")
        scores = table.select("td.score, div.score")

        if len(teams) >= 2:
            t1 = teams[0].get_text(strip=True)
            t2 = teams[1].get_text(strip=True)
            score = scores[0].get_text(strip=True) if scores else ""
            matches.append({
                "team1": t1,
                "team2": t2,
                "score": score,
                "tournament": league_name,
                "level": parse_league_level(url, league_name),
                "source": url,
            })
    return matches


def fetch_matches_from_flashscore(date_str: str) -> list[dict]:
    """Fetch football matches from Flashscore (via alternative if blocked)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    url = f"https://www.flashscore.com/football/{dt.year}-{dt.month:02d}-{dt.day:02d}/"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    event_cards = soup.select("div.event__match, div.soccerMatch")
    for card in event_cards:
        tournament_el = card.select_one("div.event__title, span.tournament-name")
        tournament = tournament_el.get_text(strip=True) if tournament_el else "Unknown"

        home_el = card.select_one("div.event__homeParticipant, span.home-name")
        away_el = card.select_one("div.event__awayParticipant, span.away-name")

        if home_el and away_el:
            t1 = home_el.get_text(strip=True)
            t2 = away_el.get_text(strip=True)
            matches.append({
                "team1": t1,
                "team2": t2,
                "score": "",
                "tournament": tournament,
                "level": parse_league_level(url, tournament),
                "source": url,
            })

    return matches


def fetch_matches_all(date_str: str, leagues: list[str] | None = None) -> list[dict]:
    """Aggregate matches from all football sources."""
    all_matches = []
    log("Fetching matches from FBref...")
    all_matches.extend(fetch_matches_from_fbref(date_str))
    log(f"  Found {len(all_matches)} matches from FBref")
    log("Fetching matches from Flashscore...")
    all_matches.extend(fetch_matches_from_flashscore(date_str))
    log(f"  Found {len(all_matches)} total matches so far")

    # Deduplicate by team1+team2
    seen = set()
    unique = []
    for m in all_matches:
        key = tuple(sorted([m["team1"].lower(), m["team2"].lower()]))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    log(f"Total unique matches: {len(unique)}")

    # Filter by leagues if specified
    if leagues:
        filtered = []
        for m in unique:
            for league in leagues:
                if league.lower() in m["tournament"].lower() or league.lower() in m["level"].lower():
                    filtered.append(m)
                    break
        log(f"After league filter ({leagues}): {len(filtered)} matches")
        return filtered

    return unique


def fetch_odds_for_match(team1: str, team2: str) -> tuple[float | None, str | None, str | None]:
    """Try to find odds for a match. Returns (odds, team_name, source_url)."""
    search_name = f"{team1}-{team2}".replace(" ", "-").lower()
    search_name = re.sub(r"[^a-z0-9-]", "", search_name)
    urls_to_try = [
        f"https://oddspedia.com/football/{search_name}",
        f"https://www.oddsportal.com/football/{search_name}",
    ]

    for url in urls_to_try:
        html = fetch(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        odds_elements = soup.select(
            "[data-odds], span.odds-value, div.odds-value, span.market-odd, "
            "div.odds, span.odds"
        )
        odds_values = []
        for el in odds_elements:
            text = el.get("data-odds", el.get_text(strip=True))
            try:
                val = float(text)
                if 1.01 <= val <= 50.0:
                    odds_values.append(val)
            except (ValueError, TypeError):
                continue

        if odds_values:
            odds = odds_values[0]
            return odds, team1, url

    return None, None, None


def attach_odds(matches: list[dict], odds_min: float, odds_max: float) -> list[dict]:
    """Fetch odds for each match and filter by range."""
    log("Fetching odds for matches...")
    enriched = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {}
        for m in matches:
            future = executor.submit(
                fetch_odds_for_match, m["team1"], m["team2"]
            )
            future_map[future] = m

        for future in as_completed(future_map):
            m = future_map[future]
            try:
                odds, team_name, source = future.result()
            except Exception as e:
                log(f"  Odds fetch error for {m['team1']} vs {m['team2']}: {e}")
                continue

            if odds and odds_min <= odds <= odds_max:
                m["odds"] = odds
                m["odds_source"] = source or "unknown"
                enriched.append(m)
                log(f"  {m['team1']} vs {m['team2']} → {odds:.2f} ✓")
            else:
                log(f"  {m['team1']} vs {m['team2']} → {'no odds' if odds is None else f'{odds:.2f} (out of range)'}")

    log(f"Qualifying matches in odds range [{odds_min}-{odds_max}]: {len(enriched)}")
    return enriched


def fetch_team_profile(team_name: str) -> str:
    """Fetch team stats from FBref."""
    name_part = team_name.lower().replace(" ", "-").replace("'", "").replace(".", "")
    url = f"https://fbref.com/en/search/search.fcgi?search={name_part}"
    html = fetch(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select("table")
        stats_text = []
        for table in tables[:3]:
            rows = table.select("tr")
            table_data = []
            for row in rows:
                cells = row.select("td, th")
                row_text = " | ".join(c.get_text(strip=True) for c in cells)
                if row_text:
                    table_data.append(row_text)
            if table_data:
                stats_text.append("\n".join(table_data))
        result = "\n\n".join(stats_text) if stats_text else html[:3000]
        return result[:4000]
    return "Profile not available"


# ─── Stage 2 & 3: AI Analysis ───────────────────────────────────────

def build_prompt(
    date_str: str,
    matches: list[dict],
    bankroll: float | None,
    odds_min: float,
    odds_max: float,
) -> str:
    """Construct the full 3-stage prompt with embedded data."""

    match_lines = []
    for i, m in enumerate(matches, 1):
        match_lines.append(
            f"Match {i}: {m['team1']} vs {m['team2']}\n"
            f"  Tournament: {m['tournament']} ({m['level']})\n"
            f"  Odds: {m.get('odds', 'N/A')} (source: {m.get('odds_source', 'N/A')})\n"
        )

    matches_text = "\n".join(match_lines) if match_lines else "No matches found in odds range."

    prompt = f"""You are a football betting analyst executing a 3-stage pipeline for matches on {date_str}.

## RAW DATA COLLECTED

Matches in odds range [{odds_min}-{odds_max}]:

{matches_text}

## Team Profile Data (from FBref)

"""

    seen_teams = set()
    for m in matches:
        for team in [m["team1"], m["team2"]]:
            if team.lower() not in seen_teams:
                seen_teams.add(team.lower())
                profile = fetch_team_profile(team)
                prompt += f"\n--- {team} Profile ---\n{profile}\n"

    prompt += f"""

## ANALYSIS INSTRUCTIONS

You MUST now perform the full 3-stage pipeline using ONLY the data above and your own football knowledge.

### STAGE 1 — Verification & Refinement
Review the match data above. Verify the leagues and identify any issues. Cross-reference with your knowledge of football schedules.

### STAGE 2 — Performance Analysis
For each team whose odds fall within {odds_min}-{odds_max}, analyze:

1. **Recent form**: Assess based on the team data above and your knowledge
2. **Head-to-head**: Note if data shows H2H info
3. **Home/Away performance**: Note home/away splits
4. **Physical condition**: Flag injuries, suspensions, fixture congestion
5. **Match context**: Formations, tactical matchups, manager changes, motivation

Score each team 1-10 on the Five-Factor system:
- Recent Form (25%)
- Home/Away Performance (25%)
- Head-to-Head (15%)
- Physical & Injury Context (20%)
- Opponent Quality (15%)

Then calculate: Total = (Form×0.25) + (HomeAway×0.25) + (H2H×0.15) + (Injury×0.20) + (Opponent×0.15)

Grade: 8.5-10 Elite | 7.0-8.4 Strong | 5.5-6.9 Moderate | <5.5 Weak

For each candidate, calculate:
- Implied Probability = 1 / odds
- Your assessed probability
- Expected Value = (assessed_prob × odds) - 1

Run the Red Flag checklist:
- Lost 3+ consecutive matches?
- 3rd match in 8 days (fixture congestion)?
- Key injuries/suspensions?
- Poor H2H record?
- Manager under pressure / recent sacking?
- Odds lengthened significantly?
- xG underperformance streak?

### STAGE 3 — Recommendations

Assign final calls:

- **Top Pick** (score > 8.0, EV > 8%)
- **Value Pick** (score > 7.0, EV > 5%)
- **Moderate Pick** (score > 5.5, EV > 0%)
- **No Bet** (everything else)
"""

    if bankroll is not None:
        prompt += f"""

### Staking (Tiered Proportional Betting)
Current bankroll: €{bankroll:.2f}

For each recommendation, include:
- Top Pick: €{bankroll * 0.03:.2f} (3% of bankroll)
- Value Pick: €{bankroll * 0.02:.2f} (2% of bankroll)
- Moderate Pick: €{bankroll * 0.01:.2f} (1% of bankroll)
"""

    prompt += """

### Report Format
Present your output with these sections:

## MARKET OVERVIEW
Brief summary of the day's matches in this odds range.

## TOP PICKS
Team, opponent, tournament, level, odds, EV, assessed win %, stake, key stats, rationale.

## VALUE PICKS
Same format as above, for lower-confidence picks.

## PICKS TO AVOID
Teams whose odds look appealing but the numbers don't support it.

## DISCLAIMER
Odds change, no guarantees, bet responsibly.

### Tone
Direct and analytical. Quantify confidence. No marketing language. Aim for 500-800 words of dense analysis.
"""
    return prompt


def call_ai(prompt: str, api_key: str) -> str:
    """Call Google Gemini API (free tier) with the constructed prompt."""
    import google.genai as genai

    client = genai.Client(api_key=api_key)

    log("Calling Gemini API (free)...")
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config={
            "max_output_tokens": 8192,
            "temperature": 0.3,
        },
    )

    content = response.text if response.text else ""
    log(f"Gemini response: {len(content)} chars")
    return content


# ─── Stage 4: Logging ───────────────────────────────────────────────

def parse_recommendations(report: str) -> list[dict]:
    """Parse the AI report to extract recommended bets."""
    recommendations = []
    current_type = None
    current_match = None

    for line in report.split("\n"):
        line_stripped = line.strip().lower()

        if "## top picks" in line_stripped:
            current_type = "Top Pick"
            current_match = None
            continue
        elif "## value picks" in line_stripped:
            current_type = "Value Pick"
            current_match = None
            continue
        elif "## picks to avoid" in line_stripped:
            current_type = None
            continue

        if not current_type:
            continue

        odds_match = re.search(r'(?:odds?|at)\s+([1-9]\.[0-9]+)', line_stripped)
        team_match = re.search(
            r'^\*{0,2}\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]*)*)\s+vs\s+', line
        )

        if team_match:
            current_match = team_match.group(1).strip()

        if odds_match and current_match:
            try:
                odds_val = float(odds_match.group(1))
                recommendations.append({
                    "team": current_match,
                    "odds": odds_val,
                    "grade": current_type,
                })
                current_match = None
            except ValueError:
                pass

    # Fallback: scan for structured pick patterns
    if not recommendations:
        for line in report.split("\n"):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            grade = None
            if "**Top Pick**" in line_stripped:
                grade = "Top Pick"
            elif "**Value Pick**" in line_stripped:
                grade = "Value Pick"

            if grade:
                team = None
                odds = None

                p_match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:vs|v\.)\s+', line_stripped)
                if p_match:
                    team = p_match.group(1).strip()

                o_match = re.search(r'(?:odds?|at|@)\s*([1-9]\.[0-9]+)', line_stripped)
                if o_match:
                    try:
                        odds = float(o_match.group(1))
                    except ValueError:
                        pass

                if team:
                    recommendations.append({
                        "team": team,
                        "odds": odds,
                        "grade": grade,
                    })

    return recommendations


def log_bets(
    date_str: str,
    recommendations: list[dict],
    matches: list[dict],
    bankroll: float | None,
):
    """Append bets to the log CSV."""
    file_exists = LOG_FILE.exists()
    rows_to_append = []
    current_balance = bankroll
    total_stake = 0.0

    for rec in recommendations:
        if rec["grade"] not in ("Top Pick", "Value Pick"):
            continue

        match_info = None
        for m in matches:
            if rec["team"].lower() in m["team1"].lower() or rec["team"].lower() in m["team2"].lower():
                match_info = m
                break

        if not match_info:
            continue

        if current_balance is not None:
            if rec["grade"] == "Top Pick":
                stake_pct = 0.03
            else:
                stake_pct = 0.02

            stake = round(current_balance * stake_pct, 2)
            total_stake += stake
        else:
            stake = 0.0

        match_label = f"{match_info['team1']} vs {match_info['team2']} ({match_info['tournament']})"
        bet_label = f"{rec['team']} to win"
        odds_str = f"{rec['odds']:.2f}" if rec["odds"] else ""
        stake_str = f"{stake:.2f}" if stake else ""
        balance_str = f"{current_balance:.2f}" if current_balance is not None else ""

        rows_to_append.append({
            "date": date_str,
            "match": match_label,
            "bet": bet_label,
            "odds": odds_str,
            "stake": stake_str,
            "result": "",
            "return": "",
            "starting_balance": balance_str,
        })

        if current_balance is not None:
            current_balance -= stake

    if not rows_to_append:
        log("No bets to log.")
        return total_stake

    write_header = not file_exists or LOG_FILE.stat().st_size == 0
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["DATE", "MATCH", "BET", "ODDS", "STAKE", "RESULT", "RETURN", "STARTING BALANCE"])
        for row in rows_to_append:
            writer.writerow([
                row["date"], row["match"], row["bet"], row["odds"],
                row["stake"], row["result"], row["return"], row["starting_balance"],
            ])

    log(f"Logged {len(rows_to_append)} bets to {LOG_FILE.name}")
    return total_stake


# ─── Report ──────────────────────────────────────────────────────────

def save_report(date_str: str, report: str):
    """Save the AI report to a dated file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"picks-{date_str}.md"
    path = REPORTS_DIR / filename
    path.write_text(report, encoding="utf-8")
    log(f"Report saved: {path}")


# ─── Main ────────────────────────────────────────────────────────────

def already_logged_today(date_str: str) -> bool:
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
        return False
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if row and row[0].strip() == date_str:
                return True
    return False


def main():
    args = parse_args()
    date_str = resolve_date(args.date)
    odds_min = args.odds_min
    odds_max = args.odds_max
    leagues = args.leagues.split(",") if args.leagues else None

    log(f"=== Football Bot — {date_str} ===")
    log(f"Odds range: {odds_min}-{odds_max}")
    if leagues:
        log(f"League filter: {leagues}")

    if not args.force and already_logged_today(date_str):
        log(f"Bets already logged for {date_str}. Skipping.")
        log("(Use --force to override.)")
        return

    bankroll = load_bankroll(args.bankroll)
    if bankroll is None:
        log("WARNING: No bankroll set. Run with --bankroll <amount>")

    all_matches = fetch_matches_all(date_str, leagues)
    if not all_matches:
        log("No matches found from web sources. Will use AI knowledge only.")

    qualified = attach_odds(all_matches, odds_min, odds_max)

    log("Building analysis prompt...")
    prompt = build_prompt(date_str, qualified, bankroll, odds_min, odds_max)

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log("ERROR: No API key. Set GOOGLE_API_KEY env var.")
        log("Get a free key at https://aistudio.google.com/apikey")
        sys.exit(1)

    report = call_ai(prompt, api_key)

    recommendations = parse_recommendations(report)
    log(f"Parsed {len(recommendations)} recommendations from report")
    total_stake = log_bets(date_str, recommendations, qualified, bankroll)

    save_bankroll(bankroll, total_stake)
    save_report(date_str, report)

    log("=== Done ===")
    print("\n" + report)


if __name__ == "__main__":
    main()
