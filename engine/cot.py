"""CFTC Traders in Financial Futures (TFF) — FX macro bias layer.

Downloads the CFTC weekly TFF report for major FX futures, computes Leveraged Money
(hedge fund / CTA) net positioning, and ranks each currency against a rolling 52-week
history stored locally in logs/cot_history.json.

Saves cot_bias.json at ~/trading/ for the weekly plan to read. History accumulates on
each run so the percentile window grows over time (weeks 1–51 use whatever is available).

CFTC releases every Friday ~15:30 ET (23:30 Dubai). Run Saturday morning via launchd.
Data: https://www.cftc.gov/dea/newcot/f_natfin.txt — free, no API key.
"""
from __future__ import annotations
import csv
import io
import json
from datetime import datetime, timezone
from urllib.request import urlopen

from . import config

_TFF_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
_HISTORY_FILE = config.LOG_DIR / "cot_history.json"
_BIAS_FILE = config.ROOT / "cot_bias.json"

# CFTC market name → currency code (order matters for display)
_MARKET_CCY: dict[str, str] = {
    "U.S. DOLLAR INDEX - ICE FUTURES U.S.": "DXY",
    "EURO FX - CHICAGO MERCANTILE EXCHANGE": "EUR",
    "BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE": "GBP",
    "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE": "JPY",
    "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE": "AUD",
    "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE": "CAD",
    "NEW ZEALAND DOLLAR - CHICAGO MERCANTILE EXCHANGE": "NZD",
    "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE": "CHF",
}

CROWDED_LONG_PCT = 80   # ≥ this percentile → specs heavily long → squeeze risk
CROWDED_SHORT_PCT = 20  # ≤ this percentile → specs heavily short → squeeze risk
LOOKBACK_WEEKS = 52


# ---------------------------------------------------------------------------
# Download + parse
# ---------------------------------------------------------------------------

def _fetch(url: str = _TFF_URL, timeout: int = 30) -> str:
    import ssl, certifi
    from urllib.request import Request
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def _int(s: str) -> int:
    return int((s or "0").replace(",", "").strip() or "0")


def parse_tff(text: str) -> dict[str, dict[str, dict]]:
    """Parse CFTC TFF comma-delimited text (positional short format, no header row).

    Column layout (0-indexed):
      0  = Market_and_Exchange_Names
      2  = Report_Date_as_YYYY-MM-DD
      14 = Lev_Money_Positions_Long_All
      15 = Lev_Money_Positions_Short_All

    Returns {ccy: {date_iso: {long, short, net}}} — only the currencies we track.
    Values are Leveraged Money (hedge fund / CTA) contract counts.
    """
    reader = csv.reader(io.StringIO(text))
    out: dict[str, dict] = {ccy: {} for ccy in _MARKET_CCY.values()}
    for row in reader:
        if len(row) < 16:
            continue
        name = row[0].strip().strip('"')
        ccy = _MARKET_CCY.get(name)
        if not ccy:
            continue
        date = row[2].strip()
        if not date or len(date) != 10:
            continue
        try:
            longs = _int(row[14])
            shorts = _int(row[15])
        except (ValueError, IndexError):
            continue
        out[ccy][date] = {"long": longs, "short": shorts, "net": longs - shorts}
    return out


# ---------------------------------------------------------------------------
# History persistence (local accumulator — grows over time)
# ---------------------------------------------------------------------------

def _load_history() -> dict[str, dict]:
    if not _HISTORY_FILE.exists():
        return {ccy: {} for ccy in _MARKET_CCY.values()}
    try:
        h = json.loads(_HISTORY_FILE.read_text())
        # Ensure all tracked currencies exist as keys
        for ccy in _MARKET_CCY.values():
            h.setdefault(ccy, {})
        return h
    except Exception:
        return {ccy: {} for ccy in _MARKET_CCY.values()}


def _save_history(h: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(h, indent=2, sort_keys=True))


def _merge(history: dict, fresh: dict) -> int:
    """Merge fresh records into history. Returns count of new records added."""
    added = 0
    for ccy, records in fresh.items():
        existing = history.setdefault(ccy, {})
        for date, rec in records.items():
            if date not in existing:
                existing[date] = rec
                added += 1
    return added


# ---------------------------------------------------------------------------
# Bias computation
# ---------------------------------------------------------------------------

def _percentile(values: list[float], v: float) -> float:
    """Rank of v within values expressed as 0–100 percentile."""
    if not values:
        return 50.0
    return round(100.0 * sum(1 for x in values if x < v) / len(values), 1)


def _signal(pct: float) -> str:
    if pct >= CROWDED_LONG_PCT:
        return "crowded_long"
    if pct <= CROWDED_SHORT_PCT:
        return "crowded_short"
    return "neutral"


def compute_bias(history: dict[str, dict]) -> dict[str, dict]:
    """Compute the latest net, 52-week percentile, and signal for each currency."""
    result = {}
    for ccy, records in history.items():
        if not records:
            continue
        sorted_dates = sorted(records)
        window = sorted_dates[-LOOKBACK_WEEKS:]
        nets = [records[d]["net"] for d in window]
        latest = window[-1]
        # Rank current week against all PRIOR weeks in the window (not itself)
        pct = _percentile(nets[:-1], nets[-1]) if len(nets) > 1 else 50.0
        result[ccy] = {
            "date": latest,
            "net": nets[-1],
            "long": records[latest]["long"],
            "short": records[latest]["short"],
            "percentile": pct,
            "signal": _signal(pct),
            "weeks_of_history": len(window),
        }
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update(url: str = _TFF_URL) -> tuple[dict, int]:
    """Download → merge → compute → save. Returns (bias, new_records_added)."""
    text = _fetch(url)
    fresh = parse_tff(text)
    history = _load_history()
    added = _merge(history, fresh)
    _save_history(history)
    bias = compute_bias(history)
    _BIAS_FILE.write_text(json.dumps({
        "updated": datetime.now(timezone.utc).isoformat(),
        "new_records_added": added,
        "currencies": bias,
    }, indent=2))
    return bias, added


def load_bias() -> dict:
    """Load the last-saved bias dict (empty if never run)."""
    if not _BIAS_FILE.exists():
        return {}
    try:
        return json.loads(_BIAS_FILE.read_text()).get("currencies", {})
    except Exception:
        return {}


_DISPLAY_ORDER = ["DXY", "EUR", "GBP", "JPY", "AUD", "CAD", "NZD", "CHF"]


def format_report(bias: dict) -> str:
    if not bias:
        return "No COT data — run `ftmo cot-update` first."
    lines = ["COT Macro Bias (CFTC Leveraged Money, 52-week percentile)"]
    for ccy in _DISPLAY_ORDER:
        d = bias.get(ccy)
        if not d:
            continue
        net_k = d["net"] / 1000
        pct = d["percentile"]
        sig = d["signal"]
        icon = "crowded-LONG" if sig == "crowded_long" else "crowded-SHORT" if sig == "crowded_short" else "neutral"
        weeks = d["weeks_of_history"]
        lines.append(
            f"  {ccy:4s}  net {net_k:+7.1f}k  |  {pct:5.1f}th pctl  |  {icon:<14}  ({weeks}w)"
        )
    last_date = next((bias[c]["date"] for c in _DISPLAY_ORDER if c in bias), "n/a")
    lines.append(f"  data as of {last_date}")
    return "\n".join(lines)
