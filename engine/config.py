"""Single source of truth for all risk-rail constants and paths.

Every hard rail in the spec is encoded here as a constant and enforced in rails.py.
Nothing in this file may be overridden at runtime by the model. The scheduled Claude
run is permission-blocked from cTrader write tools; the only writer is this engine.
"""
import os
from pathlib import Path

# ---- Paths ---------------------------------------------------------------
ROOT = Path(os.path.expanduser("~/trading"))
ENGINE = ROOT / "engine"
STATE_FILE = ROOT / "state.json"
JOURNAL_FILE = ROOT / "journal.md"
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs"
REQUEST_LOG = LOG_DIR / "requests.json"   # daily MCP request counter

# ---- Watchlist (instruments the operator scans top-down) -----------------
WATCHLIST = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
             "USDCHF", "EURJPY", "GBPJPY", "EURGBP", "XAUUSD",
             # non-USD crosses so USD-news days (FOMC/NFP/CPI) still leave tradeable pairs:
             "AUDJPY", "CADJPY", "NZDJPY", "EURAUD", "GBPAUD", "EURCHF"]
WATCHLIST_NAME = "FTMO"   # cTrader watchlist the engine keeps populated so quotes stay subscribed

# ---- Timezone ------------------------------------------------------------
TZ = "Asia/Dubai"  # UTC+4, operator local time; daily resets keyed to this

# ---- cTrader MCP endpoint ------------------------------------------------
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:9876/mcp/")
MCP_PROTOCOL = "2025-06-18"
ACCOUNT_CCY = "USD"

# ---- HARD RISK RAILS (code-enforced) -------------------------------------
DAILY_LOSS_LIMIT_PCT = 2.0      # kill-switch: halt new trades at -2% of day_start_balance
RISK_PCT_MIN = 0.25             # per-trade risk floor (reduce-after-loss target)
RISK_PCT_MAX = 0.5              # per-trade risk ceiling, never exceeded
AGG_RISK_PCT_MAX = 1.0          # aggregate open risk ceiling across all positions
MAX_SAME_CCY_POSITIONS = 2      # don't stack >2 open positions exposed the same way to one currency
MAX_TRADES_PER_DAY = 5          # counts FILLS (positions opened today), not pending placements
MAX_POOR_OUTCOMES = 2           # stop for the day after 2 losses/scratches
MIN_RR = 1.5                    # minimum reward:risk
NEWS_WINDOW_MIN = 15            # no entry +/- this many minutes around HIGH-impact events

# ---- FTMO broker limits ---------------------------------------------------
# NEW account: 2-Step Challenge, FTMO STANDARD (leverage 1:100, intraday), cTrader, $10,000 USD.
# Phase 1 profit target 10%, Phase 2 5%; max loss 10% (static), max daily loss 5%, min 4 trading days.
# Dollar limits are identical to the prior trial ($500 daily / $1,000 overall on $10k).
# STANDARD account rules (stricter than Swing): NO holding positions over the weekend; restricted
# news trading (FTMO 2-min rule — our ±15-min blackout is already stricter). Login set on purchase.
FTMO_LIMITS_CONFIRMED = True
FTMO_ACCOUNT_SIZE = 10000.0
FTMO_INITIAL_BALANCE = 10000.0       # basis for the profit target (FTMO measures from initial)
FTMO_DAILY_LOSS_USD = 500.0          # 5% of $10k
FTMO_OVERALL_LOSS_USD = 1000.0       # 10% of $10k
FTMO_OVERALL_FLOOR_USD = FTMO_ACCOUNT_SIZE - FTMO_OVERALL_LOSS_USD   # static $9,000 floor
FTMO_PROFIT_TARGET_USD = 1000.0      # Phase 1 = 10%; Verification = 5% ($500); Funded = none
FTMO_DAILY_LOSS_LIMIT_PCT = 5.0      # kept for the buffer math (stricter -2% still governs)
FTMO_MAX_LOSS_LIMIT_PCT = 10.0
# Phase-aware: challenge_phase_1 | verification_phase_2 | funded
FTMO_PHASE = "challenge_phase_1"
FTMO_PRODUCT = "two_step_standard"
FTMO_MIN_TRADING_DAYS = 4
FTMO_RESET_TZ = "Europe/Prague"      # FTMO daily-loss resets at 00:00 CE(S)T, NOT Dubai
FTMO_STOP_AT_TARGET = True           # once the profit target is reached, take no new risk (protect the pass)

# ---- Operational fail-closed controls ------------------------------------
UNREACHABLE_FREEZE_CYCLES = 2        # freeze new entries after this many consecutive unreachable cycles
PENDING_MAX_HOURS = 6               # a resting order auto-cancels after this many hours (stale thesis)
DUP_ENTRY_PIPS = 10                 # reject a new order within this many pips of an existing same-way order
MAX_TRADE_AGE_HOURS = 72            # force-exit an engine position older than this (forgotten-position guard)
FREEZE_ON_UNKNOWN_POSITION = False  # OFF: user may place own manual trades; engine keeps running
DATA_FEED_MIN_QUOTING = 4           # if fewer than this many watchlist symbols quote, treat feed as down

# ---- Request throttle (FTMO forbids >2000 server requests/day) -----------
REQUEST_CAP_PER_DAY = 1800       # hard cap, under FTMO's 2000 (watchdog + scanner + runs)

# ---- Market scanner (engine, every 30 min during active hours) ------------
SCAN_HOURS_DUBAI = (10, 22)      # only scan between 10:00 and 22:00 local (London+NY)
SCAN_NEAR_ATR = 0.5              # flag "near level" when price within this * ATR of a 20D high/low
SCAN_ALERT_COOLDOWN_MIN = 75     # don't re-ping the same symbol+level within this many minutes
                                 # (price oscillating in/out of the band was spamming repeat alerts)
SCAN_WITH_TREND_ONLY = True      # only ping support when D1 bias is bear / resistance when bull —
                                 # suppresses "price stalling at a level that's holding" false signals
SCAN_TREND_LOOKBACK = 10         # D1 bars over which the SMA20 slope is measured for the regime read
SCAN_TREND_MIN_ATR = 1.0         # SMA20 must move >= this many ATRs over the lookback to count as a
                                 # real TREND (else RANGE) — a slope-based filter, steadier than bias
SCAN_TREND_REGIME_ONLY = True    # gate pings on a trending regime aligned with the level (support needs
                                 # trend_down, resistance needs trend_up) — drops touches in chop/ranges
SCAN_MIN_VIABLE_STOP_PIPS = 5   # suppress Telegram alert when price is within this many pips of the level
                                 # (geometry too tight for a meaningful stop placement)
# ---- Shadow journal (graded would-have outcomes — measures the filtering edge) -----
SHADOW_EXPIRY_HOURS = 168        # an ungraded candidate auto-closes as "expired" after this long (1 week)
RETRY_MAX = 3                    # tool-call retry ceiling (order placement uses retries=1)
RETRY_BACKOFF_SEC = 2.0
# The session HANDSHAKE (connect) is idempotent, so it can patiently ride out a brief bridge
# blip without aborting a whole run. ~2+4+6+8+10+10 ≈ 40s total before giving up.
CONNECT_RETRIES = 6
CONNECT_BACKOFF_MAX_SEC = 10.0

# ---- Active trade management (engine Trade Manager, every 5 min) ----------
BE_TRIGGER_R = 1.0       # move stop to breakeven once trade is +1R in profit
PARTIAL_R = 2.0          # take a partial at +2R
PARTIAL_PCT = 0.5        # fraction of the position to close at PARTIAL_R
TRAIL_START_R = 2.0      # begin step-trailing the stop from +2R
TRAIL_STEP_R = 1.0       # each full +R beyond start, ratchet stop up by 1R
MANAGE_LABEL = "ftmo-engine"   # only auto-manage positions the engine itself opened

# Weekend-flat (FTMO Standard forbids holding over the weekend). The engine closes all of its
# own positions + cancels its pending orders on Friday at/after this Dubai hour (well before the
# Fri ~22:00 GMT close). Needs cTrader "Require confirmation" OFF to act unattended.
WEEKEND_FLAT_ENABLED = True
WEEKEND_FLAT_HOUR_DUBAI = 23    # Friday hour (local) to start force-flat

# Pre-news flatten: when a HIGH/CB blackout window opens, close engine positions + cancel engine
# orders on the affected currency (so a pre-news trade is never held through the event).
NEWS_FLATTEN_ENABLED = True

# ---- Sanity-gate thresholds ---------------------------------------------
SPREAD_SPIKE_MULT = 3.0          # spread > this * typical => abort (spiked)
QUOTE_MAX_AGE_SEC = 30           # quote older than this => stale => abort
STOP_SLIPPAGE_PIPS = 1.5         # assume the stop fills this many pips WORSE than its price; folded
                                 # into worst-case so the daily/overall/FTMO floor math stays
                                 # conservative (size is unchanged — still risk-correct from the stop)

# Typical calm-market spread in pips per symbol; baseline for the spike check.
# Conservative values; absent symbols skip the spike check (stale-quote check still applies).
TYPICAL_SPREADS = {
    "EURUSD": 0.6, "GBPUSD": 0.9, "USDJPY": 0.8, "USDCHF": 1.0, "AUDUSD": 0.8,
    "USDCAD": 1.2, "NZDUSD": 1.3, "EURGBP": 1.0, "EURJPY": 1.3, "GBPJPY": 1.8,
    "XAUUSD": 3.0, "AUDJPY": 1.8, "CADJPY": 2.0, "NZDJPY": 2.4, "EURAUD": 1.8,
    "GBPAUD": 3.0, "EURCHF": 1.5,
}

# ---- Semi-auto confirmation window ---------------------------------------
# With cTrader "Require confirmation" ON, the engine waits this long for the user to tap
# confirm before treating the order as skipped. A pre-place Telegram heads-up is sent so
# the user can reach cTrader in time.
CONFIRM_TIMEOUT_SEC = 300

# ---- Arming --------------------------------------------------------------
def is_armed() -> bool:
    """Execution is live only when .env ARMED=true. Default OFF."""
    return _env("ARMED", "false").strip().lower() == "true"


# ---- .env loader (no external dependency) --------------------------------
_ENV_CACHE = None


def _load_env() -> dict:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                _ENV_CACHE[k.strip()] = v.strip()
    return _ENV_CACHE


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _load_env().get(key, default)


def telegram_token() -> str:
    return _env("TELEGRAM_TOKEN", "")


def telegram_chat_id() -> str:
    return _env("TELEGRAM_CHAT_ID", "")


def gsheet_id() -> str:
    return _env("GSHEET_ID", "")


def google_sa_json() -> str:
    return _env("GOOGLE_SA_JSON", "")


def uptime_kuma_url() -> str:
    """Push URL for the Uptime Kuma heartbeat monitor. Empty = disabled."""
    return _env("UPTIME_KUMA_URL", "")


def anthropic_api_key() -> str:
    return _env("ANTHROPIC_API_KEY", "")
