"""Shadow journal — graded 'would-have' outcomes for EVERY candidate analyzed (take AND skip).

The point: at ~1 live trade/day it takes months to learn whether the discretionary filtering has
edge. By logging every decision with a hypothetical bracket and grading what price actually did, we
build that evidence in weeks — and we measure the thing that matters: do the TAKE calls win more
than the SKIP calls would have? If takes ≈ skips, the filtering adds nothing and we learn it cheaply,
on paper, before it costs capital.

Stored in its own logs/shadows.json (append-mostly; kept out of state.json so it can't contend with
the trading lock). Grading is request-frugal: only OPEN shadows are looked up, and they auto-expire.

Simplification (v1): the candidate is treated as ENTERED at `entry` immediately (candidates are
flagged AT a level), and we grade stop-vs-target first-touch over subsequent H1 bars. A bar that
straddles both is scored a LOSS (pessimistic — never overstates edge).
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone, timedelta

from . import config, sheets

_FILE = config.LOG_DIR / "shadows.json"


def _load() -> list:
    if not _FILE.exists():
        return []
    try:
        return json.loads(_FILE.read_text())
    except Exception:
        return []


def _save(items: list) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.replace(_FILE)


def grade(side: str, entry: float, stop: float, target: float, bars: list) -> str:
    """Pure first-touch grade over chronological bars. Returns 'win' | 'loss' | 'open'.
    A bar that hits BOTH stop and target is scored 'loss' (conservative)."""
    side = side.lower()
    for b in bars:
        hi, lo = float(b["high"]), float(b["low"])
        if side == "buy":
            hit_stop, hit_tgt = lo <= stop, hi >= target
        else:
            hit_stop, hit_tgt = hi >= stop, lo <= target
        if hit_stop and hit_tgt:
            return "loss"
        if hit_stop:
            return "loss"
        if hit_tgt:
            return "win"
    return "open"


def log(entry: dict) -> dict:
    """Record one analyzed candidate. Required: symbol, side, entry, stop, target, verdict.
    Optional: setup_type, confidence, rationale."""
    items = _load()
    rec = {
        "id": f"{entry['symbol']}-{int(time.time())}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": entry["symbol"].upper(),
        "side": entry["side"].lower(),
        "entry": float(entry["entry"]),
        "stop": float(entry["stop"]),
        "target": float(entry["target"]),
        "setup_type": entry.get("setup_type", ""),
        "confidence": entry.get("confidence", ""),
        "verdict": entry.get("verdict", "skip").lower(),   # take | skip
        "rationale": entry.get("rationale", ""),
        "status": "open",
        "result": "",
        "result_ts": "",
    }
    items.append(rec)
    _save(items)
    sheets.append_shadow([rec["ts"][:16].replace("T", " "), rec["symbol"], rec["side"],
                          rec["verdict"], rec["entry"], rec["stop"], rec["target"],
                          rec["setup_type"], rec["confidence"], "open", ""])
    return rec


def open_shadows() -> list:
    return [s for s in _load() if s.get("status") == "open"]


def grade_open(client, now: datetime | None = None) -> list:
    """Grade every OPEN shadow against H1 bars since it was logged; expire stale ones.
    Returns the list of shadows that reached a terminal state this pass."""
    items = _load()
    now = now or datetime.now(timezone.utc)
    graded, dirty = [], False
    for s in items:
        if s.get("status") != "open":
            continue
        try:
            ts = datetime.fromisoformat(s["ts"])
        except Exception:
            continue
        try:
            bars = client.call("get_trendbars", {"symbolName": s["symbol"], "timeframe": "h1",
                               "from": ts.isoformat(), "to": now.isoformat()}).get("bars", [])
        except Exception:
            bars = []
        # Skip the first bar (the one that was current when the candidate was logged).
        # That bar's high/low touching the entry level doesn't confirm a fill — the candidate
        # was observed AT the level, not necessarily filled. Grading from bar[1] onward means
        # we require price to revisit the level on a subsequent bar, which is a cleaner fill
        # signal and prevents overstating the take win-rate.
        res = grade(s["side"], s["entry"], s["stop"], s["target"], bars[1:] if len(bars) > 1 else [])
        if res in ("win", "loss"):
            s["status"], s["result"], s["result_ts"] = res, res, now.isoformat()
            graded.append(s); dirty = True
        elif (now - ts) > timedelta(hours=config.SHADOW_EXPIRY_HOURS):
            s["status"], s["result"], s["result_ts"] = "expired", "expired", now.isoformat()
            graded.append(s); dirty = True
    if dirty:
        _save(items)
        for s in graded:
            sheets.append_shadow([s["result_ts"][:16].replace("T", " "), s["symbol"], s["side"],
                                  s["verdict"], s["entry"], s["stop"], s["target"],
                                  s["setup_type"], s["confidence"], s["status"], s["result"]])
    return graded


def _winrate(items: list) -> dict:
    decided = [s for s in items if s.get("result") in ("win", "loss")]
    wins = sum(1 for s in decided if s["result"] == "win")
    n = len(decided)
    return {"graded": n, "wins": wins, "losses": n - wins,
            "win_rate": (wins / n) if n else 0.0,
            "open": sum(1 for s in items if s.get("status") == "open"),
            "expired": sum(1 for s in items if s.get("status") == "expired")}


def summary() -> dict:
    """The headline: would-have win rate of TAKE calls vs SKIP calls. If take ≈ skip, the
    discretionary filtering isn't adding edge."""
    items = _load()
    takes = [s for s in items if s.get("verdict") == "take"]
    skips = [s for s in items if s.get("verdict") == "skip"]
    return {"all": _winrate(items), "take": _winrate(takes), "skip": _winrate(skips),
            "total": len(items)}


def format_summary(s: dict) -> str:
    a, t, k = s["all"], s["take"], s["skip"]
    edge = (t["win_rate"] - k["win_rate"]) * 100
    return (f"🧪 Shadow journal — {s['total']} candidates logged "
            f"({a['graded']} graded, {a['open']} open, {a['expired']} expired)\n"
            f"  TAKE: {t['graded']} graded · {t['win_rate']*100:.0f}% would-win\n"
            f"  SKIP: {k['graded']} graded · {k['win_rate']*100:.0f}% would-win\n"
            f"  Filtering edge (take − skip): {edge:+.0f} pts"
            + ("  ⚠ sample too small" if a["graded"] < 20 else ""))
