"""Performance analytics over closed trades — so you can tell whether the strategy actually
has an edge. Pure math (`compute_stats`) plus a per-pair breakdown."""
from __future__ import annotations


def compute_stats(nets: list[float]) -> dict:
    """Aggregate win rate / profit factor / expectancy from a list of net P/L per trade."""
    count = len(nets)
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / count) if count else 0.0,
        "net": sum(nets),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "expectancy": (sum(nets) / count) if count else 0.0,
    }


def engine_trades(client, label: str, include_all: bool = False) -> list[dict]:
    """Pull closed trades from cTrader order history as [{symbol, net, setup, regime, confidence}].
    Shared by `ftmo stats` and the dashboard so both read the same source. Network-touching."""
    h = client.call("get_order_history")
    items = h if isinstance(h, list) else (h.get("history") or h.get("trades") or h.get("orders") or [])
    trades = []
    for it in items:
        if not include_all and it.get("label") != label:
            continue
        parts = (it.get("comment", "") or "").split("|")   # setup|regime|confidence
        trades.append({"symbol": it.get("symbolName", "?"), "net": float(it.get("netProfit") or 0),
                       "setup": parts[0] if parts else "", "regime": parts[1] if len(parts) > 1 else "",
                       "confidence": conf_bucket(parts[2] if len(parts) > 2 else "")})
    return trades


def _group(trades: list[dict], key: str, default: str) -> dict:
    groups: dict[str, list[float]] = {}
    for t in trades:
        groups.setdefault(t.get(key) or default, []).append(t.get("net", 0.0))
    return {k: compute_stats(nets) for k, nets in groups.items()}


def by_symbol(trades: list[dict]) -> dict:
    """trades: [{symbol, net}] -> {symbol: stats}."""
    return _group(trades, "symbol", "?")


def by_setup(trades: list[dict]) -> dict:
    """trades: [{setup, net}] -> {setup_type: stats} — to find which setups actually have edge."""
    return _group(trades, "setup", "untagged")


def by_regime(trades: list[dict]) -> dict:
    """trades: [{regime, net}] -> {regime: stats} — a setup may only work in one regime."""
    return _group(trades, "regime", "untagged")


def conf_bucket(conf) -> str:
    """Map a Claude confidence (0-100) to a 10-wide bucket, e.g. '80-89'. '' if not numeric."""
    try:
        c = int(float(conf))
    except (ValueError, TypeError):
        return ""
    lo = max(0, min(90, (c // 10) * 10))
    return f"{lo}-{lo + 9}"


def format_summary(s: dict) -> str:
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return (f"{s['trades']} trades · {s['win_rate']*100:.0f}% win · PF {pf} · "
            f"net ${s['net']:.2f} · exp ${s['expectancy']:.2f}/trade · "
            f"avg win ${s['avg_win']:.2f} / avg loss ${s['avg_loss']:.2f}")
