"""
Football Betting Bot — Automated daily picks pipeline.
Runs the 3-stage analysis via Groq API (Llama 3) and logs results.
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
MAX_COMPLETION_TOKENS = 4096
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
    parser.add_argument("--odds-max", type=float, default=3.0, help="Max decimal odds")
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


def american_to_decimal(value) -> float | None:
    """Convert American moneyline odds to decimal odds."""
    try:
        american = float(value)
    except (TypeError, ValueError):
        return None
    if american == 0:
        return None
    if american > 0:
        return round(1 + american / 100, 3)
    return round(1 + 100 / abs(american), 3)


def fetch_matches_from_espn_api(date_str: str) -> list[dict]:
    """Fetch fixtures and available moneyline odds from ESPN's scoreboard API."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_param = dt.strftime("%Y%m%d")
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
        f"?dates={date_param}&limit=1000"
    )
    body = fetch(url)
    if not body:
        return []

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log("  ESPN API returned invalid JSON")
        return []

    matches = []
    for event in data.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        home_name = home.get("team", {}).get("displayName")
        away_name = away.get("team", {}).get("displayName")
        if not home_name or not away_name:
            continue

        league_name = (
            event.get("league", {}).get("name")
            or competition.get("altGameNote")
            or data.get("leagues", [{}])[0].get("name")
            or "Unknown League"
        )
        odds_entries = competition.get("odds") or []
        odds_data = (odds_entries[0] if odds_entries else None) or {}
        moneyline = odds_data.get("moneyline") or {}
        home_american = (moneyline.get("home") or {}).get("close", {}).get("odds")
        away_american = (moneyline.get("away") or {}).get("close", {}).get("odds")

        matches.append({
            "team1": home_name,
            "team2": away_name,
            "score": "",
            "tournament": league_name,
            "level": parse_league_level(url, league_name),
            "source": url,
            "home_odds": american_to_decimal(home_american),
            "away_odds": american_to_decimal(away_american),
            "odds_source": (odds_data.get("provider") or {}).get("displayName", "ESPN"),
        })
    return matches


def fetch_matches_from_espn(date_str: str) -> list[dict]:
    """Fetch football matches from ESPN FC fixtures page."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    url = f"https://www.espn.com/soccer/fixtures/_/date/{dt.year}{dt.month:02d}{dt.day:02d}"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    league_containers = soup.select("div.league-container, section.league-container")
    for container in league_containers:
        league_el = container.select_one("h2, h3.league-name")
        league_name = league_el.get_text(strip=True) if league_el else "Unknown League"

        game_cards = container.select("li.event, div.event, div.game-card")
        for card in game_cards:
            teams_el = card.select("span.team-name, a.team-name, span.abbrev")
            if len(teams_el) >= 2:
                t1 = teams_el[0].get_text(strip=True)
                t2 = teams_el[1].get_text(strip=True)
                matches.append({
                    "team1": t1,
                    "team2": t2,
                    "score": "",
                    "tournament": league_name,
                    "level": parse_league_level(url, league_name),
                    "source": url,
                })

    return matches


def fetch_matches_from_bbc(date_str: str) -> list[dict]:
    """Fetch football matches from BBC Sport."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    url = f"https://www.bbc.com/sport/football/scores-fixtures/{dt.year}-{dt.month:02d}-{dt.day:02d}"
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    matches = []

    fixture_wrappers = soup.select("div[data-testid='fixture'], div.fixture, div.sp-c-fixture")
    for wrapper in fixture_wrappers:
        league_el = wrapper.find_previous(["h2", "h3", "legend"])
        league_name = league_el.get_text(strip=True) if league_el else "Unknown League"

        teams_el = wrapper.select("span.sp-c-fixture__team-name, span[data-testid='team-name']")
        if len(teams_el) >= 2:
            t1 = teams_el[0].get_text(strip=True)
            t2 = teams_el[1].get_text(strip=True)
            matches.append({
                "team1": t1,
                "team2": t2,
                "score": "",
                "tournament": league_name,
                "level": parse_league_level(url, league_name),
                "source": url,
            })

    return matches


def fetch_matches_from_fotmob_api(date_str: str) -> list[dict]:
    """Fetch football matches from FotMob-based API (free, no key)."""
    api_urls = [
        f"https://football-live-api.vercel.app/api/fotmob/matches/date/{date_str}",
    ]
    for api_url in api_urls:
        html = fetch(api_url)
        if not html:
            continue
        try:
            data = json.loads(html)
            matches = []
            if "matches" in data:
                for match in data["matches"]:
                    league_name = match.get("league", {}).get("name", "Unknown League")
                    for fixture in match.get("fixtures", []):
                        t1 = fixture.get("home", {}).get("name", "")
                        t2 = fixture.get("away", {}).get("name", "")
                        if t1 and t2:
                            matches.append({
                                "team1": t1,
                                "team2": t2,
                                "score": "",
                                "tournament": league_name,
                                "level": parse_league_level(api_url, league_name),
                                "source": api_url,
                            })
            if matches:
                return matches
        except (json.JSONDecodeError, TypeError):
            continue
    return []


def fetch_matches_all(date_str: str, leagues: list[str] | None = None) -> list[dict]:
    """Aggregate matches from all football sources."""
    log("Fetching matches and odds from ESPN API...")
    all_matches = fetch_matches_from_espn_api(date_str)
    log(f"  Found {len(all_matches)} from ESPN API")
    if not all_matches:
        log("Fetching matches from ESPN web...")
        all_matches.extend(fetch_matches_from_espn(date_str))
        log(f"  Found {len(all_matches)} from ESPN web")
    if not all_matches:
        log("Fetching matches from BBC Sport...")
        all_matches.extend(fetch_matches_from_bbc(date_str))
        log(f"  Found {sum(1 for _ in all_matches)} from BBC")
    if not all_matches:
        log("Fetching matches from FotMob API...")
        all_matches.extend(fetch_matches_from_fotmob_api(date_str))
        log(f"  Found {sum(1 for _ in all_matches)} from FotMob")

    seen = set()
    unique = []
    for m in all_matches:
        key = tuple(sorted([m["team1"].lower(), m["team2"].lower()]))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    log(f"Total unique matches: {len(unique)}")

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
    needs_lookup = []
    for match in matches:
        available = [
            odd for odd in (match.get("home_odds"), match.get("away_odds"))
            if odd is not None and odds_min <= odd <= odds_max
        ]
        if available:
            match["odds"] = available[0]
            enriched.append(match)
            log(
                f"  {match['team1']} {match.get('home_odds') or 'N/A'} vs "
                f"{match['team2']} {match.get('away_odds') or 'N/A'} ✓"
            )
        elif match.get("home_odds") is None and match.get("away_odds") is None:
            needs_lookup.append(match)

    if not needs_lookup:
        log(f"Qualifying matches in odds range [{odds_min}-{odds_max}]: {len(enriched)}")
        return enriched

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {}
        for m in needs_lookup:
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
    """Fetch team info from web sources."""
    name_part = team_name.lower().replace(" ", "-").replace("'", "").replace(".", "")
    # Try Transfermarkt search
    url = f"https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={name_part}"
    html = fetch(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        texts = [p.get_text(strip=True) for p in soup.select("p, h1, h2")[:20]]
        result = "\n".join(texts)
        return result[:2000] if result else "Profile not available"
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
        market_odds = (
            f"{m['team1']} {m['home_odds']:.2f}, {m['team2']} {m['away_odds']:.2f}"
            if m.get("home_odds") is not None and m.get("away_odds") is not None
            else str(m.get("odds", "N/A"))
        )
        match_lines.append(
            f"Match {i}: {m['team1']} vs {m['team2']}\n"
            f"  Tournament: {m['tournament']} ({m['level']})\n"
            f"  Moneyline odds: {market_odds} (source: {m.get('odds_source', 'N/A')})\n"
        )

    matches_text = "\n".join(match_lines) if match_lines else "No matches found in odds range."

    prompt = f"""You are a football betting analyst executing a 3-stage pipeline for matches on {date_str}.

## RAW DATA COLLECTED

Matches in odds range [{odds_min}-{odds_max}]:

{matches_text}

## Team Profile Data

"""

    prompt += (
        "\nNo independently verified current team profiles were supplied. "
        "Treat missing form, injury, and lineup information as uncertainty; "
        "do not manufacture it.\n"
    )

    prompt += f"""

## ANALYSIS INSTRUCTIONS

You MUST now perform the full 3-stage pipeline using only the verified fixtures and odds supplied above. Historical knowledge may provide context, but do not present it as current form, team news, or confirmed availability.

### STAGE 1 — Verification & Refinement
Review only the verified match data above. Never invent fixtures, odds, injuries, or current form. If data is unavailable, return no picks and explain which information is missing.

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
- Expected goals (xG) underperformance streak?
- Playing away against a strong home side?

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
    """Call Groq API (Llama 3) with the constructed prompt."""
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_COMPLETION_TOKENS,
        "temperature": 0.3,
    }

    log("Calling Groq API (Llama 3)...")
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        log(f"Groq response: {len(content)} chars")
        return content
    except requests.RequestException as e:
        log(f"Groq API error: {e}")
        if hasattr(e, "response") and e.response is not None:
            log(f"Response body: {e.response.text[:500]}")
        raise


# ─── Stage 4: Logging ───────────────────────────────────────────────

def parse_recommendations(report: str) -> list[dict]:
    """Parse the AI report to extract recommended bets."""
    recommendations = []
    current_type = None
    last_team = None
    last_opponent = None
    last_tournament = None

    for line in report.split("\n"):
        line_lower = line.strip().lower()
        line_clean = re.sub(r"\*+", "", line.strip().lstrip("-# ")).strip()
        line_clean = re.sub(r"^\d+[.)]\s*", "", line_clean)

        if "## top picks" in line_lower or "## top pick" in line_lower:
            current_type = "Top Pick"
            last_team = None
            last_opponent = None
            last_tournament = None
            continue
        if "## value picks" in line_lower or "## value pick" in line_lower:
            current_type = "Value Pick"
            last_team = None
            last_opponent = None
            last_tournament = None
            continue
        if "## picks to avoid" in line_lower or "## avoid" in line_lower:
            current_type = None
            last_team = None
            last_opponent = None
            last_tournament = None
            continue
        if line_lower.startswith("## "):
            current_type = None
            last_team = None
            last_opponent = None
            last_tournament = None
            continue

        if not current_type:
            continue
        if not line_clean:
            continue

        tournament_match = re.search(r'^tournament\s*:\s*(.+)$', line_clean, re.IGNORECASE)
        if tournament_match:
            last_tournament = tournament_match.group(1).strip()

        team_match = re.search(
            r'^(.+?)\s+v(?:s)?\.?\s+(.+)$',
            line_clean,
            flags=re.IGNORECASE,
        )
        if team_match:
            last_team = team_match.group(1).strip()
            last_opponent = team_match.group(2).strip()

        odds_match = None
        if re.match(r'^odds?\s*:', line_clean, re.IGNORECASE):
            odds_match = re.search(r'\b([1-9]\d*\.\d+)\b', line_clean)
        if odds_match and last_team:
            try:
                odds_val = float(odds_match.group(1))
                if 1.01 <= odds_val <= 50:
                    recommendations.append({
                        "team": last_team,
                        "opponent": last_opponent,
                        "tournament": last_tournament,
                        "odds": odds_val,
                        "grade": current_type,
                    })
            except ValueError:
                pass

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
            match_info = {
                "team1": rec.get("team", "Unknown"),
                "team2": rec.get("opponent", "Unknown"),
                "tournament": rec.get("tournament", "Unknown Competition"),
            }

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

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log("ERROR: No API key. Set GROQ_API_KEY env var.")
        log("Get a free key at https://console.groq.com/keys")
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
