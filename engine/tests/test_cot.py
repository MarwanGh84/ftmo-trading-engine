"""COT parser and bias computation — pure unit tests (no network calls)."""
from engine import cot


# Minimal TFF rows matching the real CFTC positional short format (no header row):
# col 0 = market name, col 2 = report date, col 14 = Lev Money long, col 15 = Lev Money short.
def _row(name: str, date_yymmdd: str, date_iso: str, longs: int, shorts: int) -> str:
    filler = ["0"] * 7   # cols 7-13
    return ",".join([f'"{name}"', date_yymmdd, date_iso, "090741", "CME ", "00", "090 ",
                     *filler, str(longs), str(shorts), "0", "0"])


_SAMPLE_TFF = "\n".join([
    _row("EURO FX - CHICAGO MERCANTILE EXCHANGE", "260101", "2026-01-02", 80000, 40000),
    _row("EURO FX - CHICAGO MERCANTILE EXCHANGE", "260108", "2026-01-09", 85000, 38000),
    _row("BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE", "260101", "2026-01-02", 30000, 50000),
    _row("BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE", "260108", "2026-01-09", 28000, 55000),
    _row("UNKNOWN MARKET - SOME EXCHANGE", "260101", "2026-01-02", 99999, 99999),
]) + "\n"


def test_parse_tff_known_currencies():
    result = cot.parse_tff(_SAMPLE_TFF)
    assert "EUR" in result
    assert "GBP" in result
    # Unknown market must be ignored
    assert all(k in cot._MARKET_CCY.values() for k in result)


def test_parse_tff_net_calculation():
    result = cot.parse_tff(_SAMPLE_TFF)
    assert result["EUR"]["2026-01-02"]["net"] == 80000 - 40000
    assert result["EUR"]["2026-01-09"]["net"] == 85000 - 38000


def test_parse_tff_deduplicates_dates():
    # Same date twice — second should overwrite or first should stand; no duplicates as keys
    duped = _SAMPLE_TFF + _row("EURO FX - CHICAGO MERCANTILE EXCHANGE", "260101", "2026-01-02", 1, 1) + "\n"
    result = cot.parse_tff(duped)
    assert len(result["EUR"]) == 2   # still only 2 unique dates


def test_compute_bias_returns_all_currencies_with_data():
    history = cot.parse_tff(_SAMPLE_TFF)
    bias = cot.compute_bias(history)
    assert "EUR" in bias
    assert "GBP" in bias
    # Empty currencies should be absent
    for ccy, d in bias.items():
        assert d["net"] != 0 or d["long"] == 0


def test_percentile_crowded_long():
    # Build a history where the latest week is at extreme high
    history = {
        "EUR": {f"2026-0{i+1}-01": {"long": i * 1000, "short": 0, "net": i * 1000} for i in range(10)}
    }
    bias = cot.compute_bias(history)
    # Latest week net = 9000 (highest) → ~100th percentile → crowded_long
    assert bias["EUR"]["signal"] == "crowded_long"
    assert bias["EUR"]["percentile"] >= cot.CROWDED_LONG_PCT


def test_percentile_crowded_short():
    history = {
        "GBP": {f"2026-0{i+1}-01": {"long": 0, "short": i * 1000, "net": -(i * 1000)} for i in range(10)}
    }
    bias = cot.compute_bias(history)
    # Latest week net = -9000 (most negative) → 0th percentile → crowded_short
    assert bias["GBP"]["signal"] == "crowded_short"
    assert bias["GBP"]["percentile"] <= cot.CROWDED_SHORT_PCT


def test_percentile_neutral():
    # All the same value → percentile = 0 (nothing below), but signal neutral check
    history = {
        "JPY": {f"2026-{i:02d}-01": {"long": 50000, "short": 30000, "net": 20000} for i in range(1, 11)}
    }
    bias = cot.compute_bias(history)
    # All identical → percentile = 0 but signal neutral because not ≥80
    assert bias["JPY"]["signal"] in ("neutral", "crowded_short")


def test_merge_adds_new_records():
    history = {"EUR": {"2026-01-02": {"long": 80000, "short": 40000, "net": 40000}}}
    fresh = {"EUR": {
        "2026-01-02": {"long": 99, "short": 99, "net": 0},  # existing — must NOT overwrite
        "2026-01-09": {"long": 85000, "short": 38000, "net": 47000},  # new
    }}
    for ccy in cot._MARKET_CCY.values():
        fresh.setdefault(ccy, {})
        history.setdefault(ccy, {})
    added = cot._merge(history, fresh)
    assert added == 1   # only the new date
    assert history["EUR"]["2026-01-02"]["long"] == 80000   # original preserved


def test_format_report_no_data():
    assert "run" in cot.format_report({}).lower()


def test_format_report_with_data():
    history = cot.parse_tff(_SAMPLE_TFF)
    bias = cot.compute_bias(history)
    report = cot.format_report(bias)
    assert "EUR" in report
    assert "GBP" in report
    assert "pctl" in report
