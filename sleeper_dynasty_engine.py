#!/usr/bin/env python3
"""
sleeper_dynasty_engine.py

One script for a Sleeper dynasty league: pull the full league picture AND
generate quantified trade + waiver recommendations for your team.

Supersedes sleeper_snapshot.py - everything that script produced is still
here (settings, scoring, every roster, keepers, IR/taxi, traded-pick ledger,
full draft board), now with a valuation attached to every asset.

Outputs
-------
  league_report.md     everything readable, in one file:
                         1. trade + waiver recommendations
                         2. league snapshot (rosters, picks, draft, warnings)
  league_snapshot.json  everything structured, including the recommendations

Model, in one paragraph
-----------------------
Every asset (player or future rookie pick) gets a dynasty value on a common
0-10000 scale. That value is a blend of (a) a market anchor from FantasyCalc's
public dynasty API, shaped to THIS league (12 team, 1QB, full PPR), and (b) a
bottom-up production model: league-specific projected points computed from your
exact scoring_settings, converted to VORP against a replacement level derived
from your actual starting-lineup requirements, then extended over a multi-year
horizon through a positional aging curve and discounted. Injuries and depth
chart position adjust both the asset value (mildly) and the weekly lineup value
(severely). Each roster is then scored for lineup strength, positional
surplus/deficit and contention timeline. Trades are enumerated over bounded
packages, valued for BOTH sides using each side's own needs and timeline, and
assigned an acceptance probability by Monte Carlo over per-player value
uncertainty plus a "everyone wants to win the trade" surplus threshold.

Stdlib only. No API keys. Sleeper's read endpoints are public.

Usage
-----
  python3 sleeper_dynasty_engine.py
  python3 sleeper_dynasty_engine.py --me ChuckFarbs --top 10 --max-package 2
  python3 sleeper_dynasty_engine.py --win-now 0.8         # force contender mode
  python3 sleeper_dynasty_engine.py --untouchable "Jeremiyah Love,Tre' Harris"
  python3 sleeper_dynasty_engine.py --snapshot-only       # old script behaviour
  python3 sleeper_dynasty_engine.py --offline             # re-run on cached data
  python3 sleeper_dynasty_engine.py --no-market           # skip FantasyCalc
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

# ---------------------------------------------------------------------------
# CONFIG - everything you'd want to tune lives here
# ---------------------------------------------------------------------------

LEAGUE_ID = "1316902230451818496"
MY_USERNAME = "ChuckFarbs"          # override with --me

BASE = "https://api.sleeper.app/v1"
PROJ_BASE = "https://api.sleeper.app/projections/nfl"
STATS_BASE = "https://api.sleeper.app/stats/nfl"
FANTASYCALC = "https://api.fantasycalc.com/values/current"

CACHE_DIR = ".sleeper_cache"
PLAYER_CACHE = "players_nfl.json"
PLAYER_CACHE_MAX_AGE_HOURS = 24      # Sleeper asks for <=1 pull/day
MARKET_CACHE_MAX_AGE_HOURS = 12
PROJ_CACHE_MAX_AGE_HOURS = 6

VALUE_SCALE = 10000.0                # top asset in the league lands near this

# --- dynasty horizon -------------------------------------------------------
HORIZON_YEARS = 6                    # how far ahead dynasty value looks
ANNUAL_DISCOUNT = 0.82               # per-year discount on future production

# --- blending --------------------------------------------------------------
W_MARKET = 0.65                      # weight on FantasyCalc market anchor
W_PRODUCTION = 0.35                  # weight on bottom-up league-scored model
                                     # (renormalised if market is unavailable)

# --- how much a point of weekly starting-lineup gain is worth, relative to
#     raw dynasty asset value. Computed empirically at runtime, then scaled by
#     WIN_NOW_WEIGHT which is derived from your contention score (or --win-now).
WIN_NOW_FLOOR, WIN_NOW_CEIL = 0.30, 3.50

# --- injury multipliers ----------------------------------------------------
# asset = long-term dynasty hit, lineup = this-week availability hit
INJURY_MULT = {
    None:            (1.00, 1.00),
    "":              (1.00, 1.00),
    "Questionable":  (0.99, 0.80),
    "Doubtful":      (0.98, 0.30),
    "Out":           (0.97, 0.00),
    "IR":            (0.86, 0.00),
    "PUP":           (0.84, 0.00),
    "Sus":           (0.90, 0.00),
    "COV":           (0.98, 0.35),
    "NA":            (0.80, 0.00),
    "DNR":           (0.70, 0.00),
}

# --- depth chart multipliers, applied to the production model only ----------
DEPTH_MULT = {
    "QB": {1: 1.00, 2: 0.35, 3: 0.12},
    "RB": {1: 1.00, 2: 0.62, 3: 0.32},
    "WR": {1: 1.00, 2: 0.86, 3: 0.64, 4: 0.38},
    "TE": {1: 1.00, 2: 0.45, 3: 0.20},
}
DEPTH_DEFAULT = 0.25

# --- value uncertainty (drives acceptance-probability Monte Carlo) ----------
SIGMA_BASE = 0.17
SIGMA_ROOKIE = 0.16                  # years_exp <= 1
SIGMA_YOUNG = 0.07                   # age <= 22
SIGMA_INJURED = 0.09
SIGMA_NO_PROJ = 0.11
SIGMA_BURIED = 0.07                  # depth_chart_order >= 3
SIGMA_PICK = 0.30
SIGMA_CAP = 0.55

# --- acceptance model ------------------------------------------------------
SURPLUS_MEAN = 0.07                  # partner wants to "win" by ~7% on average
SURPLUS_SD = 0.055
MC_DRAWS = 1500
PACKAGE_FRICTION = 0.90              # per extra asset beyond 2 total
BEST_PLAYER_PENALTY = 0.12           # partner dislikes shipping the best player
MIN_ACCEPT_TO_SHOW = 0.04
MIN_PACKAGE_FRAC = 0.12      # deal must be worth >= this * a 1.01 pick
FA_NEWS_MAX_DAYS = 120       # free agents staler than this aren't real options

# --- trade search bounds ---------------------------------------------------
TOP_ASSETS_PER_SIDE = 20             # consider this many tradeable assets/team
VALUE_BAND = 0.45                    # prune packages more lopsided than this
MAX_CANDIDATES_TO_SIMULATE = 900     # cheap screen -> expensive Monte Carlo

# --- positions -------------------------------------------------------------
OFFENSE = ("QB", "RB", "WR", "TE")
ALL_FANTASY = ("QB", "RB", "WR", "TE", "K", "DEF")
FLEX_ELIGIBLE = {
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
SLOT_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF",
              "REC_FLEX", "WRRB_FLEX", "FLEX", "SUPER_FLEX"]

# K and DEF are streamed in every format that matters; give them near-zero
# dynasty asset value but keep their weekly lineup contribution.
ASSET_VALUE_POS_MULT = {"K": 0.02, "DEF": 0.03}

# ---------------------------------------------------------------------------
# Aging curves: fraction of peak production retained at a given age.
# Anchor points, linearly interpolated, flat-extrapolated at the ends.
# ---------------------------------------------------------------------------
AGE_CURVES = {
    "RB": [(20, 0.86), (21, 0.92), (22, 0.97), (23, 1.00), (24, 1.00), (25, 0.98),
           (26, 0.93), (27, 0.85), (28, 0.75), (29, 0.62), (30, 0.48), (31, 0.36),
           (32, 0.26), (33, 0.17), (34, 0.10), (36, 0.03)],
    "WR": [(20, 0.66), (21, 0.74), (22, 0.84), (23, 0.92), (24, 0.97), (25, 1.00),
           (26, 1.00), (27, 0.99), (28, 0.96), (29, 0.91), (30, 0.84), (31, 0.74),
           (32, 0.62), (33, 0.50), (34, 0.38), (35, 0.27), (37, 0.10)],
    "TE": [(21, 0.48), (22, 0.58), (23, 0.70), (24, 0.81), (25, 0.90), (26, 0.96),
           (27, 1.00), (28, 1.00), (29, 0.97), (30, 0.92), (31, 0.84), (32, 0.73),
           (33, 0.61), (34, 0.48), (36, 0.22), (38, 0.06)],
    "QB": [(21, 0.76), (22, 0.82), (23, 0.88), (24, 0.92), (25, 0.96), (26, 0.98),
           (27, 1.00), (30, 1.00), (32, 1.00), (33, 0.97), (34, 0.93), (35, 0.88),
           (36, 0.81), (37, 0.73), (38, 0.63), (39, 0.51), (41, 0.25)],
    "K":  [(22, 1.00), (38, 1.00)],
    "DEF": [(0, 1.00), (99, 1.00)],
}
DEFAULT_AGE = {"QB": 26, "RB": 25, "WR": 26, "TE": 27, "K": 29, "DEF": 0}


def age_curve(pos: str, age: float) -> float:
    pts = AGE_CURVES.get(pos) or AGE_CURVES["WR"]
    if age <= pts[0][0]:
        return pts[0][1]
    if age >= pts[-1][0]:
        return max(0.0, pts[-1][1])
    for (a0, v0), (a1, v1) in zip(pts, pts[1:]):
        if a0 <= age <= a1:
            t = (age - a0) / (a1 - a0) if a1 != a0 else 0.0
            return v0 + t * (v1 - v0)
    return pts[-1][1]


def remaining_career_factor(pos: str, age: float) -> float:
    """Discounted sum of retained production over the dynasty horizon.

    Normalised so a player sitting at his positional peak with a full runway
    scores ~1.0. This is what separates a 23yo WR from a 30yo WR who are
    putting up identical numbers right now.
    """
    if pos in ("K", "DEF"):
        return 1.0
    raw = sum(ANNUAL_DISCOUNT ** t * age_curve(pos, age + t) for t in range(HORIZON_YEARS))
    peak_age = max(AGE_CURVES.get(pos, AGE_CURVES["WR"]), key=lambda p: p[1])[0]
    norm = sum(ANNUAL_DISCOUNT ** t * age_curve(pos, peak_age + t) for t in range(HORIZON_YEARS))
    return raw / norm if norm else 0.0


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

OFFLINE = False
NEWS_REFERENCE_MS = 0        # newest news timestamp in the dump = "now"
_HTTP_FAILURES: list[str] = []


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, safe + ".json")


def fetch_json(url: str, cache_key: str | None = None, max_age_h: float = 0.0,
               required: bool = True, timeout: int = 60):
    """GET JSON with an on-disk cache. Returns None on failure when not required."""
    path = _cache_path(cache_key) if cache_key else None
    if path and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        if OFFLINE or age_h < max_age_h:
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    if OFFLINE:
        if required:
            raise SystemExit(f"--offline but no cache for {cache_key or url}")
        _HTTP_FAILURES.append(f"{cache_key or url}: no cache in offline mode")
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "sleeper-dynasty-engine/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if path and os.path.exists(path):          # stale beats nothing
            with open(path) as f:
                return json.load(f)
        _HTTP_FAILURES.append(f"{cache_key or url}: {e}")
        if required:
            raise
        return None
    if path:
        with open(path, "w") as f:
            json.dump(data, f)
    return data


def get(path: str, cache_key: str | None = None, max_age_h: float = 0.0, required=True):
    return fetch_json(f"{BASE}{path}", cache_key, max_age_h, required)


def load_players() -> dict:
    """The full player dump is ~15MB. Cache it; Sleeper asks for one pull/day."""
    if os.path.exists(PLAYER_CACHE):
        age_h = (time.time() - os.path.getmtime(PLAYER_CACHE)) / 3600
        if OFFLINE or age_h < PLAYER_CACHE_MAX_AGE_HOURS:
            print(f"  using cached player file ({age_h:.1f}h old)")
            with open(PLAYER_CACHE) as f:
                return json.load(f)
    if OFFLINE:
        raise SystemExit("--offline requires an existing players_nfl.json")
    print("  downloading player dump (~15MB, once per day)...")
    players = fetch_json(f"{BASE}/players/nfl", timeout=180)
    with open(PLAYER_CACHE, "w") as f:
        json.dump(players, f)
    return players


# ---------------------------------------------------------------------------
# Projections + league-specific scoring
# ---------------------------------------------------------------------------

def fetch_projections(season: str, week: int | None = None) -> dict:
    """Sleeper's projection endpoints. Undocumented but public and stable-ish.

    Season-long:  /projections/nfl/<season>?season_type=regular&position[]=QB...
    Weekly:       /projections/nfl/<season>/<week>?...
    Returns {player_id: {stat_key: value}}. Empty dict if unavailable - the
    production model then falls back to the search_rank prior.
    """
    qs = urllib.parse.urlencode(
        [("season_type", "regular"), ("order_by", "pts_ppr")]
        + [("position[]", p) for p in ALL_FANTASY]
    )
    url = f"{PROJ_BASE}/{season}" + (f"/{week}" if week else "") + "?" + qs
    key = f"proj_{season}" + (f"_w{week}" if week else "")
    raw = fetch_json(url, key, PROJ_CACHE_MAX_AGE_HOURS, required=False)
    out: dict[str, dict] = {}
    if not raw:
        return out
    rows = raw if isinstance(raw, list) else raw.get("data") or []
    for row in rows:
        pid = str(row.get("player_id") or (row.get("player") or {}).get("player_id") or "")
        stats = row.get("stats") or {}
        if pid and stats:
            out[pid] = stats
    return out


def score_stats(stats: dict, scoring: dict) -> float:
    """Apply the league's own scoring_settings to a stat line.

    This is the whole point of doing it here instead of using somebody's PPR
    rank: your league pays 0.04/passing yard and 4 per passing TD, full PPR,
    no TE premium, and -1 per missed FG. Those choices move players.
    """
    total = 0.0
    for k, v in stats.items():
        w = scoring.get(k)
        if w and isinstance(v, (int, float)):
            total += w * v
    return total


# ---------------------------------------------------------------------------
# Market anchor (FantasyCalc public dynasty values)
# ---------------------------------------------------------------------------

def fetch_market_values(num_teams: int, num_qbs: int, ppr: float, is_dynasty: bool):
    """Returns (by_sleeper_id, pick_values_by_label). Both {} if unreachable."""
    qs = urllib.parse.urlencode({
        "isDynasty": str(bool(is_dynasty)).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
        "includeAdp": "false",
    })
    raw = fetch_json(f"{FANTASYCALC}?{qs}", "market_fantasycalc",
                     MARKET_CACHE_MAX_AGE_HOURS, required=False)
    by_sleeper, picks = {}, {}
    if not raw:
        return by_sleeper, picks
    rows = raw if isinstance(raw, list) else raw.get("players") or []
    for row in rows:
        p = row.get("player") or {}
        val = row.get("value") or row.get("combinedValue") or 0
        sid = p.get("sleeperId")
        name = (p.get("name") or "").strip()
        pos = (p.get("position") or "").upper()
        if pos in ("PI", "PICK") or ("Round Pick" in name) or _looks_like_pick(name):
            picks[_normalise_pick_label(name)] = float(val)
        elif sid:
            by_sleeper[str(sid)] = {
                "value": float(val),
                "overall_rank": row.get("overallRank"),
                "trend30": row.get("trend30Day"),
                "redraft": row.get("redraftValue"),
            }
    return by_sleeper, picks


def _looks_like_pick(name: str) -> bool:
    n = name.lower()
    return any(t in n for t in ("1st", "2nd", "3rd", "4th")) and any(c.isdigit() for c in n)


def _normalise_pick_label(name: str) -> str:
    n = name.lower().replace("round pick", "").strip()
    n = n.replace("early", "early").replace("mid", "mid").replace("late", "late")
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# Asset model
# ---------------------------------------------------------------------------

@dataclass
class Asset:
    key: str                          # sleeper player_id, or "PICK:2027:1:8"
    name: str
    pos: str
    kind: str = "player"              # player | pick
    team: str | None = None
    age: float | None = None
    exp: int | None = None
    injury: str | None = None
    depth: int | None = None
    depth_pos: str | None = None
    search_rank: int | None = None
    news_age_days: float | None = None

    ppg: float = 0.0                  # league-scored projected points per game
    ppg_lineup: float = 0.0           # ppg after availability haircut
    vorp: float = 0.0                 # ppg above positional replacement
    value: float = 0.0                # dynasty asset value, 0..VALUE_SCALE
    v_market: float = 0.0
    v_prod: float = 0.0
    sigma: float = SIGMA_BASE
    trend30: float | None = None
    owner_roster: int | None = None
    slot: str | None = None           # starter | bench | ir | taxi | fa

    def label(self) -> str:
        if self.kind == "pick":
            return self.name
        bits = f"{self.pos}-{self.team or 'FA'}"
        if self.age:
            bits += f", {self.age:.0f}y"
        if self.injury:
            bits += f", {self.injury}"
        return f"{self.name} ({bits})"

    def short(self) -> str:
        return self.name if self.kind == "pick" else f"{self.name} ({self.pos})"


def build_asset(pid: str, players: dict) -> Asset:
    p = players.get(str(pid)) or {}
    name = (p.get("full_name")
            or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            or str(pid))
    pos = p.get("position") or (p.get("fantasy_positions") or ["?"])[0]
    a = Asset(
        key=str(pid), name=name, pos=pos, team=p.get("team"),
        age=p.get("age"), exp=p.get("years_exp"),
        injury=p.get("injury_status"),
        depth=p.get("depth_chart_order"),
        depth_pos=p.get("depth_chart_position"),
        search_rank=(p.get("search_rank") if (p.get("search_rank") or 0) < 9_000_000 else None),
    )
    if a.age is None:
        a.age = DEFAULT_AGE.get(pos, 26)
    nu = p.get("news_updated")
    if nu and NEWS_REFERENCE_MS:
        a.news_age_days = max(0.0, (NEWS_REFERENCE_MS - nu) / 86_400_000)
    return a


def uncertainty(a: Asset, has_proj: bool) -> float:
    if a.kind == "pick":
        return SIGMA_PICK
    s = SIGMA_BASE
    if (a.exp or 0) <= 1:
        s += SIGMA_ROOKIE
    if (a.age or 30) <= 22:
        s += SIGMA_YOUNG
    if a.injury:
        s += SIGMA_INJURED
    if not has_proj:
        s += SIGMA_NO_PROJ
    if (a.depth or 1) >= 3:
        s += SIGMA_BURIED
    return min(s, SIGMA_CAP)


# ---------------------------------------------------------------------------
# Replacement level derived from YOUR starting lineup
# ---------------------------------------------------------------------------

def starter_demand(roster_positions: list[str], num_teams: int) -> dict[str, float]:
    """How many startable players the league consumes at each position.

    Dedicated slots are exact. FLEX demand is split across eligible positions
    in proportion to how many of each sit just past the dedicated cut - which
    in a 12-team RB/RB/WR/WR/TE/FLEX league lands mostly on RB and WR.
    """
    demand = {p: 0.0 for p in ALL_FANTASY}
    flex_slots = {k: 0 for k in FLEX_ELIGIBLE}
    for slot in roster_positions:
        if slot in demand:
            demand[slot] += num_teams
        elif slot in FLEX_ELIGIBLE:
            flex_slots[slot] += num_teams
    flex_split = {"FLEX": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
                  "WRRB_FLEX": {"RB": 0.5, "WR": 0.5},
                  "REC_FLEX": {"WR": 0.8, "TE": 0.2},
                  "SUPER_FLEX": {"QB": 0.85, "RB": 0.05, "WR": 0.08, "TE": 0.02}}
    for slot, n in flex_slots.items():
        for pos, share in flex_split.get(slot, {}).items():
            demand[pos] += n * share
    return demand


def replacement_levels(assets: list[Asset], demand: dict[str, float],
                       buffer_frac: float = 0.15) -> dict[str, float]:
    """Replacement = the ppg of the first guy past the last startable slot.

    The buffer accounts for the fact that a real waiver wire is never empty;
    the marginal add is a bit past the exact demand cut.
    """
    levels = {}
    for pos in ALL_FANTASY:
        pool = sorted((a.ppg for a in assets if a.pos == pos and a.ppg > 0), reverse=True)
        if not pool:
            levels[pos] = 0.0
            continue
        idx = int(round(demand.get(pos, 0) * (1 + buffer_frac)))
        idx = max(0, min(idx, len(pool) - 1))
        window = pool[max(0, idx - 2): idx + 3] or [pool[-1]]
        levels[pos] = statistics.fmean(window)
    return levels


# ---------------------------------------------------------------------------
# Lineup optimiser
# ---------------------------------------------------------------------------

def optimal_lineup(asset_keys, pool: dict[str, Asset], slots: list[str]):
    """Best legal starting lineup. Returns (total_ppg, {slot_index: key}).

    Dedicated slots are filled before flex slots, which is optimal here: the
    dedicated slots are position-exclusive, so taking the best available at
    each and giving the flex the best leftover cannot be beaten.
    """
    avail: dict[str, list[Asset]] = {}
    for k in asset_keys:
        a = pool.get(k)
        if a and a.kind == "player":
            avail.setdefault(a.pos, []).append(a)
    for v in avail.values():
        v.sort(key=lambda x: x.ppg_lineup, reverse=True)

    used, chosen, total = set(), {}, 0.0
    ordered = sorted(
        [(i, s) for i, s in enumerate(slots) if s in ALL_FANTASY or s in FLEX_ELIGIBLE],
        key=lambda t: SLOT_ORDER.index(t[1]) if t[1] in SLOT_ORDER else 99,
    )
    for i, slot in ordered:
        elig = {slot} if slot in ALL_FANTASY else FLEX_ELIGIBLE[slot]
        best, best_pts = None, -1e9
        for pos in elig:
            for a in avail.get(pos, []):
                if a.key in used:
                    continue
                if a.ppg_lineup > best_pts:
                    best, best_pts = a, a.ppg_lineup
                break                      # lists are sorted; first unused wins
        if best is not None:
            used.add(best.key)
            chosen[i] = best.key
            total += max(0.0, best.ppg_lineup)
    return total, chosen


# ---------------------------------------------------------------------------
# Team context
# ---------------------------------------------------------------------------

@dataclass
class Team:
    roster_id: int
    name: str
    manager: str
    player_keys: list[str] = field(default_factory=list)
    starters: list[str] = field(default_factory=list)
    ir: list[str] = field(default_factory=list)
    taxi: list[str] = field(default_factory=list)
    keepers: list[str] = field(default_factory=list)
    picks: list[str] = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    ties: int = 0
    pf: float = 0.0
    faab_used: int = 0
    waiver_pos: int | None = None

    lineup_ppg: float = 0.0
    asset_value: float = 0.0
    core_value: float = 0.0            # top 12 assets - what actually matters
    age_weighted: float = 0.0
    contend: float = 0.0               # -1 rebuild .. +1 contend
    need: dict[str, float] = field(default_factory=dict)
    surplus: dict[str, float] = field(default_factory=dict)

    def tradeable(self) -> list[str]:
        return [k for k in self.player_keys if k not in self.taxi] + self.picks


def compute_team_context(t: Team, pool: dict[str, Asset], slots: list[str],
                         demand: dict[str, float], repl: dict[str, float]):
    active = [k for k in t.player_keys if k not in t.ir]
    t.lineup_ppg, _ = optimal_lineup(active, pool, slots)

    owned = [pool[k] for k in t.player_keys + t.picks if k in pool]
    t.asset_value = sum(a.value for a in owned)
    top = sorted(owned, key=lambda a: a.value, reverse=True)[:12]
    t.core_value = sum(a.value for a in top)
    wsum = sum(a.value for a in top if a.kind == "player") or 1.0
    t.age_weighted = sum((a.age or 26) * a.value for a in top if a.kind == "player") / wsum

    # positional surplus: startable bodies above replacement, net of demand
    per_team_demand = {p: d / max(1, NUM_TEAMS) for p, d in demand.items()}
    for pos in ALL_FANTASY:
        startable = sum(1 for a in owned
                        if a.kind == "player" and a.pos == pos
                        and a.ppg >= repl.get(pos, 0) and a.key not in t.ir)
        t.surplus[pos] = startable - per_team_demand.get(pos, 0)
        # need > 1 means incoming players at this position are worth more here
        t.need[pos] = max(0.72, min(1.50, 1.0 - 0.14 * t.surplus[pos]))


def compute_contention(teams: list[Team], week: int):
    """Win-now vs rebuild, on [-1, +1].

    Early in the season record is noise, so lineup strength carries the weight;
    by midseason the record has earned its say.
    """
    rec_w = min(0.45, 0.05 * max(0, week - 1))
    str_w = 1.0 - rec_w

    def z(vals):
        m = statistics.fmean(vals)
        s = statistics.pstdev(vals) or 1.0
        return [(v - m) / s for v in vals]

    played = [t.wins + t.losses + t.ties for t in teams]
    winpct = [(t.wins + 0.5 * t.ties) / p if p else 0.5 for t, p in zip(teams, played)]
    zs_rec = z(winpct)
    zs_str = z([t.lineup_ppg for t in teams])
    zs_pf = z([t.pf for t in teams]) if any(t.pf for t in teams) else [0.0] * len(teams)
    for t, zr, zst, zp in zip(teams, zs_rec, zs_str, zs_pf):
        raw = rec_w * (0.75 * zr + 0.25 * zp) + str_w * zst
        t.contend = max(-1.0, min(1.0, raw / 1.6))


def timeline_mult(a: Asset, t: Team) -> float:
    """How a contender vs a rebuilder distorts an asset's value.

    Contenders pay up for proven production now and shade youth and picks.
    Rebuilders do the reverse. The magnitude is deliberately modest - this is
    a tilt, not a different universe.
    """
    if a.kind == "pick":
        youth = 1.0
    else:
        youth = max(0.0, min(1.0, (26 - (a.age or 26)) / 5.0))
        if (a.exp or 3) <= 1:
            youth = max(youth, 0.7)
    now = 1.0 - youth
    return 1.0 + t.contend * (0.20 * now - 0.22 * youth)


def subjective_value(a: Asset, t: Team) -> float:
    """What a given team thinks an asset is worth, before random perception."""
    v = a.value * timeline_mult(a, t)
    if a.kind == "player" and a.pos in ALL_FANTASY:
        v *= t.need.get(a.pos, 1.0)
    return v


# ---------------------------------------------------------------------------
# Draft picks as tradeable assets
# ---------------------------------------------------------------------------

# Rookie pick value as a fraction of a notional 1.01, by (round, tier).
# Shape follows dynasty market curves: 1sts are steep, everything after is
# cheap, and a late 1st is worth roughly a third of the 1.01.
PICK_TABLE = {
    (1, "early"): 0.84, (1, "mid"): 0.52, (1, "late"): 0.34,
    (2, "early"): 0.26, (2, "mid"): 0.20, (2, "late"): 0.155,
    (3, "early"): 0.115, (3, "mid"): 0.090, (3, "late"): 0.070,
    (4, "early"): 0.055, (4, "mid"): 0.045, (4, "late"): 0.035,
    (5, "early"): 0.030, (5, "mid"): 0.024, (5, "late"): 0.018,
}
PICK_SEASON_DISCOUNT = {0: 1.00, 1: 0.86, 2: 0.72, 3: 0.60}


def pick_assets(traded_picks, teams_by_id, season: int, rounds: int,
                market_picks: dict, rookie_anchor: float,
                years: list[int]) -> dict[str, Asset]:
    """Build every future rookie pick as a tradeable asset, routed to its owner.

    The pick's tier is estimated from the ORIGINAL team's contention score: a
    rebuilding team's 1st is an early 1st and worth materially more than a
    contender's. That is the piece most trade calculators skip, and in this
    league it matters - there is a real spread between the top and bottom.
    """
    owner_of: dict[tuple, int] = {}
    for tp in traded_picks or []:
        try:
            yr, rd = int(tp.get("season")), int(tp.get("round"))
            owner_of[(yr, rd, int(tp.get("roster_id")))] = int(tp.get("owner_id"))
        except (TypeError, ValueError):
            continue

    out: dict[str, Asset] = {}
    for yr in years:
        for rd in range(1, min(rounds, 4) + 1):
            for orig_rid, orig_team in teams_by_id.items():
                owner = owner_of.get((yr, rd, orig_rid), orig_rid)
                if owner not in teams_by_id:
                    owner = orig_rid
                # worse team -> earlier pick -> more valuable
                slot_frac = 0.5 - 0.42 * orig_team.contend          # 0.08 .. 0.92
                tier = "early" if slot_frac < 0.34 else ("mid" if slot_frac < 0.67 else "late")
                disc = PICK_SEASON_DISCOUNT.get(yr - season, 0.5)
                val = rookie_anchor * PICK_TABLE.get((rd, tier), 0.02) * disc

                mv = _match_market_pick(market_picks, yr, rd, tier, season)
                if mv:
                    val = W_MARKET * mv * disc_adjust(yr, season) + (1 - W_MARKET) * val

                nm = f"{yr} {_ord(rd)} ({orig_team.name})"
                a = Asset(key=f"PICK:{yr}:{rd}:{orig_rid}", name=f"{nm} [{tier}]",
                          pos="PICK", kind="pick", value=val,
                          sigma=SIGMA_PICK, owner_roster=owner)
                out[a.key] = a
                teams_by_id[owner].picks.append(a.key)
    return out


def disc_adjust(yr: int, season: int) -> float:
    return PICK_SEASON_DISCOUNT.get(yr - season, 0.6) / PICK_SEASON_DISCOUNT.get(1, 0.86)


def _ord(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}.get(n, f"{n}th")


def _match_market_pick(market_picks: dict, yr: int, rd: int, tier: str, season: int):
    if not market_picks:
        return None
    for label, val in market_picks.items():
        if str(yr) in label and _ord(rd) in label and tier in label:
            return val
    for label, val in market_picks.items():          # any year, right tier
        if _ord(rd) in label and tier in label:
            return val
    return None


# ---------------------------------------------------------------------------
# Valuation pipeline
# ---------------------------------------------------------------------------

def value_everything(pool: dict[str, Asset], scoring: dict, proj: dict,
                     demand: dict, market: dict) -> dict:
    """Populate ppg / vorp / v_market / v_prod / value on every player asset."""
    # 1. league-scored projected points
    for a in pool.values():
        if a.kind != "player":
            continue
        stats = proj.get(a.key)
        if stats:
            pts = score_stats(stats, scoring)
            games = stats.get("gp") or stats.get("gms_active") or 17
            a.ppg = pts / max(1.0, float(games)) if pts > 3 * games else pts / 17.0
            if a.ppg <= 0:
                a.ppg = pts / 17.0
        else:
            a.ppg = 0.0

    has_any_proj = sum(1 for a in pool.values() if a.ppg > 0) > 50
    if not has_any_proj:
        _ppg_from_search_rank(pool)

    # depth chart + injury haircuts
    for a in pool.values():
        if a.kind != "player":
            continue
        dmult = 1.0
        if a.depth and a.pos in DEPTH_MULT and not has_any_proj:
            dmult = DEPTH_MULT[a.pos].get(int(a.depth), DEPTH_DEFAULT)
        a.ppg *= dmult
        am, lm = INJURY_MULT.get(a.injury, (0.92, 0.5))
        a.ppg_lineup = a.ppg * lm
        a._asset_injury_mult = am                      # type: ignore[attr-defined]

    # 2. replacement level -> VORP
    players = [a for a in pool.values() if a.kind == "player"]
    repl = replacement_levels(players, demand)
    for a in players:
        a.vorp = max(0.0, a.ppg - repl.get(a.pos, 0.0))

    # 3. production-based dynasty value
    raw_prod = {}
    for a in players:
        rc = remaining_career_factor(a.pos, a.age or DEFAULT_AGE.get(a.pos, 26))
        base = softplus(a.ppg - repl.get(a.pos, 0.0))
        # slight convexity: an elite weekly edge is worth more than linear,
        # because you only get one lineup slot to spend on it
        raw = (base ** 1.25) * rc
        raw *= ASSET_VALUE_POS_MULT.get(a.pos, 1.0)
        raw *= getattr(a, "_asset_injury_mult", 1.0)
        raw_prod[a.key] = raw
    _rescale(raw_prod, VALUE_SCALE)
    for a in players:
        a.v_prod = raw_prod.get(a.key, 0.0)

    # 4. market anchor
    raw_mkt = {}
    for a in players:
        m = market.get(a.key)
        if m:
            raw_mkt[a.key] = m["value"] * ASSET_VALUE_POS_MULT.get(a.pos, 1.0)
            a.trend30 = m.get("trend30")
    if raw_mkt:
        _rescale(raw_mkt, VALUE_SCALE)
    for a in players:
        a.v_market = raw_mkt.get(a.key, 0.0)

    # 5. blend, renormalising when a market value is missing for that player
    for a in players:
        if a.v_market > 0:
            a.value = W_MARKET * a.v_market + W_PRODUCTION * a.v_prod
        else:
            a.value = a.v_prod
        a.sigma = uncertainty(a, has_proj=(a.key in proj))
    return repl


def _ppg_from_search_rank(pool: dict[str, Asset]):
    """Fallback when Sleeper's projection endpoint is down.

    Turns search_rank into a points-per-game curve per position. The shape is
    a - b*ln(rank), which fits real PPR positional scoring curves far better
    than an exponential: it keeps RB24 near 10 ppg instead of collapsing the
    replacement level to nothing.
    """
    CURVE = {  # (intercept, log-decay) fitted to 12-team full-PPR ppg by rank
        "QB": (24.0, 3.0), "RB": (21.0, 3.6), "WR": (20.5, 3.4),
        "TE": (16.5, 3.5), "K": (9.2, 1.2), "DEF": (9.0, 1.5),
    }
    by_pos: dict[str, list[Asset]] = {}
    for a in pool.values():
        if a.kind == "player" and a.pos in CURVE:
            by_pos.setdefault(a.pos, []).append(a)
    for pos, lst in by_pos.items():
        a0, b0 = CURVE[pos]
        lst.sort(key=lambda x: (x.search_rank if x.search_rank else 10**7, x.name))
        for i, a in enumerate(lst):
            if a.search_rank is None and i > 80:
                a.ppg = 0.0
                continue
            a.ppg = max(0.0, a0 - b0 * math.log(i + 1))


def softplus(x: float, beta: float = 0.45) -> float:
    """Smooth hinge at replacement level.

    A hard max(0, ppg - replacement) says every bench player, every free agent
    and every young stash is worth exactly nothing, which is wrong in dynasty
    and makes the waiver module useless. Softplus keeps the ordering below
    replacement while still collapsing it toward zero.
    """
    bx = beta * x
    if bx > 30:
        return x
    if bx < -30:
        return 0.0
    return math.log1p(math.exp(bx)) / beta


def _rescale(d: dict, target_max: float):
    mx = max(d.values(), default=0.0)
    if mx <= 0:
        return
    for k in d:
        d[k] = d[k] / mx * target_max


# ---------------------------------------------------------------------------
# Trade engine
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    partner: Team
    get_keys: tuple
    give_keys: tuple
    my_value_delta: float = 0.0        # consensus value, like a trade calculator
    my_adj_delta: float = 0.0          # after your needs + timeline
    my_lineup_delta: float = 0.0
    my_score: float = 0.0              # adj + lineup, the sort key
    their_value_delta: float = 0.0
    their_adj_delta: float = 0.0
    their_lineup_delta: float = 0.0
    their_score: float = 0.0
    my_gain_pct: float = 0.0
    their_gain_pct: float = 0.0
    p_accept: float = 0.0
    ev: float = 0.0
    notes: list[str] = field(default_factory=list)


def fills_lineup(team: Team, pool, slots, out_keys, in_keys) -> bool:
    """Can this roster still field a legal lineup after the trade?

    A dynasty rebuild can justify a lot, but it can't justify having nobody to
    put in the QB slot. Only dedicated slots are checked - flex positions are
    fungible by definition.
    """
    counts: dict[str, int] = {}
    for k in team.player_keys:
        if k in out_keys or k in team.ir:
            continue
        a = pool.get(k)
        if a and a.kind == "player":
            counts[a.pos] = counts.get(a.pos, 0) + 1
    for k in in_keys:
        a = pool.get(k)
        if a and a.kind == "player":
            counts[a.pos] = counts.get(a.pos, 0) + 1
    need: dict[str, int] = {}
    for s in slots:
        if s in ALL_FANTASY:
            need[s] = need.get(s, 0) + 1
    return all(counts.get(pos, 0) >= n for pos, n in need.items())


def _lineup_after(team: Team, pool, slots, out_keys, in_keys) -> float:
    keys = [k for k in team.player_keys if k not in out_keys and k not in team.ir]
    keys += [k for k in in_keys if pool.get(k) and pool[k].kind == "player"]
    total, _ = optimal_lineup(keys, pool, slots)
    return total


def evaluate_trade(me: Team, them: Team, get_keys, give_keys, pool, slots,
                   pts_to_value: float, win_now: float) -> Trade:
    t = Trade(partner=them, get_keys=tuple(get_keys), give_keys=tuple(give_keys))

    # consensus: what a public trade calculator would show
    raw_in = sum(pool[k].value for k in get_keys)
    raw_out = sum(pool[k].value for k in give_keys)
    t.my_value_delta = raw_in - raw_out
    t.my_gain_pct = (raw_in / raw_out - 1.0) if raw_out > 0 else 0.0

    # your book: consensus reweighted by your positional needs and timeline
    my_in = sum(subjective_value(pool[k], me) for k in get_keys)
    my_out = sum(subjective_value(pool[k], me) for k in give_keys)
    t.my_adj_delta = my_in - my_out

    their_in = sum(subjective_value(pool[k], them) for k in give_keys)
    their_out = sum(subjective_value(pool[k], them) for k in get_keys)
    t.their_adj_delta = their_in - their_out
    t.their_value_delta = raw_out - raw_in
    t.their_gain_pct = (their_in / their_out - 1.0) if their_out > 0 else 0.0

    base_me = me.lineup_ppg
    t.my_lineup_delta = _lineup_after(me, pool, slots, set(give_keys), get_keys) - base_me
    t.their_lineup_delta = _lineup_after(them, pool, slots, set(get_keys), give_keys) - them.lineup_ppg

    t.my_score = t.my_adj_delta + win_now * pts_to_value * t.my_lineup_delta
    their_win_now = WIN_NOW_FLOOR + (WIN_NOW_CEIL - WIN_NOW_FLOOR) * (them.contend + 1) / 2
    t.their_score = t.their_adj_delta + their_win_now * pts_to_value * t.their_lineup_delta
    return t


def acceptance_probability(t: Trade, pool, slots, pts_to_value: float,
                           rng: random.Random) -> float:
    """Monte Carlo over how the PARTNER might privately value the pieces.

    Each asset's perceived value is lognormal around consensus, with a sigma
    driven by age, experience, injury and data quality: a 21-year-old rookie RB
    is genuinely a coin flip between two managers, a 27-year-old WR1 is not.
    The partner also demands a surplus - people want to *win* the trade - and
    that threshold is itself random. Weekly lineup improvement is converted to
    value at the partner's own win-now weight and added to what they receive,
    so a contender will pay over consensus for a starter who plugs a hole.

    The acceptance rate across draws is the probability.
    """
    them = t.partner
    incoming = [pool[k] for k in t.give_keys]     # what THEY receive
    outgoing = [pool[k] for k in t.get_keys]      # what THEY send

    their_win_now = WIN_NOW_FLOOR + (WIN_NOW_CEIL - WIN_NOW_FLOOR) * (them.contend + 1) / 2
    lineup_utility = their_win_now * pts_to_value * t.their_lineup_delta

    # hard filter: a trade that guts their starting lineup is a non-starter
    if t.their_lineup_delta < -3.0 and them.contend > 0.1:
        return 0.02

    base_in = [(subjective_value(a, them), a.sigma) for a in incoming]
    base_out = [(subjective_value(a, them), a.sigma) for a in outgoing]
    if sum(v for v, _ in base_out) <= 0:
        return 0.0

    accepts = 0
    for _ in range(MC_DRAWS):
        vin = sum(v * math.exp(rng.gauss(0, s) - s * s / 2) for v, s in base_in)
        vout = sum(v * math.exp(rng.gauss(0, s) - s * s / 2) for v, s in base_out)
        if vout <= 0:
            continue
        required = rng.gauss(SURPLUS_MEAN, SURPLUS_SD)
        if ((vin + lineup_utility) / vout - 1.0) >= required:
            accepts += 1
    p = accepts / MC_DRAWS

    n_assets = len(t.get_keys) + len(t.give_keys)
    p *= PACKAGE_FRICTION ** max(0, n_assets - 2)

    best_asset = max(outgoing + incoming, key=lambda a: a.value)
    if best_asset.key in t.get_keys:              # they ship the best piece
        p *= (1 - BEST_PLAYER_PENALTY)
    return max(0.0, min(1.0, p))


def generate_trades(me: Team, teams: list[Team], pool, slots, pts_to_value,
                    win_now: float, max_pkg: int, top_n: int,
                    untouchable: set[str], min_package: float = 0.0,
                    seed: int = 7) -> list[Trade]:
    rng = random.Random(seed)
    mine = _tradeable_pool(me, pool, untouchable)
    candidates: list[Trade] = []

    for them in teams:
        if them.roster_id == me.roster_id:
            continue
        theirs = _tradeable_pool(them, pool, set())
        my_pkgs = _packages(mine, max_pkg)
        their_pkgs = _packages(theirs, max_pkg)
        for give in my_pkgs:
            gv = sum(pool[k].value for k in give)
            if gv <= 0:
                continue
            for get in their_pkgs:
                rv = sum(pool[k].value for k in get)
                if rv <= 0:
                    continue
                if min(rv, gv) < min_package:    # both sides must be substantive
                    continue
                if abs(rv - gv) / max(rv, gv) > VALUE_BAND:
                    continue
                if rv <= gv * 0.92:              # must plausibly help me
                    continue
                # pure pick-for-pick swaps are mostly model noise on estimated
                # draft slots; require at least one real player in the deal
                if all(pool[k].kind == "pick" for k in give + get):
                    continue
                # neither side may end up unable to submit a legal lineup
                if not fills_lineup(me, pool, slots, set(give), get):
                    continue
                if not fills_lineup(them, pool, slots, set(get), give):
                    continue
                t = evaluate_trade(me, them, get, give, pool, slots,
                                   pts_to_value, win_now)
                if t.my_score <= 0 or t.their_score <= 0:
                    continue
                candidates.append(t)

    candidates.sort(key=lambda x: x.my_score, reverse=True)
    candidates = candidates[:MAX_CANDIDATES_TO_SIMULATE]
    for t in candidates:
        t.p_accept = acceptance_probability(t, pool, slots, pts_to_value, rng)
        t.ev = t.my_score * t.p_accept
        _annotate(t, pool)

    live = [t for t in candidates if t.p_accept >= MIN_ACCEPT_TO_SHOW]
    return _dedupe(live, pool, top_n)


def _tradeable_pool(team: Team, pool, untouchable: set[str]) -> list[str]:
    keys = []
    for k in team.tradeable():
        a = pool.get(k)
        if not a or a.value <= 0:
            continue
        if a.name in untouchable or a.key in untouchable:
            continue
        if a.kind == "player" and a.pos in ("K", "DEF"):
            continue
        keys.append(k)
    keys.sort(key=lambda k: pool[k].value, reverse=True)
    return keys[:TOP_ASSETS_PER_SIDE]


def _packages(keys: list[str], max_pkg: int) -> list[tuple]:
    pkgs = [(k,) for k in keys]
    if max_pkg >= 2:
        pkgs += list(combinations(keys[:12], 2))
    if max_pkg >= 3:
        pkgs += list(combinations(keys[:8], 3))
    return pkgs


def _annotate(t: Trade, pool):
    them = t.partner
    for k in t.get_keys:
        a = pool[k]
        if a.kind == "player" and them.surplus.get(a.pos, 0) >= 1.5:
            t.notes.append(f"{a.name} is surplus at {a.pos} for them")
    for k in t.give_keys:
        a = pool[k]
        if a.kind == "player" and them.need.get(a.pos, 1) >= 1.2:
            t.notes.append(f"they need {a.pos}")
    if t.my_lineup_delta < -2.0:
        t.notes.append(f"costs you {abs(t.my_lineup_delta):.1f} ppg now")
    if them.contend > 0.35:
        t.notes.append("contender: wants win-now")
    elif them.contend < -0.35:
        t.notes.append("rebuilding: wants youth/picks")
    seen, out = set(), []
    for n in t.notes:
        if n not in seen:
            seen.add(n)
            out.append(n)
    t.notes = out[:3]


def _dedupe(trades: list[Trade], pool, top_n: int) -> list[Trade]:
    """Keep the best version of each trade *shape*.

    Without this the top 10 is ten near-identical variations on the same two
    players with a different third-rounder attached. Shape = partner + the set
    of players each way + how many picks each way, so a 2027 2nd and a 2028 2nd
    attached to the same player collapse into one entry. Each partner is capped
    at two ideas so one manager can't monopolise the list.
    """
    def shape(t: Trade):
        pl = lambda ks: frozenset(k for k in ks if pool[k].kind == "player")
        pk = lambda ks: sum(1 for k in ks if pool[k].kind == "pick")
        return (t.partner.roster_id, pl(t.get_keys), pl(t.give_keys),
                pk(t.get_keys), pk(t.give_keys))

    out, seen, per_partner, give_count = [], set(), {}, {}
    for t in sorted(trades, key=lambda x: x.my_score, reverse=True):
        sh = shape(t)
        core = (t.partner.roster_id, frozenset(t.get_keys))
        if sh in seen or core in seen:
            continue
        if per_partner.get(t.partner.roster_id, 0) >= 2:
            continue
        # don't fill the list with ten ways to sell the same two players
        givers = [k for k in t.give_keys if pool[k].kind == "player"]
        if givers and max(give_count.get(k, 0) for k in givers) >= 3:
            continue
        seen.add(sh)
        seen.add(core)
        per_partner[t.partner.roster_id] = per_partner.get(t.partner.roster_id, 0) + 1
        for k in givers:
            give_count[k] = give_count.get(k, 0) + 1
        out.append(t)
        if len(out) >= top_n:
            break
    return out


# ---------------------------------------------------------------------------
# Waiver wire
# ---------------------------------------------------------------------------

def waiver_targets(me: Team, pool, rostered: set[str], slots, repl,
                   trending: dict, faab_left: int, top_n: int = 12):
    """Rank free agents by what they actually add to THIS roster.

    Marginal value over the worst player you'd have to drop, plus any starting
    lineup improvement, plus a small nudge for league-wide add momentum. Bids
    are anchored to your own median starter's value, so a genuine starter
    costs real FAAB and a streamer costs a dollar.
    """
    # A Sleeper roster dump is full of players who haven't taken a snap in
    # years. news_updated is the cleanest available relevance signal: anyone
    # genuinely on a waiver wire has had news in the last few months.
    fas = [a for a in pool.values()
           if a.kind == "player" and a.key not in rostered
           and a.pos in ALL_FANTASY and a.team and a.value > 0
           and (a.news_age_days is None or a.news_age_days <= FA_NEWS_MAX_DAYS
                or trending.get(a.key, 0) > 0)]
    fas.sort(key=lambda a: a.value + 0.01 * trending.get(a.key, 0), reverse=True)
    fas = fas[:150]

    droppable = sorted(
        [pool[k] for k in me.player_keys
         if k in pool and pool[k].kind == "player"
         and k not in me.starters and k not in me.ir],
        key=lambda a: a.value)
    drop_floor = droppable[0] if droppable else None

    starter_vals = sorted(pool[k].value for k in me.starters
                          if k in pool and pool[k].pos in OFFENSE)
    anchor = statistics.median(starter_vals) if starter_vals else 1.0
    anchor = max(anchor, 1.0)

    rows, pos_seen = [], {"K": 0, "DEF": 0}
    base = me.lineup_ppg
    for a in fas:
        if a.pos in pos_seen:
            if pos_seen[a.pos] >= 1:        # one streamer suggestion is plenty
                continue
            pos_seen[a.pos] += 1
        after = _lineup_after(me, pool, slots,
                              {drop_floor.key} if drop_floor else set(), [a.key])
        lineup_delta = after - base
        v_delta = a.value - (drop_floor.value if drop_floor else 0)
        if v_delta <= 0 and lineup_delta <= 0.05:
            continue
        trend = trending.get(a.key, 0)
        rows.append({
            "asset": a, "value_delta": v_delta, "lineup_delta": lineup_delta,
            "trend_adds": trend, "drop": drop_floor,
            "score": max(0.0, v_delta) + 45 * max(0.0, lineup_delta) + 0.02 * trend,
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    rows = [r for r in rows if r["score"] >= 0.04 * anchor][:top_n]

    for r in rows:
        ratio = r["score"] / anchor                    # vs your median starter
        pct = 45.0 * min(1.0, ratio) ** 0.75
        if r["asset"].pos in ("K", "DEF"):
            pct *= 0.25        # streamers, not assets: never pay up for these
        r["bid_pct"] = round(max(0.5, pct), 1)
        r["bid_dollars"] = max(1, int(round(faab_left * r["bid_pct"] / 100)))
    return rows


def fetch_trending(kind="add", hours=48, limit=200) -> dict:
    raw = fetch_json(f"{BASE}/players/nfl/trending/{kind}?lookback_hours={hours}&limit={limit}",
                     f"trending_{kind}", 3, required=False) or []
    return {str(r.get("player_id")): r.get("count", 0) for r in raw}


# ---------------------------------------------------------------------------
# Reporting - one markdown file and one JSON file, everything in both
# ---------------------------------------------------------------------------

def md(s) -> str:
    """Team and player names occasionally contain a pipe; don't break tables."""
    return str(s).replace("|", "\\|").strip()


def fmt_pkg(keys, pool) -> str:
    return " + ".join(md(pool[k].short()) for k in keys)


def _hdr(snapshot, me, meta, L, teams) -> list[str]:
    st = L.get("settings") or {}
    bench = sum(1 for s in (L.get("roster_positions") or []) if s == "BN")
    start_slots = [s for s in (L.get("roster_positions") or []) if s != "BN"]
    waiver = {0: "rolling", 1: "reverse standings", 2: f"FAAB ${st.get('waiver_budget')}"}
    return [
        f"# {L.get('name')} — {L.get('season')} report",
        f"_Pulled {snapshot['pulled_at']} · week {meta['week']} · "
        f"for **{md(me.name)}** (roster {me.roster_id}, @{md(me.manager)})_",
        "",
        f"- **{L.get('total_rosters')} teams**, {L.get('status')}, "
        f"{'dynasty' if int(st.get('type', 0)) == 2 else 'keeper/redraft'}",
        f"- **Starting lineup:** {', '.join(start_slots)} (+{bench} bench, "
        f"{st.get('reserve_slots', 0)} IR)",
        f"- **Max keepers:** {st.get('max_keepers')} · **Trade deadline:** "
        f"week {st.get('trade_deadline')} · **Waivers:** "
        f"{waiver.get(st.get('waiver_type'), st.get('waiver_type'))} · "
        f"**Pick trading:** {'on' if st.get('pick_trading') else 'off'}",
        f"- **Values from:** {meta['value_source']}",
        f"- **Model:** {HORIZON_YEARS}y horizon, {ANNUAL_DISCOUNT} annual discount, "
        f"1 ppg ≈ {meta['pts_to_value']:.0f} value this season, "
        f"win-now weight {meta['win_now']:.2f}",
        "",
        "**Contents** — [Recommendations](#1--trade--waiver-recommendations) · "
        "[Rosters](#rosters) · [Traded picks](#traded-picks) · [Draft](#drafts)",
        "",
        "---",
        "",
    ]


def _sec_recommendations(me, teams, trades, waivers, pool, meta) -> list[str]:
    L, A = [], None
    A = L.append
    A("# 1 · Trade & waiver recommendations")
    A("")
    A(f"**Your posture:** {meta['posture']} (contention {me.contend:+.2f}) · "
      f"**Roster value:** {me.asset_value:,.0f} "
      f"(core-12 {me.core_value:,.0f}, rank {meta['value_rank']}/{len(teams)}) · "
      f"**Starting lineup:** {me.lineup_ppg:.1f} ppg "
      f"(rank {meta['lineup_rank']}/{len(teams)}) · "
      f"**Weighted age:** {me.age_weighted:.1f}")
    A("")

    A("## Your positional balance")
    A("")
    A("| Pos | Startable above replacement | League demand/team | Surplus | "
      "Need multiplier | Replacement ppg |")
    A("|---|---|---|---|---|---|")
    for pos in ALL_FANTASY:
        A(f"| {pos} | {me.surplus.get(pos, 0) + meta['demand_pt'].get(pos, 0):.0f} "
          f"| {meta['demand_pt'].get(pos, 0):.2f} | {me.surplus.get(pos, 0):+.2f} "
          f"| {me.need.get(pos, 1):.2f} | {meta['repl'].get(pos, 0):.1f} |")
    A("")
    A("_Need multiplier > 1.00 means a player at that position is worth more to "
      "you than his consensus value; < 1.00 means you're already deep there._")
    A("")

    A(f"## Top {len(trades)} trades, ranked by your adjusted value gain")
    A("")
    if not trades:
        A("_No trade cleared the filters. Loosen with `--max-package 3`, or "
          "check the data warnings below._")
    else:
        A("| # | Partner | You get | You give | Δ value | Gain % | Δ ppg | "
          "Score (your book) | Their Δ value | Their gain % | P(accept) | EV |")
        A("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            A(f"| {i} | {md(t.partner.name)} (@{md(t.partner.manager)}) "
              f"| {fmt_pkg(t.get_keys, pool)} | {fmt_pkg(t.give_keys, pool)} "
              f"| **{t.my_value_delta:+,.0f}** | {t.my_gain_pct*100:+.0f}% "
              f"| {t.my_lineup_delta:+.2f} | **{t.my_score:+,.0f}** "
              f"| {t.their_value_delta:+,.0f} | {t.their_gain_pct*100:+.0f}% "
              f"| **{t.p_accept*100:.0f}%** | {t.ev:,.0f} |")
        A("")
        A("_**Δ value** is consensus, the number a public trade calculator shows. "
          "**Score** is the same trade re-priced through your positional needs, "
          "your contention timeline and the lineup points it actually wins you — "
          "that's the sort key. **EV** = score × P(accept); sort by that if you'd "
          "rather send offers that land._")
        A("")
        A("### Trade detail")
        for i, t in enumerate(trades, 1):
            A("")
            A(f"**{i}. {md(t.partner.name)}** — P(accept) {t.p_accept*100:.0f}%, "
              f"contention {t.partner.contend:+.2f}, "
              f"weighted age {t.partner.age_weighted:.1f}")
            A("")
            A("- You get: " + ", ".join(md(pool[k].label()) for k in t.get_keys))
            A("- You give: " + ", ".join(md(pool[k].label()) for k in t.give_keys))
            A(f"- Your side: {t.my_value_delta:+,.0f} consensus, "
              f"{t.my_adj_delta:+,.0f} your book, {t.my_lineup_delta:+.2f} ppg "
              f"→ score {t.my_score:+,.0f}")
            A(f"- Their side: {t.their_value_delta:+,.0f} consensus, "
              f"{t.their_adj_delta:+,.0f} their book, "
              f"{t.their_lineup_delta:+.2f} ppg → score {t.their_score:+,.0f}")
            if t.notes:
                A(f"- Angle: {'; '.join(t.notes)}")
        A("")

    A("## Waiver / FAAB targets")
    A("")
    A(f"_FAAB remaining: ${meta['faab_left']}. Bids scale with marginal value "
      f"over the player you'd drop, not with hype._")
    A("")
    if not waivers:
        A("_Nothing on the wire beats your worst bench player._")
    else:
        A("| # | Player | Value | Δ value over your drop | Δ your ppg | "
          "Recent adds | Suggested bid | Drop |")
        A("|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(waivers, 1):
            a = r["asset"]
            A(f"| {i} | {md(a.label())} | {a.value:,.0f} | {r['value_delta']:+,.0f} "
              f"| {r['lineup_delta']:+.2f} | {r['trend_adds']:,} "
              f"| ${r['bid_dollars']} ({r['bid_pct']:.0f}%) "
              f"| {md(r['drop'].short()) if r['drop'] else '—'} |")
    A("")

    A("## League context")
    A("")
    A("| Roster | Manager | Record | Lineup ppg | Core-12 value | Wtd age | "
      "Contention | Thin at | FAAB left |")
    A("|---|---|---|---|---|---|---|---|---|")
    for t in sorted(teams, key=lambda x: x.core_value, reverse=True):
        thin = ", ".join(p for p in OFFENSE if t.surplus.get(p, 0) <= -0.6) or "—"
        mark = " ←" if t.roster_id == me.roster_id else ""
        A(f"| {md(t.name)}{mark} | @{md(t.manager)} | {t.wins}-{t.losses}-{t.ties} "
          f"| {t.lineup_ppg:.1f} | {t.core_value:,.0f} | {t.age_weighted:.1f} "
          f"| {t.contend:+.2f} | {thin} | ${meta['faab_total'] - t.faab_used} |")
    A("")
    A("---")
    A("")
    return L


def _sec_snapshot(snapshot, teams, pool, meta) -> list[str]:
    """Everything the original snapshot script produced, plus valuations."""
    L, A = [], None
    A = L.append
    league = snapshot["league"]
    names = {t.roster_id: t.name for t in teams}

    A("# 2 · League snapshot")
    A("")
    A("## Scoring (non-zero only)")
    A("")
    sc = league.get("scoring_settings") or {}
    A(", ".join(f"`{k}`={v}" for k, v in sorted(sc.items()) if v))
    A("")

    A("## Rosters")
    for t in sorted(teams, key=lambda x: x.roster_id):
        A("")
        A(f"### {md(t.name)} (roster {t.roster_id}, @{md(t.manager)})")
        A(f"_{t.wins}-{t.losses}-{t.ties} · lineup {t.lineup_ppg:.1f} ppg · "
          f"core-12 value {t.core_value:,.0f} · weighted age {t.age_weighted:.1f} "
          f"· contention {t.contend:+.2f}_")
        if t.keepers:
            A("- **Keepers:** " + ", ".join(
                md(pool[k].label()) for k in t.keepers if k in pool))
        bench = [k for k in t.player_keys if k not in t.starters and k not in t.ir]
        for label, keys in (("Starters", t.starters), ("Bench", bench),
                            ("IR", t.ir), ("Taxi", t.taxi)):
            if keys:
                A(f"- **{label}:** " + ", ".join(
                    f"{md(pool[k].label())} `{pool[k].value:,.0f}`"
                    for k in keys if k in pool))
        if t.picks:
            A("- **Picks:** " + ", ".join(
                f"{md(pool[k].name)} `{pool[k].value:,.0f}`"
                for k in sorted(t.picks, key=lambda k: -pool[k].value) if k in pool))
    A("")

    A("## Traded picks")
    A("")
    tp = snapshot.get("traded_picks") or []
    if not tp:
        A("_None._")
    else:
        A("| Season | Round | Originally | Now owned by |")
        A("|---|---|---|---|")
        for p in sorted(tp, key=lambda x: (str(x.get("season")), x.get("round") or 0)):
            orig = names.get(p.get("roster_id"), f"roster {p.get('roster_id')}")
            now = names.get(p.get("owner_id"), f"roster {p.get('owner_id')}")
            A(f"| {p.get('season')} | {p.get('round')} | {md(orig)} | {md(now)} |")
    A("")

    A("## Drafts")
    for d in snapshot.get("drafts") or []:
        A("")
        A(f"### {d.get('season')} draft ({d.get('type')}, {d.get('status')}) — "
          f"{len(d.get('picks') or [])} picks made")
        if not d.get("picks"):
            A("_No picks recorded._")
            continue
        for pk in d["picks"]:
            pl = pk.get("player") or {}
            keep = " **[KEEPER]**" if pk.get("is_keeper") else ""
            team = names.get(pk.get("roster_id"), f"roster {pk.get('roster_id')}")
            A(f"- `{pk.get('round')}.{pk.get('pick_no')}` {md(team)}: "
              f"{md(pl.get('name'))} ({pl.get('pos')}-{pl.get('team')}) "
              f"`{pl.get('value', 0):,}`{keep}")
    A("")

    if meta["warnings"]:
        A("## Data warnings")
        A("")
        for w in meta["warnings"]:
            A(f"- {w}")
        A("")

    A("---")
    A("")
    A("_Values are model output, not gospel. Acceptance probabilities assume your "
      "league-mates value players near consensus with the stated uncertainty; a "
      "manager who is a long way off consensus will behave differently._")
    return L


def write_report(snapshot, me, teams, trades, waivers, pool, meta,
                 path="league_report.md") -> str:
    """One markdown file: recommendations first, full snapshot underneath."""
    out = _hdr(snapshot, me, meta, snapshot["league"], teams)
    if not meta.get("snapshot_only"):
        out += _sec_recommendations(me, teams, trades, waivers, pool, meta)
    out += _sec_snapshot(snapshot, teams, pool, meta)
    with open(path, "w") as f:
        f.write("\n".join(out))
    return path



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

NUM_TEAMS = 12


def main():
    global OFFLINE, NUM_TEAMS

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default=LEAGUE_ID)
    ap.add_argument("--me", default=MY_USERNAME,
                    help="your Sleeper display_name, team name, or roster id")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-package", type=int, default=2, choices=[1, 2, 3])
    ap.add_argument("--win-now", type=float, default=None,
                    help="0=pure rebuild, 1=all-in. Default: derived from your roster")
    ap.add_argument("--untouchable", default="",
                    help="comma-separated player names never to trade away")
    ap.add_argument("--offline", action="store_true",
                    help="use cached API responses only")
    ap.add_argument("--no-market", action="store_true",
                    help="skip FantasyCalc, use the production model alone")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="just pull and dump the league, skip the trade engine "
                         "(the original sleeper_snapshot.py behaviour)")
    ap.add_argument("--md-out", default="league_report.md")
    ap.add_argument("--json-out", default="league_snapshot.json")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    OFFLINE = args.offline

    warnings: list[str] = []

    print(f"Pulling league {args.league}...")
    try:
        league = get(f"/league/{args.league}", "league", 1)
        users = get(f"/league/{args.league}/users", "users", 1)
        rosters = get(f"/league/{args.league}/rosters", "rosters", 0.25)
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"Sleeper returned HTTP {e.code} for league {args.league}.\n"
            f"  404 -> check the league id (it's in your Sleeper league URL)\n"
            f"  429 -> you're rate limited; wait a minute and retry\n"
            f"  5xx -> Sleeper is having a moment; retry, or use --offline "
            f"to re-run on the last cached pull.")
    except urllib.error.URLError as e:
        raise SystemExit(f"Couldn't reach Sleeper ({e.reason}). Check your "
                         f"connection, or run with --offline to use the last "
                         f"cached pull.")
    if not league or not rosters:
        raise SystemExit(f"League {args.league} returned no data.")
    traded_picks = get(f"/league/{args.league}/traded_picks", "traded_picks", 1) or []
    drafts = get(f"/league/{args.league}/drafts", "drafts", 24) or []
    state = get("/state/nfl", "state", 1, required=False) or {}
    players = load_players()

    # calibrate "now" from the dump itself rather than the clock, so a cached
    # or replayed player file still yields sane recency
    global NEWS_REFERENCE_MS
    NEWS_REFERENCE_MS = max((p.get("news_updated") or 0) for p in players.values())

    season = str(league.get("season") or state.get("season") or datetime.now().year)
    week = int(state.get("week") or league.get("settings", {}).get("leg") or 1)
    NUM_TEAMS = int(league.get("total_rosters") or 12)
    slots = league.get("roster_positions") or []
    scoring = league.get("scoring_settings") or {}
    settings = league.get("settings") or {}
    is_dynasty = int(settings.get("type", 0)) == 2

    ppr = float(scoring.get("rec", 0) or 0)
    num_qbs = sum(1 for s in slots if s == "QB") + sum(1 for s in slots if s == "SUPER_FLEX")

    print(f"  season {season}, week {week}, {NUM_TEAMS} teams, "
          f"{'dynasty' if is_dynasty else 'redraft/keeper'}, {ppr} PPR, {num_qbs}QB")

    # --- projections -------------------------------------------------------
    print("  fetching projections...")
    proj = fetch_projections(season)
    if not proj:
        proj = fetch_projections(season, week)
    if not proj:
        warnings.append("Sleeper projections unavailable — production model fell "
                        "back to the search_rank prior. Values are much coarser.")
        print("    !! projections unavailable, falling back to search_rank")
    else:
        print(f"    got {len(proj)} projection rows")

    # --- market ------------------------------------------------------------
    market, market_picks = ({}, {})
    if not args.no_market:
        print("  fetching market values (FantasyCalc dynasty)...")
        market, market_picks = fetch_market_values(NUM_TEAMS, num_qbs, ppr, is_dynasty)
        print(f"    got {len(market)} player values, {len(market_picks)} pick values")
    if not market:
        warnings.append("FantasyCalc market values unavailable — values are "
                        "100% bottom-up from your scoring settings.")
    value_source = ("FantasyCalc dynasty market "
                    f"({W_MARKET:.0%}) blended with league-scored production model "
                    f"({W_PRODUCTION:.0%})") if market else \
                   "league-scored production model only (no market anchor)"

    # --- build assets ------------------------------------------------------
    users_by_id = {u["user_id"]: u for u in users}

    def team_label(owner_id):
        u = users_by_id.get(owner_id) or {}
        meta = u.get("metadata") or {}
        return (meta.get("team_name") or u.get("display_name") or "(unclaimed)").strip()

    pool: dict[str, Asset] = {}
    teams: list[Team] = []
    rostered: set[str] = set()
    for r in sorted(rosters, key=lambda x: x["roster_id"]):
        s = r.get("settings") or {}
        owner = r.get("owner_id")
        t = Team(
            roster_id=r["roster_id"],
            name=team_label(owner),
            manager=(users_by_id.get(owner) or {}).get("display_name") or "?",
            player_keys=[str(p) for p in (r.get("players") or []) if p and p != "0"],
            starters=[str(p) for p in (r.get("starters") or []) if p and p != "0"],
            ir=[str(p) for p in (r.get("reserve") or [])],
            taxi=[str(p) for p in (r.get("taxi") or [])],
            keepers=[str(p) for p in (r.get("keepers") or []) if p and p != "0"],
            wins=s.get("wins") or 0, losses=s.get("losses") or 0, ties=s.get("ties") or 0,
            pf=(s.get("fpts") or 0) + (s.get("fpts_decimal") or 0) / 100,
            faab_used=s.get("waiver_budget_used") or 0,
            waiver_pos=s.get("waiver_position"),
        )
        teams.append(t)
        rostered.update(t.player_keys)

    # every player in the league universe gets an asset, not just rostered ones
    for pid, p in players.items():
        pos = p.get("position")
        if pos not in ALL_FANTASY:
            continue
        if pos != "DEF" and not p.get("active"):
            continue
        pool[str(pid)] = build_asset(pid, players)

    for t in teams:
        for k in t.player_keys:
            if k not in pool:
                pool[k] = build_asset(k, players)

    demand = starter_demand(slots, NUM_TEAMS)
    demand_pt = {k: v / NUM_TEAMS for k, v in demand.items()}
    repl = value_everything(pool, scoring, proj, demand, market)

    for t in teams:
        compute_team_context(t, pool, slots, demand, repl)
    compute_contention(teams, week)

    # --- picks (needs contention scores, so it comes after) ----------------
    if settings.get("pick_trading"):
        rookie_anchor = _rookie_anchor(pool)
        teams_by_id = {t.roster_id: t for t in teams}
        # this season's picks only count if its rookie draft hasn't happened yet
        pick_years = [int(season) + 1, int(season) + 2]
        if not any((d.get("status") or "").lower() == "complete"
                   and str(d.get("season")) == str(season) for d in drafts):
            pick_years.insert(0, int(season))
        print(f"  valuing rookie picks for {pick_years} "
              f"(1.01 anchor = {rookie_anchor:,.0f})")
        pool.update(pick_assets(traded_picks, teams_by_id, int(season),
                                int(settings.get("draft_rounds") or 3),
                                market_picks, rookie_anchor, pick_years))
        for t in teams:                       # recompute with picks included
            compute_team_context(t, pool, slots, demand, repl)

    me = _find_me(teams, args.me)
    print(f"  you are {me.name} (roster {me.roster_id})")

    # points-to-value conversion, calibrated on this league's own numbers
    pts_to_value = _calibrate_pts_to_value(pool, week)
    win_now = args.win_now if args.win_now is not None else \
        WIN_NOW_FLOOR + (WIN_NOW_CEIL - WIN_NOW_FLOOR) * (me.contend + 1) / 2
    posture = ("hard contend" if me.contend > 0.5 else
               "contend" if me.contend > 0.15 else
               "balanced" if me.contend > -0.15 else
               "retool" if me.contend > -0.5 else "rebuild")

    untouchable = {n.strip() for n in args.untouchable.split(",") if n.strip()}

    faab_left = max(0, int(settings.get("waiver_budget") or 0) - me.faab_used)
    trades, waivers = [], []
    if args.snapshot_only:
        print("  --snapshot-only: skipping trade and waiver analysis")
    else:
        print(f"  searching trades (max package {args.max_package})...")
        t0 = time.time()
        min_package = MIN_PACKAGE_FRAC * _rookie_anchor(pool)
        trades = generate_trades(me, teams, pool, slots, pts_to_value, win_now,
                                 args.max_package, args.top, untouchable,
                                 min_package, args.seed)
        print(f"    {len(trades)} recommendations in {time.time()-t0:.1f}s")

        print("  scoring waiver wire...")
        trending = fetch_trending("add")
        waivers = waiver_targets(me, pool, rostered, slots, repl,
                                 trending, faab_left)

    # --- snapshot ----------------------------------------------------------
    draft_blocks = []
    for d in drafts:
        did = d["draft_id"]
        block = {k: d.get(k) for k in
                 ("draft_id", "type", "status", "season", "settings", "slot_to_roster_id")}
        block["picks"] = []
        picks = get(f"/draft/{did}/picks", f"draft_{did}_picks", 24, required=False) or []
        for pk in picks:
            pid = str(pk.get("player_id"))
            a = pool.get(pid) or build_asset(pid, players)
            block["picks"].append({
                "round": pk.get("round"), "pick_no": pk.get("pick_no"),
                "roster_id": pk.get("roster_id"), "is_keeper": pk.get("is_keeper"),
                "player": {"id": pid, "name": a.name, "pos": a.pos,
                           "team": a.team, "value": round(a.value)},
            })
        draft_blocks.append(block)

    def dump_team(t: Team):
        return {
            "roster_id": t.roster_id, "team": t.name, "manager": t.manager,
            "record": {"wins": t.wins, "losses": t.losses, "ties": t.ties, "pf": t.pf,
                       "waiver_position": t.waiver_pos, "waiver_budget_used": t.faab_used},
            "lineup_ppg": round(t.lineup_ppg, 2),
            "asset_value": round(t.asset_value),
            "core_value": round(t.core_value),
            "weighted_age": round(t.age_weighted, 1),
            "contention": round(t.contend, 3),
            "surplus": {k: round(v, 2) for k, v in t.surplus.items()},
            "need": {k: round(v, 3) for k, v in t.need.items()},
            "starters": [_dump_asset(pool[k]) for k in t.starters if k in pool],
            "bench": [_dump_asset(pool[k]) for k in t.player_keys
                      if k not in t.starters and k not in t.ir and k in pool],
            "ir": [_dump_asset(pool[k]) for k in t.ir if k in pool],
            "taxi": [_dump_asset(pool[k]) for k in t.taxi if k in pool],
            "keepers": [_dump_asset(pool[k]) for k in t.keepers if k in pool],
            "picks": [_dump_asset(pool[k]) for k in t.picks if k in pool],
        }

    snapshot = {
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "league": {
            "name": league.get("name"), "league_id": league.get("league_id"),
            "previous_league_id": league.get("previous_league_id"),
            "season": season, "status": league.get("status"), "week": week,
            "total_rosters": NUM_TEAMS, "roster_positions": slots,
            "settings": settings, "scoring_settings": scoring,
        },
        "model": {
            "value_source": value_source,
            "replacement_ppg": {k: round(v, 2) for k, v in repl.items()},
            "starter_demand_per_team": {k: round(v, 2) for k, v in demand_pt.items()},
            "pts_to_value": round(pts_to_value, 1),
            "win_now_weight": round(win_now, 3),
            "horizon_years": HORIZON_YEARS, "annual_discount": ANNUAL_DISCOUNT,
            "warnings": warnings + _HTTP_FAILURES,
        },
        "teams": [dump_team(t) for t in teams],
        "traded_picks": traded_picks,
        "drafts": draft_blocks,
    }
    meta = {
        "league_name": league.get("name"), "week": week,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "value_source": value_source, "posture": posture, "win_now": win_now,
        "faab_left": faab_left, "faab_total": int(settings.get("waiver_budget") or 0),
        "demand_pt": demand_pt, "repl": repl,
        "pts_to_value": pts_to_value, "snapshot_only": args.snapshot_only,
        "warnings": warnings + _HTTP_FAILURES,
        "value_rank": 1 + sorted((t.core_value for t in teams),
                                 reverse=True).index(me.core_value),
        "lineup_rank": 1 + sorted((t.lineup_ppg for t in teams),
                                  reverse=True).index(me.lineup_ppg),
    }

    # recommendations live inside the one snapshot file, not a second JSON
    snapshot["recommendations"] = {
        "for_roster_id": me.roster_id,
        "trades": [{
            "partner_roster_id": t.partner.roster_id,
            "partner": t.partner.name, "manager": t.partner.manager,
            "you_get": [_dump_asset(pool[k]) for k in t.get_keys],
            "you_give": [_dump_asset(pool[k]) for k in t.give_keys],
            "my_value_delta": round(t.my_value_delta),
            "my_adj_delta": round(t.my_adj_delta),
            "my_lineup_delta": round(t.my_lineup_delta, 2),
            "my_score": round(t.my_score),
            "my_gain_pct": round(t.my_gain_pct, 4),
            "their_value_delta": round(t.their_value_delta),
            "their_adj_delta": round(t.their_adj_delta),
            "their_gain_pct": round(t.their_gain_pct, 4),
            "p_accept": round(t.p_accept, 4),
            "ev": round(t.ev), "notes": t.notes,
        } for t in trades],
        "waivers": [{
            "player": _dump_asset(r["asset"]),
            "value_delta": round(r["value_delta"]),
            "lineup_delta": round(r["lineup_delta"], 2),
            "trend_adds": r["trend_adds"],
            "bid_pct": r["bid_pct"], "bid_dollars": r["bid_dollars"],
            "drop": _dump_asset(r["drop"]) if r["drop"] else None,
        } for r in waivers],
    }
    snapshot["model"]["meta"] = {k: v for k, v in meta.items()
                                 if k not in ("repl", "demand_pt", "warnings")}

    with open(args.json_out, "w") as f:
        json.dump(snapshot, f, indent=1)
    write_report(snapshot, me, teams, trades, waivers, pool, meta, args.md_out)


    print(f"\nDone. {len(teams)} teams, {len(pool)} assets valued, "
          f"{len(trades)} trades, {len(waivers)} waiver targets.")
    print(f"Wrote {args.md_out} and {args.json_out}")
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print("  - " + w)


def _dump_asset(a: Asset) -> dict:
    d = {"id": a.key, "name": a.name, "pos": a.pos, "kind": a.kind,
         "value": round(a.value), "ppg": round(a.ppg, 2), "vorp": round(a.vorp, 2)}
    for k, v in (("team", a.team), ("age", a.age), ("exp", a.exp),
                 ("injury", a.injury), ("depth", a.depth),
                 ("sigma", round(a.sigma, 3)), ("trend30", a.trend30)):
        if v is not None:
            d[k] = v
    return d


def _rookie_anchor(pool) -> float:
    """Value of a notional 1.01 rookie pick.

    In a 12-team 1QB dynasty the 1.01 trades around the value of a top-10
    overall asset, so anchor it there rather than to whatever this year's
    rookie class happens to look like.
    """
    vals = sorted((a.value for a in pool.values() if a.kind == "player"), reverse=True)
    if len(vals) > 12:
        return statistics.fmean(vals[7:12])
    return (vals[0] if vals else 1000.0) * 0.6


def _calibrate_pts_to_value(pool, week: int) -> float:
    """Value of one point per game of starting-lineup improvement, THIS season.

    Two steps, because getting this wrong is what makes trade calculators
    recommend nonsense:

    1. Marginal slope. Fit dValue/dVORP over the *marginal starter* band
       (VORP 1-5) rather than the top of the curve. Value is convex in VORP,
       so a slope fitted on elite players would massively overprice a point of
       lineup upgrade at the bottom of your starting nine.
    2. Season share. Asset value spans the whole dynasty horizon; a lineup
       upgrade only banks the rest of THIS season. Scale by the fraction of
       the discounted horizon that the remaining weeks represent.

    The caller multiplies the result by the team's win-now weight, so a
    contender can push this back up and a rebuilder can push it to near zero.
    """
    band = [(a.vorp, a.value) for a in pool.values()
            if a.kind == "player" and 1.0 <= a.vorp <= 5.0 and a.value > 0]
    if len(band) < 15:
        band = [(a.vorp, a.value) for a in pool.values()
                if a.kind == "player" and a.vorp > 0.3 and a.value > 0]
    if not band:
        return 60.0
    num = sum(v * p for p, v in band)
    den = sum(p * p for p, v in band)
    slope = (num / den) if den else 150.0

    horizon_weight = sum(ANNUAL_DISCOUNT ** t for t in range(HORIZON_YEARS))
    season_share = (max(1, 18 - week) / 17.0) / horizon_weight
    return slope * season_share


def _find_me(teams, who: str) -> Team:
    w = str(who).strip().lower()
    for t in teams:
        if str(t.roster_id) == w:
            return t
    for t in teams:
        if (t.manager or "").lower() == w or (t.name or "").lower() == w:
            return t
    for t in teams:
        if w in (t.manager or "").lower() or w in (t.name or "").lower():
            return t
    raise SystemExit(f"Could not find a team matching '{who}'. "
                     f"Options: {[t.manager for t in teams]}")


if __name__ == "__main__":
    main()
