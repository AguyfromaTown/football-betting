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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
BANKROLL_FILE = REPO_ROOT / "bankroll.txt"
LOG_FILE = REPO_ROOT / "bets-log.csv"
AUDIT_FILE = REPO_ROOT / "predictions-log.csv"
PERFORMANCE_FILE = REPO_ROOT / "performance-summary.md"
REPORTS_DIR = REPO_ROOT / "reports"

REQUEST_TIMEOUT = 30
MAX_COMPLETION_TOKENS = 4096
MAX_AI_MATCHES = 20
MAX_DAILY_EXPOSURE = 0.08
MAX_DAILY_BETS = 4
MAX_MARKET_OVERROUND = 1.18
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
    parser.add_argument("--settle-only", action="store_true", help="Settle pending bets without generating picks")
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


def competitor_record(competitor: dict) -> str | None:
    """Return ESPN's total W-D-L record summary when present."""
    for record in competitor.get("records") or []:
        if record.get("type") == "total" and record.get("summary"):
            return str(record["summary"])
    return None


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
        draw_american = (odds_data.get("drawOdds") or {}).get("moneyLine")

        matches.append({
            "event_id": str(event.get("id", "")),
            "team1": home_name,
            "team2": away_name,
            "score": f"{home.get('score', '')}-{away.get('score', '')}",
            "completed": bool((event.get("status", {}).get("type") or {}).get("completed")),
            "home_winner": bool(home.get("winner")),
            "away_winner": bool(away.get("winner")),
            "tournament": league_name,
            "level": parse_league_level(url, league_name),
            "source": url,
            "home_odds": american_to_decimal(home_american),
            "away_odds": american_to_decimal(away_american),
            "draw_odds": american_to_decimal(draw_american),
            "home_form": home.get("form") or None,
            "away_form": away.get("form") or None,
            "home_record": competitor_record(home),
            "away_record": competitor_record(away),
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

def record_points_rate(summary: str | None) -> float | None:
    """Convert an ESPN soccer W-D-L summary to points earned / points available."""
    if not summary:
        return None
    match = re.fullmatch(r"\s*(\d+)-(\d+)-(\d+)\s*", summary)
    if not match:
        return None
    wins, draws, losses = (int(value) for value in match.groups())
    played = wins + draws + losses
    return (3 * wins + draws) / (3 * played) if played else None


def form_points_rate(form: str | None) -> float | None:
    """Convert a compact form string such as WWLLD to a normalized points rate."""
    results = [char for char in (form or "").upper() if char in "WDL"]
    if not results:
        return None
    points = sum(
        3 if result == "W" else 1 if result == "D" else 0
        for result in results
    )
    return points / (3 * len(results))


def calculate_team_baseline(match: dict, team: str) -> dict | None:
    """Estimate win probability from the de-vigged 3-way market and ESPN evidence."""
    home_odds = match.get("home_odds")
    away_odds = match.get("away_odds")
    draw_odds = match.get("draw_odds")
    if not all(
        isinstance(odds, (int, float)) and odds > 1
        for odds in (home_odds, away_odds, draw_odds)
    ):
        return None

    team_key = normalize_team_name(team)
    if team_key == normalize_team_name(match["team1"]):
        team_odds = float(home_odds)
        team_form = form_points_rate(match.get("home_form"))
        opponent_form = form_points_rate(match.get("away_form"))
        team_record = record_points_rate(match.get("home_record"))
        opponent_record = record_points_rate(match.get("away_record"))
    elif team_key == normalize_team_name(match["team2"]):
        team_odds = float(away_odds)
        team_form = form_points_rate(match.get("away_form"))
        opponent_form = form_points_rate(match.get("home_form"))
        team_record = record_points_rate(match.get("away_record"))
        opponent_record = record_points_rate(match.get("home_record"))
    else:
        return None

    inverse_total = (
        1 / float(home_odds)
        + 1 / float(away_odds)
        + 1 / float(draw_odds)
    )
    market_probability = (1 / team_odds) / inverse_total

    evidence = []
    if team_form is not None and opponent_form is not None:
        evidence.append((0.65, team_form - opponent_form))
    if team_record is not None and opponent_record is not None:
        evidence.append((0.35, team_record - opponent_record))
    if not evidence:
        return None

    weight_total = sum(weight for weight, _ in evidence)
    strength_difference = sum(
        weight * difference for weight, difference in evidence
    ) / weight_total
    evidence_adjustment = max(-0.08, min(0.08, strength_difference * 0.10))
    assessed_probability = max(
        0.02,
        min(0.95, market_probability + evidence_adjustment),
    )
    ev = assessed_probability * team_odds - 1
    score = max(0.0, min(10.0, 6.0 + max(0.0, ev) * 30))

    return {
        "market_probability": market_probability,
        "evidence_adjustment": evidence_adjustment,
        "assessed_probability": assessed_probability,
        "ev": ev,
        "score": score,
        "team_odds": team_odds,
        "team_form_rate": team_form,
        "opponent_form_rate": opponent_form,
        "team_record_rate": team_record,
        "opponent_record_rate": opponent_record,
        "market_overround": inverse_total,
        "complete_evidence": all(value is not None for value in (
            team_form,
            opponent_form,
            team_record,
            opponent_record,
        )),
        "signals_agree": (
            team_form is not None
            and opponent_form is not None
            and team_record is not None
            and opponent_record is not None
            and (team_form - opponent_form) * (team_record - opponent_record) >= 0
        ),
    }


def baseline_is_reliable(baseline: dict | None) -> bool:
    """Require a complete, internally consistent evidence set and sane market."""
    return bool(
        baseline
        and baseline.get("complete_evidence")
        and baseline.get("signals_agree")
        and 0.98 <= baseline.get("market_overround", 0) <= MAX_MARKET_OVERROUND
    )


def build_statistical_candidates(
    matches: list[dict],
    odds_min: float,
    odds_max: float,
) -> list[dict]:
    """Scan every eligible team so AI omissions cannot hide a statistical edge."""
    candidates = []
    for match in matches:
        for team, opponent in (
            (match["team1"], match["team2"]),
            (match["team2"], match["team1"]),
        ):
            baseline = calculate_team_baseline(match, team)
            if (
                baseline_is_reliable(baseline)
                and odds_min <= baseline["team_odds"] <= odds_max
                and baseline["ev"] > 0
            ):
                candidates.append({
                    "team": team,
                    "opponent": opponent,
                    "score": baseline["score"],
                    "assessed_probability": baseline["assessed_probability"],
                })
    return candidates


def select_analysis_matches(matches: list[dict], limit: int = MAX_AI_MATCHES) -> list[dict]:
    """Keep the Groq prompt bounded, prioritizing matches with the best baseline EV."""
    ranked = []
    for index, match in enumerate(matches):
        baselines = [
            calculate_team_baseline(match, match["team1"]),
            calculate_team_baseline(match, match["team2"]),
        ]
        best_ev = max(
            (baseline["ev"] for baseline in baselines if baseline),
            default=float("-inf"),
        )
        ranked.append((best_ev, -index, match))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[:limit]]


def build_deterministic_report(
    date_str: str,
    matches: list[dict],
    candidates: list[dict],
    bankroll: float | None,
) -> str:
    """Create a complete report without AI so API failures never stop the workflow."""
    match_by_team = {}
    for match in matches:
        match_by_team[normalize_team_name(match["team1"])] = match
        match_by_team[normalize_team_name(match["team2"])] = match

    classified = []
    for candidate in candidates:
        match = match_by_team.get(normalize_team_name(candidate["team"]))
        if not match:
            continue
        baseline = calculate_team_baseline(match, candidate["team"])
        if not baseline:
            continue
        ev = baseline["ev"]
        score = baseline["score"]
        if score > 8 and ev > 0.08:
            grade = "Top Pick"
            stake_pct = 0.03
        elif score > 7 and ev > 0.05:
            grade = "Value Pick"
            stake_pct = 0.02
        elif score > 5.5 and ev > 0:
            grade = "Moderate Pick"
            stake_pct = 0.0
        else:
            continue
        classified.append((ev, candidate, match, baseline, grade, stake_pct))
    classified.sort(key=lambda item: item[0], reverse=True)

    lines = [
        "## MARKET OVERVIEW",
        "",
        f"Python evaluated {len(matches)} verified matches for {date_str}. "
        "Probabilities use de-vigged three-way market odds plus bounded recent-form "
        "and season-record adjustments.",
    ]
    for heading, grades in (
        ("TOP PICKS", {"Top Pick"}),
        ("VALUE PICKS", {"Value Pick", "Moderate Pick"}),
    ):
        lines.extend(["", f"## {heading}", ""])
        entries = [item for item in classified if item[4] in grades]
        if not entries:
            lines.append("None.")
            continue
        for _, candidate, match, baseline, grade, stake_pct in entries:
            opponent = (
                match["team2"] if normalize_team_name(candidate["team"])
                == normalize_team_name(match["team1"]) else match["team1"]
            )
            stake = bankroll * stake_pct if bankroll is not None else None
            if grade == "Moderate Pick":
                stake_text = "; watchlist only, no stake"
            else:
                stake_text = f"; stake €{stake:.2f}" if stake is not None else ""
            lines.append(
                f"- **{candidate['team']} vs {opponent}** — {grade}; "
                f"odds {baseline['team_odds']:.2f}; assessed probability "
                f"{baseline['assessed_probability']:.1%}; EV {baseline['ev']:.2%}; "
                f"score {baseline['score']:.2f}{stake_text}."
            )

    machine_picks = [{
        "team": candidate["team"],
        "opponent": candidate["opponent"],
        "score": round(candidate["score"], 6),
        "assessed_probability": round(candidate["assessed_probability"], 8),
    } for candidate in candidates]
    lines.extend([
        "",
        "## PICKS TO AVOID",
        "",
        "Any team without a positive Python-calculated EV, or whose own odds fall "
        "outside the requested range.",
        "",
        "## DISCLAIMER",
        "",
        "This is a simple market-and-results heuristic, not a calibrated guarantee. "
        "Odds change and betting involves risk. Bet responsibly.",
        "",
        "## MACHINE READABLE PICKS",
        "",
        "```json",
        json.dumps(machine_picks, indent=2, ensure_ascii=False),
        "```",
    ])
    return "\n".join(lines) + "\n"


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
            f"{m['team1']} {m['home_odds']:.2f}, "
            f"Draw {m['draw_odds']:.2f}, "
            f"{m['team2']} {m['away_odds']:.2f}"
            if m.get("home_odds") is not None
            and m.get("away_odds") is not None
            and m.get("draw_odds") is not None
            else str(m.get("odds", "N/A"))
        )
        home_baseline = calculate_team_baseline(m, m["team1"])
        away_baseline = calculate_team_baseline(m, m["team2"])

        def baseline_text(team: str, baseline: dict | None) -> str:
            if not baseline:
                return f"  Python baseline for {team}: unavailable"
            return (
                f"  Python baseline for {team}: market fair "
                f"{baseline['market_probability']:.1%}, evidence adjustment "
                f"{baseline['evidence_adjustment']:+.1%}, assessed "
                f"{baseline['assessed_probability']:.1%}, "
                f"EV {baseline['ev']:.2%}, score {baseline['score']:.2f}"
            )

        match_lines.append(
            f"Match {i}: {m['team1']} vs {m['team2']}\n"
            f"  Tournament: {m['tournament']} ({m['level']})\n"
            f"  Moneyline odds: {market_odds} (source: {m.get('odds_source', 'N/A')})\n"
            f"  ESPN evidence: {m['team1']} form={m.get('home_form') or 'N/A'}, "
            f"record={m.get('home_record') or 'N/A'}; "
            f"{m['team2']} form={m.get('away_form') or 'N/A'}, "
            f"record={m.get('away_record') or 'N/A'}\n"
            f"{baseline_text(m['team1'], home_baseline)}\n"
            f"{baseline_text(m['team2'], away_baseline)}\n"
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

Python has supplied a probability baseline for each team when the full 3-way
market, form, and season record were available. It de-vigged the home/draw/away
prices and applied a bounded evidence adjustment. Treat the Python probability,
score, and EV as authoritative. Do not replace them with your own arithmetic.
Do not recommend a team whose Python baseline is unavailable or has non-positive
EV.

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
- **Moderate Pick / Watchlist** (score > 5.5, EV > 0%; no authorized stake)
- **No Bet** (everything else)
"""

    if bankroll is not None:
        prompt += f"""

### Staking (Tiered Proportional Betting)
Current bankroll: €{bankroll:.2f}

For each recommendation, include:
- Top Pick: €{bankroll * 0.03:.2f} (3% of bankroll)
- Value Pick: €{bankroll * 0.02:.2f} (2% of bankroll)
- Moderate Pick: watchlist only (no stake)
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

### Machine-readable picks (REQUIRED)
End the report with exactly one fenced JSON array under this heading:

## MACHINE READABLE PICKS

```json
[
  {
    "team": "Exact team name from RAW DATA COLLECTED",
    "opponent": "Exact opponent name from RAW DATA COLLECTED",
    "score": 7.5,
    "assessed_probability": 0.62
  }
]
```

Copy the Python baseline score and assessed probability exactly. Include only
teams whose Python baseline EV is positive. Do not include odds, EV, grade, or
stake in this JSON because Python will use the verified market odds and
recalculate those values itself.
Use `[]` when no candidate is supported. The narrative sections must agree with
this array. Never manufacture current form, H2H, injuries, lineups, or xG.
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
    json_blocks = re.findall(
        r"```json\s*(.*?)```",
        report,
        re.IGNORECASE | re.DOTALL,
    )
    for block in reversed(json_blocks):
        try:
            items = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(items, list):
            continue
        structured = []
        for item in items:
            if not isinstance(item, dict) or not item.get("team"):
                continue
            try:
                score = float(item["score"])
                probability = float(item["assessed_probability"])
            except (KeyError, TypeError, ValueError):
                continue
            structured.append({
                "team": str(item["team"]).strip(),
                "opponent": str(item.get("opponent", "")).strip(),
                "score": score,
                "assessed_probability": probability,
            })
        return structured

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


def normalize_team_name(name: str) -> str:
    """Normalize punctuation and spacing for model-to-market comparisons."""
    ascii_name = unicodedata.normalize("NFKD", name.casefold()).encode(
        "ascii",
        "ignore",
    ).decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_name)


def validate_recommendations(
    recommendations: list[dict],
    matches: list[dict],
    odds_min: float | None = None,
    odds_max: float | None = None,
) -> list[dict]:
    """Use verified odds and Python arithmetic to authorize recommendations."""
    validated = []
    for recommendation in recommendations:
        team = recommendation.get("team", "")
        team_key = normalize_team_name(team)
        try:
            score = float(recommendation["score"])
            probability = float(recommendation["assessed_probability"])
        except (KeyError, TypeError, ValueError):
            log(f"  Rejected {team or 'unknown'}: missing score/probability")
            continue
        if probability > 1:
            probability /= 100
        if not 0 < probability < 1 or not 0 <= score <= 10:
            log(f"  Rejected {team or 'unknown'}: invalid score/probability")
            continue

        match_info = None
        verified_team = None
        verified_odds = None
        for match in matches:
            if team_key == normalize_team_name(match["team1"]):
                match_info = match
                verified_team = match["team1"]
                verified_odds = match.get("home_odds") or match.get("odds")
                break
            if team_key == normalize_team_name(match["team2"]):
                match_info = match
                verified_team = match["team2"]
                verified_odds = match.get("away_odds") or match.get("odds")
                break
        if not match_info or verified_odds is None:
            log(f"  Rejected {team or 'unknown'}: no verified team-specific odds")
            continue
        verified_odds = float(verified_odds)
        if (
            (odds_min is not None and verified_odds < odds_min)
            or (odds_max is not None and verified_odds > odds_max)
        ):
            log(
                f"  Rejected {team}: own odds {verified_odds:.2f} outside "
                f"requested range {odds_min}-{odds_max}"
            )
            continue

        baseline = calculate_team_baseline(match_info, verified_team)
        if not baseline:
            log(f"  Rejected {team or 'unknown'}: statistical baseline unavailable")
            continue
        if not baseline_is_reliable(baseline):
            log(
                f"  Rejected {team or 'unknown'}: incomplete, conflicting, "
                "or low-quality market evidence"
            )
            continue
        if (
            abs(probability - baseline["assessed_probability"]) > 0.005
            or abs(score - baseline["score"]) > 0.05
        ):
            log(
                f"  Ignored AI estimate for {team}: using Python baseline "
                f"probability {baseline['assessed_probability']:.2%}, "
                f"score {baseline['score']:.2f}"
            )
        probability = baseline["assessed_probability"]
        score = baseline["score"]
        ev = probability * verified_odds - 1
        if score > 8 and ev > 0.08:
            grade = "Top Pick"
        elif score > 7 and ev > 0.05:
            grade = "Value Pick"
        elif score > 5.5 and ev > 0:
            grade = "Moderate Pick"
        else:
            log(
                f"  Rejected {team}: score {score:.2f}, "
                f"recalculated EV {ev:.2%}"
            )
            continue

        validated.append({
            **recommendation,
            "team": verified_team,
            "score": score,
            "assessed_probability": probability,
            "odds": verified_odds,
            "ev": ev,
            "grade": grade,
            "match": match_info,
            "baseline": baseline,
        })
        log(
            f"  Validated {team}: {grade}, score {score:.2f}, EV {ev:.2%}"
        )
    return validated


def select_portfolio(
    recommendations: list[dict],
    max_exposure: float = MAX_DAILY_EXPOSURE,
    max_bets: int = MAX_DAILY_BETS,
) -> list[dict]:
    """Select the strongest non-duplicated bets within a daily risk budget."""
    stake_rates = {"Top Pick": 0.03, "Value Pick": 0.02}
    ranked = sorted(
        recommendations,
        key=lambda rec: (rec.get("ev", 0), rec.get("score", 0)),
        reverse=True,
    )
    selected = []
    seen_matches = set()
    exposure = 0.0
    for recommendation in ranked:
        stake_rate = stake_rates.get(recommendation.get("grade"))
        match = recommendation.get("match") or {}
        if stake_rate is None or not match:
            continue
        match_key = tuple(sorted((
            normalize_team_name(match.get("team1", "")),
            normalize_team_name(match.get("team2", "")),
        )))
        if match_key in seen_matches:
            log(f"  Portfolio rejected {recommendation['team']}: match already selected")
            continue
        if len(selected) >= max_bets or exposure + stake_rate > max_exposure + 1e-9:
            log(f"  Portfolio rejected {recommendation['team']}: daily risk cap reached")
            continue
        selected.append(recommendation)
        seen_matches.add(match_key)
        exposure += stake_rate
    log(
        f"Portfolio selected {len(selected)} bet(s) with planned exposure "
        f"{exposure:.1%}"
    )
    return selected


def append_prediction_audit(
    date_str: str,
    matches: list[dict],
    recommendations: list[dict],
    authorized: list[dict],
):
    """Persist every modelled team, including rejected and watchlist outcomes."""
    headers = [
        "DATE", "EVENT_ID", "MATCH", "PICK", "OPENING_ODDS",
        "MARKET_PROBABILITY", "MODEL_PROBABILITY", "EV", "SCORE",
        "EVIDENCE", "DECISION", "REASON", "RESULT", "CLOSING_ODDS", "CLV",
    ]
    if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size:
        with open(AUDIT_FILE, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            old_rows, old_headers = list(reader), reader.fieldnames or []
        if old_headers != headers:
            for row in old_rows:
                row.setdefault("REASON", "legacy")
            with open(AUDIT_FILE, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                writer.writeheader(); writer.writerows(old_rows)
    validated = {normalize_team_name(item["team"]): item for item in recommendations}
    selected = {normalize_team_name(item["team"]) for item in authorized}
    existing = set()
    if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size:
        with open(AUDIT_FILE, newline="", encoding="utf-8") as handle:
            existing = {
                (row.get("DATE", ""), row.get("EVENT_ID", ""), row.get("PICK", ""))
                for row in csv.DictReader(handle)
            }
    rows = []
    for match in matches:
        for team in (match["team1"], match["team2"]):
            baseline = calculate_team_baseline(match, team)
            if not baseline:
                continue
            key = (date_str, match.get("event_id", ""), team)
            if key in existing:
                continue
            item = validated.get(normalize_team_name(team))
            decision = (
                item["grade"] if normalize_team_name(team) in selected
                else "Watchlist" if item else "Rejected"
            )
            if normalize_team_name(team) in selected:
                reason = "authorized"
            elif item and item.get("grade") in {"Top Pick", "Value Pick"}:
                reason = "portfolio_limit"
            elif item:
                reason = "below_staking_threshold"
            elif not baseline_is_reliable(baseline):
                reason = "insufficient_or_conflicting_evidence"
            elif baseline["ev"] <= 0:
                reason = "non_positive_ev"
            else:
                reason = "not_selected"
            rows.append([
                date_str, match.get("event_id", ""),
                f"{match['team1']} vs {match['team2']}", team,
                f"{baseline['team_odds']:.3f}",
                f"{baseline['market_probability']:.6f}",
                f"{baseline['assessed_probability']:.6f}",
                f"{baseline['ev']:.6f}", f"{baseline['score']:.3f}",
                "reliable" if baseline_is_reliable(baseline) else "insufficient",
                decision, reason, "", "", "",
            ])
    if not rows:
        return
    write_header = not AUDIT_FILE.exists() or not AUDIT_FILE.stat().st_size
    with open(AUDIT_FILE, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if write_header:
            writer.writerow(headers)
        writer.writerows(rows)
    log(f"Audited {len(rows)} evaluated team(s) to {AUDIT_FILE.name}")


def settle_pending_bets() -> int:
    """Settle completed bets from ESPN and credit returns to the bankroll."""
    if not LOG_FILE.exists() or not LOG_FILE.stat().st_size:
        return 0
    with open(LOG_FILE, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    pending_dates = sorted({row.get("DATE", "") for row in rows if not row.get("RESULT", "").strip()})
    events_by_date = {date: fetch_matches_from_espn_api(date) for date in pending_dates if date}
    settled = 0
    credited = 0.0
    for row in rows:
        if row.get("RESULT", "").strip():
            continue
        match_label = normalize_team_name(row.get("MATCH", ""))
        pick = normalize_team_name(re.sub(r"\s+to win\s*$", "", row.get("BET", ""), flags=re.I))
        match = next((item for item in events_by_date.get(row.get("DATE", ""), [])
                      if normalize_team_name(item["team1"]) in match_label
                      and normalize_team_name(item["team2"]) in match_label), None)
        if not match or not match.get("completed"):
            continue
        if pick == normalize_team_name(match["team1"]):
            won = match.get("home_winner")
            closing = match.get("home_odds")
        elif pick == normalize_team_name(match["team2"]):
            won = match.get("away_winner")
            closing = match.get("away_odds")
        else:
            continue
        stake = float(row.get("STAKE") or 0)
        odds = float(row.get("ODDS") or 0)
        returned = stake * odds if won else 0.0
        row["RESULT"] = "W" if won else "L"
        row["RETURN"] = f"{returned:.2f}"
        credited += returned
        settled += 1
        update_audit_result(row.get("DATE", ""), pick, row["RESULT"], closing)
    if settled:
        headers = ["DATE", "MATCH", "BET", "ODDS", "STAKE", "RESULT", "RETURN", "STARTING BALANCE"]
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        balance = float(BANKROLL_FILE.read_text().strip() or 0) + credited
        BANKROLL_FILE.write_text(f"{balance:.2f}", encoding="utf-8")
        log(f"Settled {settled} bet(s); credited €{credited:.2f}")
    return settled


def update_audit_result(date_str: str, pick_key: str, result: str, closing_odds):
    if not AUDIT_FILE.exists() or not AUDIT_FILE.stat().st_size:
        return
    with open(AUDIT_FILE, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
        headers = handle.fieldnames
    changed = False
    for row in rows:
        if row["DATE"] == date_str and normalize_team_name(row["PICK"]) == pick_key:
            row["RESULT"] = result
            if closing_odds:
                row["CLOSING_ODDS"] = f"{closing_odds:.3f}"
                opening = float(row.get("OPENING_ODDS") or 0)
                row["CLV"] = f"{opening / closing_odds - 1:.6f}" if opening else ""
            changed = True
    if changed:
        with open(AUDIT_FILE, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)


def generate_performance_summary():
    """Build an evidence report from settled bets and audited probabilities."""
    bets = []
    if LOG_FILE.exists() and LOG_FILE.stat().st_size:
        with open(LOG_FILE, newline="", encoding="utf-8") as handle:
            bets = list(csv.DictReader(handle))
    settled = [row for row in bets if row.get("RESULT") in {"W", "L"}]
    stakes = sum(float(row.get("STAKE") or 0) for row in settled)
    profit = sum(float(row.get("RETURN") or 0) - float(row.get("STAKE") or 0) for row in settled)
    wins = sum(row.get("RESULT") == "W" for row in settled)
    audit = []
    if AUDIT_FILE.exists() and AUDIT_FILE.stat().st_size:
        with open(AUDIT_FILE, newline="", encoding="utf-8") as handle:
            audit = list(csv.DictReader(handle))
    resolved = [row for row in audit if row.get("RESULT") in {"W", "L"}]
    brier = None
    if resolved:
        brier = sum(
            (float(row["MODEL_PROBABILITY"]) - (1 if row["RESULT"] == "W" else 0)) ** 2
            for row in resolved if row.get("MODEL_PROBABILITY")
        ) / len(resolved)
    clv_values = [float(row["CLV"]) for row in resolved if row.get("CLV")]
    lines = [
        "# Football Bot Performance",
        "",
        f"- Settled bets: {len(settled)}",
        f"- Win rate: {wins / len(settled):.1%}" if settled else "- Win rate: N/A",
        f"- Profit/loss: €{profit:.2f}",
        f"- ROI: {profit / stakes:.2%}" if stakes else "- ROI: N/A",
        f"- Brier score: {brier:.4f}" if brier is not None else "- Brier score: N/A",
        f"- Average CLV: {sum(clv_values) / len(clv_values):.2%}" if clv_values else "- Average CLV: N/A",
        "",
        "## Calibration",
        "",
        "| Predicted probability | Predictions | Actual win rate |",
        "|---|---:|---:|",
    ]
    for low, high in ((0.50, .55), (.55, .60), (.60, .65), (.65, .70), (.70, 1.01)):
        bucket = [row for row in resolved if row.get("MODEL_PROBABILITY") and low <= float(row["MODEL_PROBABILITY"]) < high]
        label = f"{low:.0%}–{high:.0%}" if high <= 1 else "70%+"
        actual = sum(row["RESULT"] == "W" for row in bucket) / len(bucket) if bucket else None
        lines.append(f"| {label} | {len(bucket)} | {actual:.1%} |" if actual is not None else f"| {label} | 0 | N/A |")
    PERFORMANCE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Performance summary saved: {PERFORMANCE_FILE.name}")


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
    existing_bets = set()
    if file_exists and LOG_FILE.stat().st_size > 0:
        with open(LOG_FILE, newline="", encoding="utf-8") as existing_file:
            for row in csv.DictReader(existing_file):
                existing_bets.add((
                    row.get("DATE", "").strip(),
                    normalize_team_name(
                        re.sub(r"\s+to win\s*$", "", row.get("BET", ""), flags=re.I)
                    ),
                ))

    for rec in recommendations:
        if rec["grade"] not in ("Top Pick", "Value Pick"):
            continue
        bet_key = (date_str, normalize_team_name(rec["team"]))
        if bet_key in existing_bets:
            log(f"  Skipped duplicate logged bet: {rec['team']} on {date_str}")
            continue

        match_info = rec.get("match")
        if not match_info:
            log(f"  Skipped {rec['team']}: missing validated match")
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
        existing_bets.add(bet_key)

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

def add_validation_summary(
    report: str,
    candidate_count: int,
    recommendations: list[dict],
) -> str:
    """Append Python's authoritative betting decision to the AI report."""
    lines = [
        "",
        "## PYTHON VALIDATION RESULT",
        "",
        (
            f"The analysis produced {candidate_count} candidate(s). "
            f"Python accepted {len(recommendations)} bet(s) after matching "
            "verified team-specific odds and recalculating expected value."
        ),
    ]
    if recommendations:
        for rec in recommendations:
            lines.append(
                f"- **{rec['team']}** — {rec['grade']}, odds "
                f"{rec['odds']:.2f}, assessed probability "
                f"{rec['assessed_probability']:.1%}, verified EV {rec['ev']:.2%}."
            )
    else:
        lines.extend([
            "",
            "**Final betting decision: NO BETS.** Any narrative picks above were "
            "rejected and must not be treated as recommendations.",
        ])
    return report.rstrip() + "\n" + "\n".join(lines) + "\n"


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

    settle_pending_bets()
    generate_performance_summary()

    if args.settle_only:
        log("Settlement-only run complete")
        return

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

    statistical_candidates = build_statistical_candidates(
        qualified,
        odds_min,
        odds_max,
    )
    log(f"Found {len(statistical_candidates)} positive-EV statistical candidates")
    analysis_matches = select_analysis_matches(qualified)
    log(
        f"Building bounded analysis prompt with {len(analysis_matches)}/"
        f"{len(qualified)} qualifying matches..."
    )
    prompt = build_prompt(date_str, analysis_matches, bankroll, odds_min, odds_max)

    api_key = os.environ.get("GROQ_API_KEY")
    report = None
    if api_key:
        try:
            report = call_ai(prompt, api_key)
        except requests.RequestException:
            log("Groq unavailable; continuing with deterministic Python report")
    else:
        log("No GROQ_API_KEY; continuing with deterministic Python report")
    if report is None:
        report = build_deterministic_report(
            date_str,
            qualified,
            statistical_candidates,
            bankroll,
        )

    ai_candidates = parse_recommendations(report)
    log(f"Parsed {len(ai_candidates)} AI recommendation candidates from report")
    candidates_by_team = {
        normalize_team_name(candidate["team"]): candidate
        for candidate in ai_candidates
    }
    for candidate in statistical_candidates:
        candidates_by_team[normalize_team_name(candidate["team"])] = candidate
    candidates = list(candidates_by_team.values())
    log(f"Validating {len(candidates)} unique recommendation candidates")
    recommendations = validate_recommendations(
        candidates,
        qualified,
        odds_min,
        odds_max,
    )
    log(f"Validated {len(recommendations)} recommendations")
    validated_bets = [
        recommendation
        for recommendation in recommendations
        if recommendation["grade"] in ("Top Pick", "Value Pick")
    ]
    authorized_bets = select_portfolio(validated_bets)
    append_prediction_audit(date_str, qualified, recommendations, authorized_bets)
    log(f"Authorized {len(authorized_bets)} Top/Value bets for logging")
    total_stake = log_bets(date_str, authorized_bets, qualified, bankroll)

    save_bankroll(bankroll, total_stake)
    final_report = add_validation_summary(
        report,
        len(candidates),
        authorized_bets,
    )
    save_report(date_str, final_report)
    generate_performance_summary()

    log("=== Done ===")
    print("\n" + final_report)


if __name__ == "__main__":
    main()
