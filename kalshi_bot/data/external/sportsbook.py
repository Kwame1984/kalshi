"""ESPN hidden-API sportsbook moneylines -> devigged MLB win probabilities.

One free unauthenticated call:
    https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates=YYYYMMDD
events[].competitions[].odds[] carries the book's moneyline. As of Jul 2026
ESPN's odds partner is DraftKings (it was ESPN BET when this pattern was
first documented); the moneyline lives at odds[].moneyline.{home,away} with
"current"/"close"/"open" phases holding American-odds strings ("+159").
The sports.core.api.espn.com odds ref was probed too and serves the SAME
single provider (count=1, DraftKings), so there is no multi-provider
consensus available from ESPN — one soft line is all we get.

HONESTY / KNOWN LIMITATION: DraftKings (like ESPN BET) is a *soft* retail
book. Its devigged moneyline is a decent first-order anchor but it is NOT a
sharp fair value: soft books shade toward public money and lag sharp moves.
Pinnacle is the accepted sharp reference; The Odds API's free tier serves
Pinnacle but requires a signup key we don't have. Treat probabilities from
this module as "book consensus proxy" for measuring Kalshi-vs-book
divergence, not as a tradable fair value in itself.

De-vig helpers implemented (two-outcome):
  * multiplicative — normalize implied probs to sum 1 (the standard);
  * power          — solve sum(p_i^k) = 1, favorite-longshot aware;
  * Shin (1992/93) — insider-trading model, z solved so probs sum to 1.
For MLB moneylines (both probs near 0.3-0.7) the three agree to ~1e-3;
they diverge materially only for longshots.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import httpx

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
)

# ESPN team abbreviation -> Kalshi KXMLBGAME ticker code.
# Built empirically 2026-07-07 by crossing the ESPN scoreboard slate against
# every open KXMLBGAME event (all 30 teams appeared). Only two differ today,
# but ESPN occasionally renames abbreviations (OAK->ATH in 2025, WAS<->WSH
# flip-flops) — EXTEND THIS MAP when a scoreboard team fails to match.
ESPN_TO_KALSHI: dict[str, str] = {
    "ARI": "AZ",   # Diamondbacks
    "ATH": "ATH",
    "ATL": "ATL",
    "BAL": "BAL",
    "BOS": "BOS",
    "CHC": "CHC",
    "CHW": "CWS",  # White Sox
    "CIN": "CIN",
    "CLE": "CLE",
    "COL": "COL",
    "DET": "DET",
    "HOU": "HOU",
    "KC": "KC",
    "LAA": "LAA",
    "LAD": "LAD",
    "MIA": "MIA",
    "MIL": "MIL",
    "MIN": "MIN",
    "NYM": "NYM",
    "NYY": "NYY",
    "PHI": "PHI",
    "PIT": "PIT",
    "SD": "SD",
    "SEA": "SEA",
    "SF": "SF",
    "STL": "STL",
    "TB": "TB",
    "TEX": "TEX",
    "TOR": "TOR",
    "WSH": "WSH",
}


@dataclass
class GameOdds:
    """One MLB game's book snapshot. Team codes are ESPN abbreviations."""

    home: str
    away: str
    start_time: dt.datetime          # aware UTC
    ml_home: float | None            # American odds, e.g. -194
    ml_away: float | None
    provider: str
    state: str                       # 'pre' | 'in' | 'post'
    espn_id: str = ""

    @property
    def kalshi_home(self) -> str:
        return ESPN_TO_KALSHI.get(self.home, self.home)

    @property
    def kalshi_away(self) -> str:
        return ESPN_TO_KALSHI.get(self.away, self.away)

    def has_moneyline(self) -> bool:
        return self.ml_home is not None and self.ml_away is not None


# ---------------------------------------------------------------------------
# devig helpers
# ---------------------------------------------------------------------------

def american_to_prob(ml: float | int | str | None) -> float | None:
    """American odds -> implied probability WITH the vig still in it."""
    if ml is None:
        return None
    if isinstance(ml, str):
        t = ml.strip().upper()
        if t in ("EVEN", "EV"):
            return 0.5
        if t in ("", "OFF", "N/A", "--"):
            return None
        try:
            ml = float(t.replace("+", ""))
        except ValueError:
            return None
    ml = float(ml)
    if ml == 0:
        return None
    if ml < 0:
        return -ml / (-ml + 100.0)
    return 100.0 / (ml + 100.0)


def devig_multiplicative(p_a: float, p_b: float) -> tuple[float, float]:
    """Normalize the two vigged probs to sum to 1."""
    s = p_a + p_b
    return p_a / s, p_b / s


def devig_power(p_a: float, p_b: float, tol: float = 1e-12) -> tuple[float, float]:
    """Solve p_a^k + p_b^k = 1 for k (bisection). k>1 when overround > 0.

    The power method removes proportionally MORE vig from the longshot,
    consistent with the favorite-longshot bias soft books price in.
    """
    def f(k: float) -> float:
        return p_a ** k + p_b ** k - 1.0

    lo, hi = 1.0, 2.0
    if f(lo) < 0:            # underround book (arb) — degenerate, search down
        lo = 0.25
    while f(hi) > 0:
        hi *= 2
        if hi > 64:
            break
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = 0.5 * (lo + hi)
    qa, qb = p_a ** k, p_b ** k
    s = qa + qb              # residual ~tol; renormalize exactly
    return qa / s, qb / s


def devig_shin(p_a: float, p_b: float, tol: float = 1e-12) -> tuple[float, float]:
    """Shin's insider-trading devig for two outcomes.

    p_i = (sqrt(z^2 + 4(1-z) pi_i^2 / B) - z) / (2(1-z)), z solved so the
    probs sum to 1 (z = implied fraction of insider money).
    """
    B = p_a + p_b
    if B <= 1.0:             # no overround; nothing to strip
        return devig_multiplicative(p_a, p_b)

    def probs(z: float) -> tuple[float, float]:
        d = 2.0 * (1.0 - z)
        qa = (math.sqrt(z * z + 4.0 * (1.0 - z) * p_a * p_a / B) - z) / d
        qb = (math.sqrt(z * z + 4.0 * (1.0 - z) * p_b * p_b / B) - z) / d
        return qa, qb

    def f(z: float) -> float:
        qa, qb = probs(z)
        return qa + qb - 1.0

    lo, hi = 0.0, 0.5        # f(0) = sqrt(B) - 1 > 0; f decreasing in z
    while f(hi) > 0 and hi < 0.99:
        hi = min(0.99, hi + 0.25)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    qa, qb = probs(0.5 * (lo + hi))
    s = qa + qb
    return qa / s, qb / s


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def _american_from_side(side: dict) -> float | None:
    """odds[].moneyline.{home,away}: prefer current, then close, then open."""
    for phase in ("current", "close", "open"):
        entry = side.get(phase) or {}
        p = american_to_prob(entry.get("odds"))
        if p is not None:
            # return the raw american, not the prob — re-parse below
            t = str(entry["odds"]).strip().upper()
            if t in ("EVEN", "EV"):
                return 100.0
            return float(t.replace("+", ""))
    return None


def _extract_moneylines(odds_obj: dict) -> tuple[float | None, float | None]:
    ml = odds_obj.get("moneyline") or {}
    ml_home = _american_from_side(ml.get("home") or {})
    ml_away = _american_from_side(ml.get("away") or {})
    if ml_home is None:      # legacy shape: numeric on {home,away}TeamOdds
        ml_home = (odds_obj.get("homeTeamOdds") or {}).get("moneyLine")
    if ml_away is None:
        ml_away = (odds_obj.get("awayTeamOdds") or {}).get("moneyLine")
    return (
        float(ml_home) if ml_home is not None else None,
        float(ml_away) if ml_away is not None else None,
    )


async def fetch_mlb_moneylines(
    date: dt.date | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[GameOdds]:
    """One scoreboard call -> per-game moneylines for `date` (ESPN's ET day).

    Games whose odds haven't posted yet (e.g. game 2 of a doubleheader)
    come back with ml_home/ml_away = None so callers can report them.
    """
    params = {"dates": date.strftime("%Y%m%d")} if date else None
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        r = await client.get(SCOREBOARD_URL, params=params)
        r.raise_for_status()
        data = r.json()
    finally:
        if own_client:
            await client.aclose()

    games: list[GameOdds] = []
    for event in data.get("events", []):
        comps = event.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in teams or "away" not in teams:
            continue
        ml_home = ml_away = None
        provider = ""
        for odds_obj in comp.get("odds") or []:
            ml_home, ml_away = _extract_moneylines(odds_obj)
            if ml_home is not None and ml_away is not None:
                provider = (odds_obj.get("provider") or {}).get("name", "")
                break
        games.append(GameOdds(
            home=teams["home"]["team"]["abbreviation"],
            away=teams["away"]["team"]["abbreviation"],
            start_time=dt.datetime.fromisoformat(
                event["date"].replace("Z", "+00:00")
            ),
            ml_home=ml_home,
            ml_away=ml_away,
            provider=provider,
            state=(event.get("status", {}).get("type", {}) or {}).get("state", ""),
            espn_id=str(event.get("id", "")),
        ))
    return games
