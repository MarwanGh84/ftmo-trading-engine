"""Pure position-sizing and buffer math. No I/O, no side effects -> fully unit-tested.

Size is ALWAYS derived from the stop distance, never from desired profit.
We never widen a stop to fit a size.
"""
from __future__ import annotations
import math
from dataclasses import dataclass


def stop_pips(entry: float, stop: float, pip_size: float) -> float:
    """Distance from entry to stop, in pips. Always positive."""
    if pip_size <= 0:
        raise ValueError("pip_size must be > 0")
    return abs(entry - stop) / pip_size


def risk_dollars(balance: float, risk_pct: float) -> float:
    return balance * risk_pct / 100.0


def pip_value_per_lot_usd(pip_size: float, lot_size: float, quote_to_usd: float) -> float:
    """Value of one pip per 1.0 lot, expressed in USD.

    quote_to_usd converts the symbol's QUOTE currency into USD:
      - EURUSD (quote USD)     -> quote_to_usd = 1.0          -> $10.00 / pip / lot
      - USDJPY (quote JPY)     -> quote_to_usd = 1 / USDJPY   -> ~$6.24 / pip / lot
      - EURGBP (quote GBP)     -> quote_to_usd = GBPUSD       -> price-dependent
    """
    if quote_to_usd <= 0:
        raise ValueError("quote_to_usd must be > 0")
    return pip_size * lot_size * quote_to_usd


def lots_for_risk(risk_d: float, stop_pips_: float, pip_value_lot_usd: float) -> float:
    """Lots such that (stop distance) * (pip value) == risk_d. Unrounded."""
    if stop_pips_ <= 0 or pip_value_lot_usd <= 0:
        raise ValueError("stop_pips and pip_value must be > 0")
    return risk_d / (stop_pips_ * pip_value_lot_usd)


def units_from_lots(lots: float, lot_size: float, min_volume: float, volume_step: float) -> float:
    """Convert lots -> units, snapped DOWN to the broker volume grid.

    Snapping down guarantees we never exceed the intended risk. Returns 0.0 if the
    risk-correct size is below the broker minimum (caller must then reject the trade).
    """
    raw_units = lots * lot_size
    steps = math.floor(raw_units / volume_step)
    units = steps * volume_step
    if units < min_volume:
        return 0.0
    return float(units)


def worst_case_usd(units: float, stop_pips_: float, pip_value_lot_usd: float,
                   lot_size: float, spread_cost: float = 0.0,
                   commission: float = 0.0, swap: float = 0.0,
                   slippage: float = 0.0) -> float:
    """Worst-case loss if stop is hit, including frictions. Positive number = loss.

    `slippage` is the extra loss assumed when a stop fills worse than its price (stops are
    not guaranteed; fast/post-news tape gaps through them). Including it here makes the buffer
    rails conservative without changing position size.
    """
    lots = units / lot_size
    stop_loss = lots * stop_pips_ * pip_value_lot_usd
    return stop_loss + abs(spread_cost) + abs(commission) + abs(swap) + abs(slippage)


def reward_risk(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        raise ValueError("risk distance must be > 0")
    return reward / risk


@dataclass
class Buffers:
    """How much loss room remains before each limit, before and after a candidate trade."""
    daily_room_now: float       # $ until -2% kill-switch (off day_start_balance)
    daily_room_after: float     # $ remaining if this trade's worst case is realized
    overall_room_now: float     # $ until overall max-loss floor (off account_baseline)
    overall_room_after: float


def buffers(balance: float, equity: float, day_start_balance: float,
            account_baseline: float, daily_loss_limit_pct: float,
            overall_loss_limit_pct: float, candidate_worst_case: float,
            open_floating_loss: float = 0.0) -> Buffers:
    """Compute remaining loss room vs the daily and overall floors.

    Floors are absolute equity levels we must not breach:
      daily_floor   = day_start_balance * (1 - daily_loss_limit_pct/100)
      overall_floor = account_baseline  * (1 - overall_loss_limit_pct/100)
    Room "now" is current equity minus the floor. Room "after" subtracts the
    candidate trade's worst-case loss (open floating loss is already in equity).
    """
    daily_floor = day_start_balance * (1 - daily_loss_limit_pct / 100.0)
    overall_floor = account_baseline * (1 - overall_loss_limit_pct / 100.0)
    daily_now = equity - daily_floor
    overall_now = equity - overall_floor
    return Buffers(
        daily_room_now=daily_now,
        daily_room_after=daily_now - candidate_worst_case,
        overall_room_now=overall_now,
        overall_room_after=overall_now - candidate_worst_case,
    )
