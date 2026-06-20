# FTMO Autonomous Operator — System Report
*Generated 2026-06-19 · `~/trading` · 118 tests passing · ~3,350 LOC*

## 1. What it is
A **fully autonomous, scheduled FTMO trading operator** running on this Mac. Claude acts as the
*analysis brain* (finds and proposes trades); a deterministic **Python engine** is the *only* thing that
can place, size, or manage orders — and it enforces every risk rule in code. The two are deliberately
separated so that no matter what the AI decides, it physically cannot breach the risk limits.

## 2. Core architecture & the safety principle
```
  Scanner (every 30 min) ──flags levels──▶ Candidates
                                              │
  Scheduled Claude runs ◀──read──────────────┘
  + you pasting signals          │
        │  analyze top-down       ▼
        └──propose JSON──▶  DETERMINISTIC ENGINE  ──▶ cTrader (local MCP bridge)
                            (20 rails, sizing,         127.0.0.1:9876
                             reconcile, manage)
```
- **Claude is permission-blocked** from all cTrader *write* tools. It can only read market data and hand
  a proposal to the engine.
- **The engine is the single source of truth for risk.** It refuses any proposal that violates a rail,
  regardless of how good the setup looks.
- Discretion can only ever *narrow* what executes — never bypass a rule.

## 3. Account & rules
| | |
|---|---|
| Account | Paid 2-Step Challenge, FTMO Standard, cTrader login 7568956 |
| Size / leverage | $10,000 · 1:100 |
| Profit target | +$1,000 (10%, Phase 1) |
| Self-imposed kill-switch | **−2%/day** (stricter than FTMO's 5%/$500) |
| Max loss floor | $9,000 (10%, static) |
| Standard rules | No weekend holds · restricted news (our ±15-min+ blackout is stricter) |
| Daily reset | 00:00 **Europe/Prague** (FTMO's boundary, not Dubai) |

## 4. The deterministic engine (the executor)
19 Python modules. Key ones:

| Module | Role |
|---|---|
| `rails.py` | **20 hard rails** (the proof that rules are code-enforced) |
| `risk.py` | Pure sizing — always from stop distance, snapped down, +slippage buffer |
| `execute.py` | The only path that places an order: reconcile → size → gate → place bracket |
| `reconcile.py` | Diffs state vs live cTrader (cTrader wins); counts fills; detects closures |
| `state.py` | Atomic state + cross-process file lock + Prague daily reset |
| `trade_manager.py` | Auto-management: breakeven, partial, trail, weekend-flat, news-flatten |
| `guards.py` | Phase-target freeze, unprotected-position close, trade-age force-exit |
| `mcp_client.py` | Sole cTrader client; session cleanup + retry (the 404 fix) |
| `news.py` | Tiered news-blackout evaluation |
| `scanner.py` | Level/regime scanner + candidate funnel |
| `shadow.py` | Edge-measurement journal (graded would-have outcomes) |
| `stats.py` / `sheets.py` / `telegram.py` | Analytics, Google Sheet, Telegram |

**The 20 rails** (each unit-tested, each refuses one violation): kill-switch −2% · per-trade risk
0.25–0.5% · stop mandatory/never-widened · R:R ≥1.5 · min broker size · max 5 fills/day · stop after
2 poor outcomes · reduce-to-0.25%-after-loss · aggregate risk ≤1% · no correlated-opposing · max 2
same-currency-direction · news blackout · no CB-decision entry · daily-buffer-after · overall-buffer-after
· FTMO hard floor ($9k/$500) · spread-spike · stale-quote · not-frozen · no-duplicate-order ·
target-reached.

## 5. The Claude brain (analysis)
**Scheduled runs** (Claude app, Dubai weekdays):

| Time | Run | Trades? |
|---|---|---|
| Mon 08:03 | Weekly plan | no |
| 09:32 | Morning brief (audit + publish news windows) | no |
| **11:00** | London | yes |
| **13:32** | Midday / London-afternoon | yes |
| **16:33** | NY overlap | yes |
| 20:06 | EOD review | no |

Plus **interactive**: you paste a signal or the bot pings me, and I vet it inline with the same
discipline. Governed by `CLAUDE.md` (the constitution) + `prompts/`. Core principle baked in: **quality
over frequency — skip is the default; the 5/day cap is a ceiling, not a target.**

## 6. The scanner (candidate funnel)
Runs every 30 min (launchd, no Claude cost). For each of 17 pairs it computes 20D levels, ATR, bias, and
a **trend regime** (slope-based). It pings only when price is **at a level, with a confirmed aligned
trend, and not within the 75-min de-dupe cooldown** — then hands the flagged pairs to the next Claude run.

## 7. Reporting & visibility
- **Telegram** — every run, trade, closure, freeze, kill-switch.
- **Live Google Sheet** — redesigned **KPI-card Dashboard** (equity, day P/L, progress/risk gauges,
  schedule+news, performance panel) + **Trades / Runs / Watchlist / Shadow** tabs.
- **journal.md** — append-only local log.

## 8. Safety & reliability
Kill-switch (equity-derived, plausibility-guarded) · fail-closed freeze on cTrader outage / unknown
position / degraded feed · weekend auto-flatten · news auto-flatten · pending-order expiry · trade-age
force-exit · atomic state writes + cross-process lock · session-limit/404 hardening · caffeinate (Mac
stays awake) · request cap 1,800/day.

## 9. Edge measurement (the honest core)
- Every trade tagged `setup_type / regime / confidence` → `ftmo stats` (win-rate, PF, expectancy).
- **Shadow journal** grades *every* candidate evaluated (take **and** skip) → `ftmo shadow-stats`
  measures whether TAKE calls beat SKIP calls = the filtering-edge metric, building a sample **on paper**
  in weeks.
- **Stance:** the edge is **unproven** (lifetime = 2 closed trades). Treated as capital preservation,
  not a money printer, until ~30–50 graded samples exist. No auto-adaptation on noise.

## 10. Current live status (2026-06-19)
| | |
|---|---|
| Equity | **$10,077.25** (overall **+$77.25**) |
| To target | $922.75 |
| Open / Pending | 0 / 0 |
| Today | 0/5 trades · 0/2 poor (fresh Prague day) |
| Trading days | 2 / 4 minimum |
| Record | 2 closed (XAUUSD +$133.57, GBPUSD −$56.32), PF 2.37 — *n too small to mean anything* |
| Mode | **ARMED — LIVE** |

## 11. Known limitations & deferred
- **Edge unproven** — the central open question; the shadow journal is accumulating the answer.
- **Pro latency** — setups between the ~5 daily runs are missed; true real-time needs Claude Max.
- **Local cTrader bridge** — can't move to cloud routines without changing execution venue.
- **Fast round-trip blind spot** — a fill+close between two reconciles evades the *governors* (not the
  capital kill-switch). Documented, deferred.
- **Auto-feedback loop** (disable losing setups, confidence-weighted sizing) — deferred until ~30–50
  samples, on purpose.

## 12. Command reference
```
ftmo audit · execute · manage · watchdog · set-news · bars · quote · scan ·
candidates · subscribe · backtest · stats · eod · shadow · shadow-grade · shadow-stats
```
