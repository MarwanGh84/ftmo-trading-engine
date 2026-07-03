"""Auto morning brief — no Claude API needed, purely deterministic.

Fetches the ForexFactory weekly calendar JSON, applies tiered padding rules from
morning_brief.md, writes news_windows to state, and sends a Telegram summary.

Tier 1 (FOMC/NFP/CPI/rate decisions): −60 min to +45 min
Tier 2 (GDP/PCE/PMI/unemployment/retail sales): −30 min to +30 min
Tier 3 (other HIGH impact): −15 min to +15 min
Rate decisions also get kind="cb" (engine holds any open CB-pair positions).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import urlopen

from . import config, telegram
from . import state as state_mod

_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_TRACKED_CCYS = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"}
_TZ_DUBAI = ZoneInfo("Asia/Dubai")

# Keyword sets for tiering (all lowercase)
_CB_KEYWORDS = {
    "rate decision", "interest rate decision", "monetary policy",
    "rate statement", "rate vote",
}
_TIER1_KEYWORDS = {
    "fomc", "nfp", "non-farm", "non farm", "payrolls",
    "cpi", "consumer price index",
    "fed chair", "fed minutes", "federal open",
    "boe", "ecb", "rba rate", "boc rate", "rbnz rate", "snb rate", "boj rate",
    "bank of england", "bank of canada", "reserve bank", "european central",
} | _CB_KEYWORDS
_TIER2_KEYWORDS = {
    "gdp", "gross domestic",
    "pce", "personal consumption",
    "pmi", "purchasing managers",
    "unemployment", "jobless", "claimant",
    "retail sales", "employment change",
    "job", "payroll",
}


def _is_cb(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _CB_KEYWORDS)


def _tier(title: str) -> tuple[int, int, str]:
    """Return (pre_min, post_min, kind) for a HIGH-impact event title."""
    t = title.lower()
    if any(kw in t for kw in _TIER1_KEYWORDS):
        kind = "cb" if _is_cb(t) else "high"
        return 60, 45, kind
    if any(kw in t for kw in _TIER2_KEYWORDS):
        return 30, 30, "high"
    return 15, 15, "high"


def _fetch(url: str = _CALENDAR_URL, timeout: int = 20) -> list[dict]:
    import time as _time
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(3):
        try:
            with urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            last_exc = exc
            # 429 rate-limit: back off and retry; other errors: fail immediately
            if "429" in str(exc) and attempt < 2:
                _time.sleep(60 * (attempt + 1))
            else:
                raise
    raise last_exc


def _today_events(events: list[dict], today_dubai) -> list[dict]:
    """Filter for HIGH-impact events on today's Dubai date for tracked currencies."""
    day_start = datetime(today_dubai.year, today_dubai.month, today_dubai.day,
                         tzinfo=_TZ_DUBAI).astimezone(timezone.utc)
    day_end = day_start + timedelta(hours=24)
    out = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        ccy = ev.get("country", "").upper()
        if ccy not in _TRACKED_CCYS:
            continue
        try:
            raw = ev["date"]
            dt_utc = datetime.fromisoformat(raw).astimezone(timezone.utc)
        except Exception:
            continue
        if day_start <= dt_utc < day_end:
            out.append({**ev, "_utc": dt_utc})
    return sorted(out, key=lambda e: e["_utc"])


def _to_windows(events: list[dict]) -> list[dict]:
    """Convert filtered events to engine news_window dicts."""
    windows = []
    for ev in events:
        dt_utc = ev["_utc"]
        pre, post, kind = _tier(ev.get("title", ""))
        start = (dt_utc - timedelta(minutes=pre)).replace(second=0, microsecond=0)
        end = (dt_utc + timedelta(minutes=post)).replace(second=0, microsecond=0)
        windows.append({
            "ccy": ev["country"].upper(),
            "start_iso": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_iso": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": ev.get("title", ""),
            "kind": kind,
        })
    return windows


def run(client, state: dict) -> list[dict]:
    """Fetch calendar, compute windows, write to state, send Telegram. Returns windows list."""
    today_dubai = state_mod.now_dubai().date()

    try:
        all_events = _fetch()
    except Exception as e:
        raise RuntimeError(f"ForexFactory fetch failed — {e}") from e

    events = _today_events(all_events, today_dubai)
    windows = _to_windows(events)

    # Write to state
    state["news_windows"] = windows
    state["news_windows_date"] = state_mod.now_ftmo().date().isoformat()
    state_mod.save(state)

    # Draw on charts (best effort)
    try:
        from . import chart_overlay
        chart_overlay.clear_news_lines(client)
        chart_overlay.draw_news_lines(client, windows)
    except Exception:
        pass

    # Telegram brief
    now_s = state_mod.now_dubai().strftime("%H:%M")
    if events:
        event_lines = []
        for ev in events:
            t_utc = ev["_utc"].strftime("%H:%MZ")
            t_dubai = ev["_utc"].astimezone(_TZ_DUBAI).strftime("%H:%M")
            pre, post, kind = _tier(ev.get("title", ""))
            tier_s = f"−{pre}/+{post}min"
            cb_tag = " [CB]" if kind == "cb" else ""
            event_lines.append(
                f"  {telegram.code(t_dubai)} {telegram.esc(ev['country'])} "
                f"{telegram.esc(ev.get('title', '')[:40])}{cb_tag} "
                f"({tier_s} · {t_utc})"
            )
        events_text = "\n".join(event_lines)
    else:
        events_text = "  <i>No HIGH-impact events today — all pairs tradeable</i>"

    lines = [
        f"🌅 <b>Morning Brief — {today_dubai.strftime('%a %d %b')}</b>  {now_s} Dubai",
        f"",
        f"📅 Today's events ({len(events)} HIGH-impact):",
        events_text,
    ]
    if windows:
        blocked = sorted({w["ccy"] for w in windows})
        lines.append(f"\n🚫 Blackout currencies: {', '.join(blocked)}")

    telegram.send("\n".join(lines))
    return windows
