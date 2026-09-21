# Fantasy Dynasty Engine — project guide

A Python script that pulls my Sleeper dynasty league and produces quantified
trade and waiver recommendations. This file explains the league, how to run the
script, how to read what it spits out, and where the model can be argued with.

**This doc is project knowledge for both me and Claude.** If you're Claude
reading this at the start of a conversation, skip to
[Working with Claude](#working-with-claude) — it says what I usually need.

---

## The league

| | |
|---|---|
| Name | Fantastic Fantasies |
| League ID | `1316902230451818496` |
| My team | **ChuckFarbs**, roster 11 |
| Format | Dynasty, 12 teams, 1QB, full PPR |
| Starting lineup | QB, RB, RB, WR, WR, TE, FLEX, K, DEF |
| Bench / IR / taxi | 17 / 2 / 0 |
| Rookie draft | 5 rounds, linear |
| Pick trading | On (2027 and 2028 picks are live assets) |
| Waivers | FAAB, $100 budget |
| Trade deadline | Week 10 · 2-day review · 6 votes to veto |
| Playoffs | 7 teams, starts week 15, 3 divisions |

**Scoring quirks that actually move players:**

- Passing yards at **0.04** (25 yards per point) and passing TDs at **4**, not 6.
  QBs are suppressed relative to standard. In a 1QB league they're already
  replaceable; this pushes them further down.
- **Full PPR** (`rec = 1.0`) with **no TE premium**. Volume receivers and
  pass-catching backs gain; a TE1 is not artificially propped up.
- Rush/rec yards at 0.1, all non-passing TDs at 6.
- `-2` per INT and fumble lost, `-1` per missed FG and missed XP.

The script reads all of this straight from the league's `scoring_settings` and
scores every projection through it, so none of the above is hardcoded — if the
commissioner changes scoring, valuations follow automatically.

---

## Quick start

```bash
# first run — pulls everything, ~15MB player dump, takes a minute
python3 sleeper_dynasty_engine.py

# subsequent runs are fast; the player dump is cached for 24h
python3 sleeper_dynasty_engine.py
```

Stdlib only. No API keys, no pip install, no virtualenv. Python 3.10+.

It writes two files into the working directory:

| File | What's in it |
|---|---|
| `league_report.md` | Everything readable, in one document. Recommendations first, full league snapshot underneath. **This is the file to upload to a chat.** |
| `league_snapshot.json` | Everything structured, including the recommendations under a `recommendations` key. For scripting against. |

It also creates a `.sleeper_cache/` directory and a `players_nfl.json` file.
Both are caches — safe to delete, they just get re-downloaded.

---

## Reading the report

### Header block

States the league shape, then two model lines worth checking every run:

- **Values from** — tells you whether the FantasyCalc market anchor loaded. If
  it says "league-scored production model only", the market call failed and the
  numbers are much coarser.
- **Model** — the horizon, discount, and the current `1 ppg ≈ N value`
  conversion. That conversion is recalibrated every run from the league's own
  value distribution, so it drifts as the season progresses.

### Positional balance

| Column | Meaning |
|---|---|
| Startable above replacement | How many of my players at that position clear replacement level |
| League demand/team | How many that position the league consumes per team, including the FLEX share (RB/WR ≈ 2.45, TE ≈ 1.10) |
| Surplus | The difference. Negative = a hole |
| Need multiplier | What an incoming player at that position is worth **to me** vs consensus. 1.15 means I'd pay a 15% premium |
| Replacement ppg | The bar a starter has to clear |

The need multiplier is what makes the engine's recommendations differ from a
generic trade calculator — it re-prices every deal through my actual roster
holes.

### The trade table

| Column | Meaning |
|---|---|
| Δ value | **Consensus.** What FantasyPros or KTC would show you |
| Gain % | Consensus value in ÷ consensus value out |
| Δ ppg | Change to my optimal starting lineup, this week |
| **Score (your book)** | The sort key. Consensus, re-priced through my positional needs and contention timeline, plus the lineup points converted to value |
| Their Δ value | Consensus, from their side (mirror of mine) |
| Their gain % | Their side re-priced through **their** needs and timeline |
| P(accept) | Modelled probability they say yes — see below |
| EV | Score × P(accept) |

**Δ value and Score diverging is normal and is the point.** A deal can be
consensus-negative for me and still score well if it fills a hole or fits my
timeline. Conversely a consensus win that leaves me worse at a position I'm
already thin at will score poorly.

Sort by **Score** to find the best trades. Sort by **EV** to find the ones
worth actually sending.

### Acceptance probability

Not a fudge factor. It runs 1,500 Monte Carlo draws of how the partner might
*privately* value each piece — lognormal around consensus, with a per-player σ
driven by age, experience, injury status and data quality. A 21-year-old rookie
RB has σ near the 0.55 cap (two managers genuinely disagree); a 27-year-old WR1
sits near 0.17. It then adds the partner's weekly lineup gain, converted at
*their* win-now weight, and checks the total against a randomly drawn surplus
threshold — because people need to feel they won the trade.

Rough calibration: **>40%** is worth sending cold. **25–40%** needs a pitch.
**<15%** means the model thinks they hang up.

Things that are hard filters, not probability adjustments:

- Either side left unable to field a legal lineup → rejected outright
- Partner losing >3 ppg while contending → 2%

### Waiver table

Ranked by marginal value over the worst player I'd have to drop, plus any
lineup improvement, plus a nudge for league-wide add momentum.

Bids are anchored to my own median starter's value, not to hype. A genuine
starter costs real FAAB; K and DEF bids are damped 75% because streaming them
is a weekly decision, not an asset acquisition. Free agents whose last news
item is more than 120 days old are filtered out — a Sleeper player dump is full
of guys who haven't taken a snap in three years.

### League context

Every team's lineup strength, core-12 value, weighted age, contention score and
thin positions. This is the table to look at before deciding *who* to approach.
Contention runs -1 (full rebuild) to +1 (all-in), weighted toward lineup
strength early in the season and toward record as the sample grows.

---

## How the numbers are built

### Asset value

Every player and every future rookie pick lands on one 0–10,000 scale. Two
independent estimates, blended **65% market / 35% production**:

1. **Market anchor** — FantasyCalc's public dynasty API, queried shaped to this
   league (`isDynasty=true&numTeams=12&numQbs=1&ppr=1`). Joins on `sleeperId`,
   so no fuzzy name matching. Also supplies rookie pick values.
2. **Production model** — Sleeper projections scored through this league's
   exact scoring settings, converted to VORP against a replacement level
   derived from the real lineup demand, then extended across a 6-year horizon
   through positional aging curves and discounted 18%/year.

Where a player has no market value, the production model carries him alone.

### Aging curves

Fraction of peak production retained by age, per position, piecewise linear:

- **RB** peaks 23–24, falls off a cliff after 27 (0.85 at 27, 0.48 at 30)
- **WR** peaks 25–27, gentle decline (0.91 at 29, 0.62 at 32)
- **TE** peaks 27–28, late bloomers (0.70 at 23, 0.92 at 30)
- **QB** plateaus 27–32, slow decline after 35

This is why the model reads a 23-year-old and a 30-year-old with identical
current production very differently.

### Replacement level

Derived from the league's actual `roster_positions`, not a rule of thumb. A
12-team league with two RB slots and a FLEX consumes roughly 2.45 startable RBs
per team, so replacement sits around RB34 with a 15% waiver buffer. Value below
replacement uses a **softplus** hinge rather than a hard zero — a hard cutoff
says every bench player and free agent is worth exactly nothing, which is wrong
in dynasty and makes the waiver module useless.

### Rookie pick values

Priced off a notional 1.01 anchor (the mean of assets ranked 8–12 overall),
times a round/tier table, times a year discount (0.86 for next year, 0.72 for
the year after).

The tier is estimated from the **original** team's contention score — a
rebuilding team's 1st is an early 1st and worth substantially more than a
contender's. Most trade calculators treat all 1sts the same; in this league the
spread between the top and bottom rosters is wide enough that it matters. The
traded-pick ledger is followed, so a pick shows up as an asset of whoever
currently owns it, labelled with where it came from.

---

## Command reference

| Flag | Default | What it does |
|---|---|---|
| `--me` | `ChuckFarbs` | Display name, team name, or roster id |
| `--league` | this league | Another league id |
| `--top` | `10` | How many trades to output |
| `--max-package` | `2` | Assets per side. `3` is ~7s vs ~3s |
| `--win-now` | auto | `0` = pure rebuild, `1`+ = all-in. Overrides the derived contention score |
| `--untouchable` | — | Comma-separated names never to trade away |
| `--snapshot-only` | off | Pull and dump only, skip the trade engine |
| `--no-market` | off | Skip FantasyCalc, use the production model alone |
| `--offline` | off | Re-run the analysis on cached data, no network |
| `--md-out` | `league_report.md` | Output filename |
| `--json-out` | `league_snapshot.json` | Output filename |
| `--seed` | `7` | Monte Carlo seed. Change it to check result stability |

Common combinations:

```bash
# I think the model is wrong about my timeline
python3 sleeper_dynasty_engine.py --win-now 0.8

# Don't show me anything that involves selling my rookie RB
python3 sleeper_dynasty_engine.py --untouchable "Jeremiyah Love"

# Bigger packages, more options
python3 sleeper_dynasty_engine.py --max-package 3 --top 15

# Just refresh the league dump, no analysis
python3 sleeper_dynasty_engine.py --snapshot-only

# Re-run with different settings without hammering the API
python3 sleeper_dynasty_engine.py --offline --win-now 0.5
```

---

## Tuning

All constants live in the `CONFIG` block at the top of the script. The ones
worth touching, in rough order of how likely they are to need it:

| Constant | Default | Effect |
|---|---|---|
| `SURPLUS_MEAN` | `0.07` | How much my league-mates need to feel they won. **Tune this first** — I know these managers, the model doesn't |
| `W_MARKET` / `W_PRODUCTION` | `0.65` / `0.35` | Market anchor vs bottom-up model. Push toward production if I trust my scoring-specific read more than consensus |
| `HORIZON_YEARS` | `6` | How far dynasty value looks ahead |
| `ANNUAL_DISCOUNT` | `0.82` | Lower = more win-now |
| `WIN_NOW_FLOOR` / `CEIL` | `0.30` / `3.50` | Range the contention score maps into |
| `SIGMA_*` | various | Per-player valuation uncertainty. Raising these raises acceptance probabilities across the board |
| `VALUE_BAND` | `0.45` | How lopsided a package can be before it's pruned |
| `MIN_PACKAGE_FRAC` | `0.12` | Floor on deal size, as a fraction of a 1.01 pick |
| `TOP_ASSETS_PER_SIDE` | `20` | Search breadth. Raising it costs time quadratically |
| `AGE_CURVES` | — | The aging curves themselves, as anchor points |

---

## When the output looks wrong

**Check the console first.** It prints how many projection rows and market
values loaded. If either is zero, the report will also carry a **Data warnings**
section at the bottom.

| Symptom | Likely cause |
|---|---|
| "Sleeper projections unavailable" | The projections endpoint is undocumented and can change without notice. The model falls back to a `search_rank`-derived curve, which is much coarser. Values are still directionally useful; treat exact numbers with suspicion |
| "FantasyCalc market values unavailable" | Their API was unreachable. Values become 100% bottom-up |
| All trades are "sell my veterans for picks" | The model has read me as a rebuild. Check the contention score in the header; override with `--win-now` if I disagree |
| Acceptance probabilities all look high | `SIGMA_*` values may be too generous, or `SURPLUS_MEAN` too low for this league |
| Waiver list full of names I don't recognise | Projections probably failed. The fallback ranks by `search_rank`, which is stale for fringe players |
| HTTP 429 | Rate limited. Wait a minute, or use `--offline` |
| Same trade suggested every week | Expected — the model is consistent. Check whether the partner has actually declined it |

---

## Working with Claude

**What I usually want help with, roughly in order:**

1. **Sanity-checking the output.** I'll paste `league_report.md`. Tell me
   whether the top trades actually make sense as football decisions, not just
   as arithmetic. The model doesn't know that a coach just named someone the
   starter, or that a manager hates a player.
2. **Writing the trade pitch.** Given a row from the table, draft the Sleeper
   message. Short, specific, framed around what *they* get.
3. **Arguing with the model.** If a valuation looks wrong, help me work out
   whether it's the market anchor, the aging curve, the replacement level or
   the need multiplier — then which constant to change.
4. **Code changes.** Adding features, fixing the fallback path, adjusting the
   search.

**Things worth knowing before answering:**

- The script's console output and the **Data warnings** section tell you
  whether the numbers are trustworthy. Check them before reasoning about
  specific values.
- The trade engine has never been validated against live API responses — both
  upstream endpoints were unreachable from the sandbox where it was built. The
  parsers were tested against correctly-shaped synthetic data. Treat a first
  live run as the real test.
- Sleeper's projections endpoint is undocumented. It is the most likely thing
  to break.
- I'm technical (data engineering day job) — skip the hand-holding on the
  Python, go straight at the model.
- Don't assume the recommendations are right just because they're quantified.
  The acceptance model in particular encodes assumptions about managers I know
  and it doesn't.

---

## Data sources

| Source | Endpoint | Documented? |
|---|---|---|
| League, users, rosters, traded picks, drafts | `api.sleeper.app/v1/league/...` | Yes |
| Player dump | `api.sleeper.app/v1/players/nfl` | Yes — pull once per day max |
| Trending adds | `api.sleeper.app/v1/players/nfl/trending/add` | Yes |
| NFL state (current week) | `api.sleeper.app/v1/state/nfl` | Yes |
| Projections | `api.sleeper.app/projections/nfl/<season>` | **No** — undocumented, may break |
| Dynasty market values | `api.fantasycalc.com/values/current` | Public, no key |

Depth chart position, injury status, age, experience and news recency all come
from the player dump.
