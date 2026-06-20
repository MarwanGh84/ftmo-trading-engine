"""Live Google Sheets reporting. The engine (the single writer) appends to a Sheet on
every run and trade. Fully optional and fail-safe: if gspread isn't installed or the
.env values (GSHEET_ID, GOOGLE_SA_JSON) are missing, every function no-ops and logs —
it can NEVER throw into the trading path.

Setup (one time, see ~/trading/SETUP_SHEETS.md): create a Google Cloud service account,
enable the Sheets API, download its JSON key, create a Sheet and share it (Editor) with
the service account's client_email, then set GOOGLE_SA_JSON + GSHEET_ID in .env.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

from . import config

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _IMPORT_OK = True
except Exception:  # library not installed -> feature disabled
    _IMPORT_OK = False

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_TABS = {
    "Dashboard": [],
    "Trades": ["Time", "Symbol", "Side", "Type", "Units", "Lots", "SL pips", "TP pips",
               "Risk $", "Risk %", "R:R", "WorstCase $", "Status", "Detail"],
    "Runs": ["Time", "Run", "Action", "Summary"],
    "Watchlist": ["Symbol", "Price", "D1 Bias", "Regime", "20D Low", "20D High", "Near", "Note",
                  "Updated"],
    "Shadow": ["Time", "Symbol", "Side", "Verdict", "Entry", "Stop", "Target", "Setup", "Conf",
               "Status", "Result"],
}
_ss_cache = None


def _log(line: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_DIR / "sheets.log", "a") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z  {line}\n")


def enabled() -> bool:
    return bool(_IMPORT_OK and config.gsheet_id() and config.google_sa_json()
               and Path(config.google_sa_json()).exists())


def _spreadsheet():
    global _ss_cache
    if _ss_cache is not None:
        return _ss_cache
    creds = Credentials.from_service_account_file(config.google_sa_json(), scopes=_SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(config.gsheet_id())
    _ensure_tabs(ss)
    _ss_cache = ss
    return ss


_LOG_WIDTHS = {
    "Trades": [112, 78, 52, 58, 68, 56, 62, 62, 70, 58, 52, 92, 96, 360],
    "Runs": [120, 86, 90, 520],
    "Watchlist": [80, 90, 70, 96, 90, 90, 90, 220, 80],
    "Shadow": [120, 78, 50, 64, 78, 78, 78, 150, 52, 70, 64],
}


def _ensure_tabs(ss) -> None:
    existing = {w.title: w for w in ss.worksheets()}
    for name, headers in _TABS.items():
        if name not in existing:
            ws = ss.add_worksheet(title=name, rows=400, cols=max(6, len(headers) or 6))
        else:
            ws = existing[name]
        if headers and ws.row_values(1) != headers:
            ws.update([headers], "A1")
        if headers:
            _style_log_tab(ws, _LOG_WIDTHS.get(name, []))


def _style_log_tab(ws, widths) -> None:
    """Professional styling for the Trades/Runs log tabs: coloured frozen header,
    tuned column widths, and a filter. Idempotent — safe to re-apply each run."""
    if not widths:
        return
    try:
        sid, ncols = ws.id, len(widths)
        last = chr(ord("A") + ncols - 1)
        ws.batch_format([{"range": f"A1:{last}1",
                          "format": _fmt(bg=_STEEL, color=_WHITE, bold=True, align="CENTER", valign="MIDDLE")}])
        reqs = [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                 "startIndex": i, "endIndex": i + 1}, "properties": {"pixelSize": w}, "fields": "pixelSize"}}
                for i, w in enumerate(widths)]
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                     "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 30}, "fields": "pixelSize"}})
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid,
                     "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
        reqs.append({"setBasicFilter": {"filter": {"range": {"sheetId": sid,
                     "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": ncols}}}})
        ws.spreadsheet.batch_update({"requests": reqs})
    except Exception as e:
        _log(f"style {ws.title} EXC {e}")


# ---- palette (professional navy / steel / status colours) ----------------
_NAVY, _NAVY2, _STEEL = "#0B2447", "#19376D", "#1F4E79"
_LABELBG, _WHITE, _DARK, _GREYTXT = "#EEF2F8", "#FFFFFF", "#1A2433", "#5B6B85"
_SUBTXT = "#C7D2E2"
_GREEN, _GREENBG = "#1E7E34", "#E6F4EA"
_RED, _REDBG = "#C0392B", "#FCE8E6"
_AMBER, _AMBERBG = "#B9770E", "#FCF3E3"


def _rgb(h: str) -> dict:
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _fmt(bg=None, color=None, bold=None, italic=None, size=None, align=None, valign=None) -> dict:
    f = {}
    if bg is not None:
        f["backgroundColor"] = _rgb(bg)
    tf = {}
    if color is not None:
        tf["foregroundColor"] = _rgb(color)
    if bold is not None:
        tf["bold"] = bold
    if italic is not None:
        tf["italic"] = italic
    if size is not None:
        tf["fontSize"] = size
    if tf:
        f["textFormat"] = tf
    if align is not None:
        f["horizontalAlignment"] = align
    if valign is not None:
        f["verticalAlignment"] = valign
    return f


def _bar(frac: float, width: int = 24) -> str:
    """Unicode progress bar built in Python (no SPARKLINE formula dependency)."""
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def _room_color(frac: float) -> str:
    """Green when there's plenty of buffer left, amber when tightening, red when nearly gone."""
    return _GREEN if frac > 0.5 else _AMBER if frac > 0.2 else _RED


def update_dashboard(snap: dict) -> bool:
    """Render the Dashboard tab: KPI cards + progress/risk gauges + schedule/news + performance."""
    if not enabled():
        _log("disabled, skip dashboard")
        return False
    try:
        from . import config as cfg
        ws = _spreadsheet().worksheet("Dashboard")
        if ws.col_count < 8:
            ws.add_cols(8 - ws.col_count)
        sid = ws.id

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        armed = cfg.is_armed()
        mode = "ARMED — LIVE EXECUTION" if armed else "DRY-RUN (disarmed)"
        ks_hit = snap.get("daily_limit_hit")
        frozen = snap.get("frozen")
        pnl = snap.get("daily_pnl", 0.0)
        profit = snap.get("profit", 0.0)
        to_target = snap.get("to_target", 0.0)
        equity = snap.get("equity", 0.0)
        balance = snap.get("balance", 0.0)
        target = cfg.FTMO_PROFIT_TARGET_USD
        daily_room = snap.get("daily_room", 0.0)
        overall_room = snap.get("overall_room", 0.0)
        daily_full = snap.get("daily_room_full") or (cfg.FTMO_INITIAL_BALANCE * cfg.DAILY_LOSS_LIMIT_PCT / 100)
        overall_full = snap.get("overall_room_full") or (cfg.FTMO_INITIAL_BALANCE * cfg.FTMO_MAX_LOSS_LIMIT_PCT / 100)
        tdays = snap.get("trading_days", 0)
        min_days = snap.get("min_trading_days", 4)
        status = ("FROZEN" if frozen else "HALTED −2%" if ks_hit
                  else "ARMED" if armed else "DISARMED")
        subtitle = f"Updated {ts}    ·    {snap.get('phase','')}    ·    {mode}"
        if frozen:
            subtitle += f"    ·    ⚠ {snap.get('frozen_reason','')}"

        tgt_frac = profit / target if target else 0.0
        daily_frac = daily_room / daily_full if daily_full else 0.0
        overall_frac = overall_room / overall_full if overall_full else 0.0
        days_frac = (tdays / min_days) if min_days else 1.0
        pnl_s = f"{'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}"

        st = snap.get("stats") or {}
        n = st.get("trades", 0)
        if n:
            pf = "∞" if st.get("profit_factor") == float("inf") else f"{st.get('profit_factor',0):.2f}"
            perf_line = (f"{n} trades  ·  {st.get('win_rate',0)*100:.0f}% win  ·  PF {pf}  ·  "
                         f"net ${st.get('net',0):,.2f}  ·  exp ${st.get('expectancy',0):,.2f}/trade")
            perf_caveat = (f"⚠ Sample too small to be meaningful (n={n}) — treat as noise until ~30–50 closed trades."
                           if n < 30 else "Edge is now measurable — review by setup/regime in `ftmo stats`.")
        else:
            perf_line = "No closed trades yet."
            perf_caveat = "Performance metrics populate here as trades close."

        grid = [
            ["FTMO OPERATOR DASHBOARD", "", "", "", "", "", "", ""],
            [subtitle, "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            ["EQUITY", "", "DAY P/L", "", "TO TARGET", "", "DAILY ROOM", ""],
            [f"${equity:,.2f}", "", pnl_s, "", f"${to_target:,.0f}", "", f"${daily_room:,.0f}", ""],
            ["", "", "", "", "", "", "", ""],
            ["PROGRESS & RISK", "", "", "", "", "", "", ""],
            ["Profit → target", "", _bar(tgt_frac), "", "", "", f"${profit:,.0f} / ${target:,.0f}  ({tgt_frac*100:.0f}%)", ""],
            ["Daily room (−2%)", "", _bar(daily_frac), "", "", "", f"${daily_room:,.0f} of ${daily_full:,.0f}", ""],
            ["Overall room", "", _bar(overall_frac), "", "", "", f"${overall_room:,.0f} of ${overall_full:,.0f}", ""],
            ["Trading days", "", _bar(days_frac), "", "", "", f"{tdays} / {min_days}", ""],
            ["", "", "", "", "", "", "", ""],
            ["DETAIL & STATUS", "", "", "", "", "", "", ""],
            ["Balance", f"${balance:,.2f}", "Open pos", f"{snap.get('open_positions',0)}",
             "Pending", f"{snap.get('pending_orders',0)}", "Kill-switch", "HIT" if ks_hit else "OK"],
            ["Trades today", f"{snap.get('trades_today',0)} / {cfg.MAX_TRADES_PER_DAY}",
             "Poor", f"{snap.get('poor_outcomes',0)} / {cfg.MAX_POOR_OUTCOMES}",
             "Mode", "LIVE" if armed else "DRY", "Status", status],
            ["", "", "", "", "", "", "", ""],
            ["SCHEDULE & NEWS", "", "", "", "", "", "", ""],
            ["Next run", snap.get("next_run", "—"), "", "News today", snap.get("news_today", "—"), "",
             "Wknd-flat", snap.get("weekend_flat", "—")],
            ["", "", "", "", "", "", "", ""],
            ["PERFORMANCE — since inception", "", "", "", "", "", "", ""],
            [perf_line, "", "", "", "", "", "", ""],
            [perf_caveat, "", "", "", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            ["Auto-updated by the FTMO operator engine — do not edit manually", "", "", "", "", "", "", ""],
        ]
        ws.clear()
        ws.update(grid, "A1")

        status_fmt = (_fmt(bg=_REDBG, color=_RED, bold=True, align="CENTER") if (frozen or ks_hit)
                      else _fmt(bg=_AMBERBG, color=_AMBER, bold=True, align="CENTER") if armed
                      else _fmt(color=_GREYTXT, bold=True, align="CENTER"))
        hdr = _fmt(bg=_STEEL, color=_WHITE, bold=True, size=11, align="LEFT", valign="MIDDLE")
        kpi_label = _fmt(bg=_STEEL, color=_SUBTXT, bold=True, size=9, align="CENTER", valign="MIDDLE")
        kpi_val = _fmt(bg=_WHITE, color=_DARK, bold=True, size=14, align="CENTER", valign="MIDDLE")
        lbl = _fmt(bg=_LABELBG, color=_DARK, bold=True, valign="MIDDLE")
        val = _fmt(bg=_WHITE, color=_DARK, align="CENTER", valign="MIDDLE")
        ws.batch_format([
            {"range": "A1:H1", "format": _fmt(bg=_NAVY, color=_WHITE, bold=True, size=16, align="CENTER", valign="MIDDLE")},
            {"range": "A2:H2", "format": _fmt(bg=_NAVY2, color=_SUBTXT, size=10, align="CENTER", valign="MIDDLE")},
            # KPI cards
            {"range": "A4:H4", "format": kpi_label},
            {"range": "A5:H5", "format": kpi_val},
            {"range": "C5:D5", "format": _fmt(bg=_WHITE, color=_GREEN if pnl >= 0 else _RED, bold=True, size=14, align="CENTER", valign="MIDDLE")},
            {"range": "E5:F5", "format": _fmt(bg=_WHITE, color=_GREEN if to_target <= 0 else _NAVY2, bold=True, size=14, align="CENTER", valign="MIDDLE")},
            {"range": "G5:H5", "format": _fmt(bg=_WHITE, color=_room_color(daily_frac), bold=True, size=14, align="CENTER", valign="MIDDLE")},
            # section headers
            {"range": "A7:H7", "format": hdr}, {"range": "A13:H13", "format": hdr},
            {"range": "A17:H17", "format": hdr}, {"range": "A20:H20", "format": hdr},
            # gauges: labels / bars / values
            {"range": "A8:B11", "format": lbl},
            {"range": "C8:F11", "format": _fmt(bg=_WHITE, size=11, align="LEFT", valign="MIDDLE")},
            {"range": "G8:H11", "format": _fmt(bg=_WHITE, color=_GREYTXT, align="LEFT", valign="MIDDLE")},
            {"range": "C8:F8", "format": _fmt(bg=_WHITE, color=_NAVY2, size=11, align="LEFT", valign="MIDDLE")},
            {"range": "C9:F9", "format": _fmt(bg=_WHITE, color=_room_color(daily_frac), size=11, align="LEFT", valign="MIDDLE")},
            {"range": "C10:F10", "format": _fmt(bg=_WHITE, color=_room_color(overall_frac), size=11, align="LEFT", valign="MIDDLE")},
            {"range": "C11:F11", "format": _fmt(bg=_WHITE, color=_STEEL, size=11, align="LEFT", valign="MIDDLE")},
            # detail/status rows: label cols A,C,E,G ; value cols B,D,F,H
            {"range": "A14:A15", "format": lbl}, {"range": "C14:C15", "format": lbl},
            {"range": "E14:E15", "format": lbl}, {"range": "G14:G15", "format": lbl},
            {"range": "B14:B15", "format": val}, {"range": "D14:D15", "format": val},
            {"range": "F14:F15", "format": val}, {"range": "H14:H15", "format": val},
            {"range": "H14", "format": (_fmt(bg=_REDBG, color=_RED, bold=True, align="CENTER") if ks_hit
                                        else _fmt(bg=_GREENBG, color=_GREEN, bold=True, align="CENTER"))},
            {"range": "H15", "format": status_fmt},
            # schedule/news
            {"range": "A18:A18", "format": lbl}, {"range": "D18:D18", "format": lbl}, {"range": "G18:G18", "format": lbl},
            {"range": "B18:C18", "format": _fmt(bg=_WHITE, color=_DARK, align="LEFT", valign="MIDDLE")},
            {"range": "E18:F18", "format": _fmt(bg=_WHITE, color=_DARK, align="LEFT", valign="MIDDLE")},
            {"range": "H18:H18", "format": _fmt(bg=_WHITE, color=_AMBER, bold=True, align="LEFT", valign="MIDDLE")},
            # performance
            {"range": "A21:H21", "format": _fmt(bg=_WHITE, color=_DARK, bold=True, size=11, align="LEFT", valign="MIDDLE")},
            {"range": "A22:H22", "format": _fmt(bg=_AMBERBG if n < 30 else _GREENBG,
                                                color=_AMBER if n < 30 else _GREEN, italic=True, size=9, align="LEFT")},
            {"range": "A24:H24", "format": _fmt(color=_GREYTXT, italic=True, size=9, align="CENTER")},
        ])

        def _col(i, px):
            return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                    "startIndex": i, "endIndex": i + 1}, "properties": {"pixelSize": px}, "fields": "pixelSize"}}

        def _row(i, px):
            return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS",
                    "startIndex": i, "endIndex": i + 1}, "properties": {"pixelSize": px}, "fields": "pixelSize"}}

        def _merge(r0, r1, c0, c1):
            return {"mergeCells": {"range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                    "startColumnIndex": c0, "endColumnIndex": c1}, "mergeType": "MERGE_ALL"}}

        side = {"style": "SOLID", "color": _rgb("#C9D4E5")}
        thick = {"style": "SOLID_MEDIUM", "color": _rgb("#9DB0CC")}
        cards = {"sheetId": sid, "startRowIndex": 3, "endRowIndex": 5, "startColumnIndex": 0, "endColumnIndex": 8}
        gauges = {"sheetId": sid, "startRowIndex": 7, "endRowIndex": 11, "startColumnIndex": 0, "endColumnIndex": 8}
        detail = {"sheetId": sid, "startRowIndex": 13, "endRowIndex": 15, "startColumnIndex": 0, "endColumnIndex": 8}
        requests = [
            {"unmergeCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 24,
                                        "startColumnIndex": 0, "endColumnIndex": 8}}},
            _merge(0, 1, 0, 8), _merge(1, 2, 0, 8),
            # KPI cards (2 cols each, label + value rows)
            _merge(3, 4, 0, 2), _merge(3, 4, 2, 4), _merge(3, 4, 4, 6), _merge(3, 4, 6, 8),
            _merge(4, 5, 0, 2), _merge(4, 5, 2, 4), _merge(4, 5, 4, 6), _merge(4, 5, 6, 8),
            # section header bars
            _merge(6, 7, 0, 8), _merge(12, 13, 0, 8), _merge(16, 17, 0, 8), _merge(19, 20, 0, 8),
            # gauges: label A:B, bar C:F, value G:H
            _merge(7, 8, 0, 2), _merge(7, 8, 2, 6), _merge(7, 8, 6, 8),
            _merge(8, 9, 0, 2), _merge(8, 9, 2, 6), _merge(8, 9, 6, 8),
            _merge(9, 10, 0, 2), _merge(9, 10, 2, 6), _merge(9, 10, 6, 8),
            _merge(10, 11, 0, 2), _merge(10, 11, 2, 6), _merge(10, 11, 6, 8),
            # schedule row: value B:C and E:F
            _merge(17, 18, 1, 3), _merge(17, 18, 4, 6),
            # performance + footer
            _merge(20, 21, 0, 8), _merge(21, 22, 0, 8), _merge(23, 24, 0, 8),
            _col(0, 150), _col(1, 120), _col(2, 80), _col(3, 95), _col(4, 95), _col(5, 95), _col(6, 110), _col(7, 115),
            _row(0, 42), _row(1, 24), _row(2, 8), _row(3, 22), _row(4, 34), _row(5, 8),
            _row(11, 8), _row(15, 8), _row(18, 22), _row(19, 8), _row(22, 8),
            {"updateBorders": {"range": cards, "top": thick, "bottom": thick, "left": thick, "right": thick,
                               "innerVertical": side}},
            {"updateBorders": {"range": gauges, "top": side, "bottom": side, "left": side, "right": side,
                               "innerHorizontal": side}},
            {"updateBorders": {"range": detail, "top": side, "bottom": side, "left": side, "right": side,
                               "innerHorizontal": side, "innerVertical": side}},
        ]
        _spreadsheet().batch_update({"requests": requests})
        ws.freeze(rows=2)
        return True
    except Exception as e:
        _log(f"dashboard EXC {e}")
        return False


def update_watchlist(rows: list) -> bool:
    """Overwrite the Watchlist tab body with the latest scan (header kept)."""
    if not enabled():
        _log("disabled, skip watchlist")
        return False
    try:
        ws = _spreadsheet().worksheet("Watchlist")
        ws.batch_clear(["A2:I100"])
        if rows:
            ws.update(rows, f"A2:I{1 + len(rows)}")
        return True
    except Exception as e:
        _log(f"watchlist EXC {e}")
        return False


def append_trade(row: list) -> bool:
    return _append("Trades", row)


def append_run(row: list) -> bool:
    return _append("Runs", row)


def append_shadow(row: list) -> bool:
    return _append("Shadow", row)


def _append(tab: str, row: list) -> bool:
    if not enabled():
        _log(f"disabled, skip {tab} append")
        return False
    try:
        ws = _spreadsheet().worksheet(tab)
        ws.append_row([str(c) for c in row], value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        _log(f"{tab} append EXC {e}")
        return False
