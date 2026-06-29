"""Shadow journal — graded 'would-have' outcomes for EVERY candidate analyzed (take AND skip).

The point: at ~1 live trade/day it takes months to learn whether the discretionary filtering has
edge. By logging every decision with a hypothetical bracket and grading what price actually did, we
build that evidence in weeks — and we measure the thing that matters: do the TAKE calls win more
than the SKIP calls would have? If takes ≈ skips, the filtering adds nothing and we learn it cheaply,
on paper, before it costs capital.

Stored in its own logs/shadows.json (append-mostly; kept out of state.json so it can't contend with
the trading lock). Grading is request-frugal: only OPEN shadows are looked up, and they auto-expire.

B1: fcntl.flock() around every read-modify-write cycle prevents concurrent grading losses (scanner
and watchdog both calling grade_open() simultaneously). The lock is held only for the brief
load→mutate→save section; slow MCP bar-fetches run outside it.

B2: Fill validation — before grading, verify the entry price was actually touched on a bar AFTER
bar[0]. bar[0] is skipped because price was AT the level when logged (not yet filled). Shadows
where the level was never revisited are marked no_fill and excluded from win-rate stats.
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta

from . import config, sheets

_FILE = config.LOG_DIR / "shadows.json"
_LOCK_FILE = config.LOG_DIR / ".shadows.lock"


@contextlib.contextmanager
def _shadow_lock(timeout: float = 30.0):
    """Exclusive advisory lock around the read-modify-write cycle on shadows.json."""
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(_LOCK_FILE, "w")
    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


def _load() -> list:
    if not _FILE.exists():
        return []
    try:
        return json.loads(_FILE.read_text())
    except Exception:
        return []


def _save(items: list) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(config.LOG_DIR), suffix=".shadows.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(items, indent=2))
        os.replace(tmp, str(_FILE))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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


def _find_fill_bar(side: str, entry: float, bars: list) -> int | None:
    """Return index of first bar in `bars` that touched the entry price (fill signal). None if no touch.

    For a BUY limit at `entry`: fills when a bar's low reaches or goes below entry.
    For a SELL limit at `entry`: fills when a bar's high reaches or goes above entry.
    """
    side = side.lower()
    for i, b in enumerate(bars):
        hi, lo = float(b["high"]), float(b["low"])
        if side == "buy" and lo <= entry:
            return i
        if side == "sell" and hi >= entry:
            return i
    return None


def log(entry: dict) -> dict:
    """Record one analyzed candidate. Required: symbol, side, entry, stop, target, verdict.
    Optional: setup_type, confidence, rationale."""
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
    with _shadow_lock():
        items = _load()
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
    Returns the list of shadows that reached a terminal state this pass.

    MCP bar-fetches happen outside the lock; only the brief load→mutate→save holds it.
    """
    now = now or datetime.now(timezone.utc)

    # Step 1: collect open shadows and fetch bars (outside lock — slow MCP calls)
    items_snapshot = _load()
    bar_cache: dict[str, list] = {}
    for s in items_snapshot:
        if s.get("status") != "open":
            continue
        try:
            ts = datetime.fromisoformat(s["ts"])
        except Exception:
            continue
        try:
            bars = client.call("get_trendbars", {
                "symbolName": s["symbol"], "timeframe": "h1",
                "from": ts.isoformat(), "to": now.isoformat(),
            }).get("bars", [])
        except Exception:
            bars = []
        bar_cache[s["id"]] = bars

    # Step 2: compute terminal states for each shadow (pure computation, outside lock)
    updates: dict[str, tuple[str, str, str]] = {}   # id → (status, result, result_ts)
    for s in items_snapshot:
        sid = s.get("id", "")
        if s.get("status") != "open" or sid not in bar_cache:
            continue
        try:
            ts = datetime.fromisoformat(s["ts"])
        except Exception:
            continue
        bars = bar_cache[sid]
        # Skip bar[0]: price was AT the level when logged — not a fill, just proximity.
        # Require entry level to be revisited on a SUBSEQUENT bar before grading.
        remaining = bars[1:] if len(bars) > 1 else []
        fill_idx = _find_fill_bar(s["side"], s["entry"], remaining)
        if fill_idx is None:
            if (now - ts) > timedelta(hours=config.SHADOW_EXPIRY_HOURS):
                updates[sid] = ("expired", "no_fill", now.isoformat())
            continue
        res = grade(s["side"], s["entry"], s["stop"], s["target"], remaining[fill_idx:])
        if res in ("win", "loss"):
            updates[sid] = (res, res, now.isoformat())
        elif (now - ts) > timedelta(hours=config.SHADOW_EXPIRY_HOURS):
            updates[sid] = ("expired", "expired", now.isoformat())

    if not updates:
        return []

    # Step 3: apply updates under lock (brief critical section — just load/mutate/save)
    graded = []
    with _shadow_lock():
        items = _load()
        for s in items:
            uid = s.get("id", "")
            if uid in updates and s.get("status") == "open":
                status, result, result_ts = updates[uid]
                s["status"], s["result"], s["result_ts"] = status, result, result_ts
                graded.append(dict(s))
        if graded:
            _save(items)

    # Step 4: Sheets logging (outside lock)
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
            "expired": sum(1 for s in items if s.get("status") == "expired"),
            "no_fill": sum(1 for s in items if s.get("result") == "no_fill")}


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
    nf = a.get("no_fill", 0)
    nf_s = f", {nf} no-fill" if nf else ""
    return (f"🧪 Shadow journal — {s['total']} candidates logged "
            f"({a['graded']} graded, {a['open']} open, {a['expired']} expired{nf_s})\n"
            f"  TAKE: {t['graded']} graded · {t['win_rate']*100:.0f}% would-win\n"
            f"  SKIP: {k['graded']} graded · {k['win_rate']*100:.0f}% would-win\n"
            f"  Filtering edge (take − skip): {edge:+.0f} pts"
            + ("  ⚠ sample too small" if a["graded"] < 20 else ""))
