"""Reads your closed STOCK trades (NVDA, AMZN, TSLA, MSFT, META…) from eToro and rebuilds the P/L workbook.

Output: docs/data/Stocks_PL_tracker.xlsx, password-protected with the XLSX_PASSWORD secret
(the repo is public). Rewritten only when the trade list changes.
eToro's "profit" does NOT include the spread, so the workbook subtracts shares × spread per share.
The spread per share per stock is the engine's measured median (Settings sheet, editable).
"""
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

sys.path.insert(0, os.path.dirname(__file__))
from common import CFG, OUT, etoro_on, headers, instrument_id, load_json  # noqa: E402

XLSX = OUT / "Stocks_PL_tracker.xlsx"
HASH = OUT / "pl_tracker.hash"
TRADE_API = "https://public-api.etoro.com/api/v1/trading/info"


def _get(path, params=None):
    r = requests.get(f"{TRADE_API}{path}", params=params, timeout=20, headers=headers())
    if r.status_code != 200:
        raise RuntimeError(f"eToro {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def g(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def closed_stock_trades():
    ids = {}
    for s in CFG["stocks"]:
        try:
            ids[instrument_id(s)] = s
        except Exception as e:  # noqa: BLE001
            print(f"P/L: no id for {s}: {e}")
    rows, page = [], 1
    while page <= 50:
        js = _get("/trade/history", {"minDate": CFG.get("pl_history_from", "2026-10-15"), "page": page, "pageSize": 100})
        batch = js if isinstance(js, list) else g(js, "items", "trades", "data", "history", default=[])
        rows += batch
        if len(batch) < 100:
            break
        page += 1
    out = []
    for t in rows:
        iid = int(g(t, "instrumentId", "instrumentID", default=-1))
        if iid not in ids:
            continue
        out.append({
            "id": str(g(t, "positionId", "positionID")), "sym": ids[iid],
            "dir": "Long" if g(t, "isBuy", default=True) else "Short",
            "units": float(g(t, "units", default=0)), "lev": int(g(t, "leverage", default=1)),
            "inv": float(g(t, "initialInvestment", "investment", default=0)),
            "open_t": pd.Timestamp(g(t, "openTimestamp", "openDateTime")).tz_convert("UTC"),
            "close_t": pd.Timestamp(g(t, "closeTimestamp", "closeDateTime")).tz_convert("UTC"),
            "open": float(g(t, "openRate", default=0)), "close": float(g(t, "closeRate", default=0)),
            "sl": float(g(t, "stopLossRate", default=0) or 0), "tp": float(g(t, "takeProfitRate", default=0) or 0),
            "net": float(g(t, "netProfit", default=0)), "fees": float(g(t, "fees", default=0) or 0),
        })
    return sorted(out, key=lambda x: x["open_t"])


def engine_match(trades):
    """'Yes' = same stock and direction, opened within 20 minutes after a 🟢/🔴 signal."""
    sigs = [(s["sym"], pd.Timestamp(s["time_utc"]), "Long" if s["direction"] == "LONG" else "Short")
            for s in load_json(OUT / "journal.json", {"signals": []})["signals"]]
    for t in trades:
        t["engine"] = "Yes" if any(sym == t["sym"] and d == t["dir"] and
                                   timedelta(minutes=-2) <= t["open_t"] - st <= timedelta(minutes=20)
                                   for sym, st, d in sigs) else "No"


def spread_table():
    meas = load_json(OUT / "spreads.json", {})
    out = []
    for s in CFG["stocks"]:
        v = meas.get(s) or []
        if len(v) >= 20:
            out.append((s, round(float(pd.Series(v).median()), 4), f"measured median of {len(v)} live quotes"))
        else:
            out.append((s, CFG.get("default_spread_per_share", {}).get(s, 0.10), "default (not enough live quotes yet)"))
    return out


# ───────────────────────── workbook ─────────────────────────
F = "Arial"
BLUE, BLACK, BOLD = Font(name=F, size=10, color="0000FF"), Font(name=F, size=10), Font(name=F, size=10, bold=True)
SMALL = Font(name=F, size=9, color="666666")
HF, HFILL = Font(name=F, size=10, bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F2A44")
THIN = Border(bottom=Side(style="thin", color="D9D9D9"))
GF, RF = PatternFill("solid", fgColor="D9EAD3"), PatternFill("solid", fgColor="F4CCCC")
INPUT = PatternFill("solid", fgColor="FFF2CC")
USD = '$#,##0.00;[Red]($#,##0.00);"-"'
PRICE = "#,##0.00"


def header(ws, row, names, widths=None):
    for i, nm in enumerate(names, 1):
        c = ws.cell(row=row, column=i, value=nm)
        c.font, c.fill = HF, HFILL
        c.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
        if widths:
            ws.column_dimensions[c.column_letter].width = widths[i - 1]


def build(trades, spreads, stamp):
    wb = Workbook()
    sm = wb.active
    sm.title = "Summary"
    tr, dy, se, rm = (wb.create_sheet(n) for n in ("Trades", "Daily", "Settings", "How to use"))
    n = max(len(trades), 1)
    T = lambda c: f"Trades!${c}$2:${c}${n + 1}"  # noqa: E731

    # Settings: spread per share per stock (input, editable)
    header(se, 1, ["Stock", "Spread per share ($)", "Source"], [10, 18, 44])
    for i, (s, v, src) in enumerate(spreads, 2):
        se.cell(row=i, column=1, value=s).font = BLACK
        c = se.cell(row=i, column=2, value=v)
        c.font, c.fill, c.number_format = BLUE, INPUT, "$0.0000"
        se.cell(row=i, column=3, value=src).font = SMALL
    se.cell(row=len(spreads) + 3, column=1,
            value="Change a yellow cell if eToro's spread changes; every sheet recalculates.").font = SMALL
    SP = f"Settings!$A$2:$B${len(spreads) + 1}"

    cols = [("Position ID", 13), ("Stock", 8), ("Direction", 9), ("Shares", 9), ("Leverage", 8),
            ("Amount invested ($)", 12), ("Open time (UTC)", 17), ("Close time (UTC)", 17), ("Open time (Dubai)", 17),
            ("Date (Dubai)", 11), ("Minutes held", 9), ("Open price", 10), ("Close price", 10), ("Stop loss", 10),
            ("Take profit", 10), ("Price move ($/share)", 11), ("Gross P/L ($)", 11),
            ("eToro profit, before spread ($)", 13), ("Spread (estimated) ($)", 12), ("Fees shown by eToro ($)", 11),
            ("Net P/L after spread ($)", 13), ("Result", 8), ("Engine signal?", 9)]
    header(tr, 1, [c[0] for c in cols], [c[1] for c in cols])
    tr.row_dimensions[1].height = 32
    for i, t in enumerate(trades):
        x = i + 2
        vals = {1: t["id"], 2: t["sym"], 3: t["dir"], 4: t["units"], 5: t["lev"], 6: t["inv"],
                7: t["open_t"].tz_localize(None).to_pydatetime(), 8: t["close_t"].tz_localize(None).to_pydatetime(),
                12: t["open"], 13: t["close"], 14: t["sl"] or None, 15: t["tp"] or None,
                18: t["net"], 20: t["fees"], 23: t["engine"]}
        for col, v in vals.items():
            tr.cell(row=x, column=col, value=v).font = BLUE
        f = {9: f"=G{x}+4/24", 10: f"=INT(I{x})", 11: f"=ROUND((H{x}-G{x})*1440,1)",
             16: f'=(M{x}-L{x})*IF(C{x}="Short",-1,1)', 17: f"=P{x}*D{x}",
             19: f"=-D{x}*IFERROR(VLOOKUP(B{x},{SP},2,FALSE),0)", 21: f"=R{x}+S{x}",
             22: f'=IF(U{x}>0,"Win","Loss")'}
        for col, v in f.items():
            tr.cell(row=x, column=col, value=v).font = BLACK
        for col in range(1, len(cols) + 1):
            tr.cell(row=x, column=col).border = THIN
        for col in (7, 8, 9):
            tr.cell(row=x, column=col).number_format = "dd mmm yyyy hh:mm"
        tr.cell(row=x, column=10).number_format = "dd mmm yyyy"
        for col in (12, 13, 14, 15, 16):
            tr.cell(row=x, column=col).number_format = PRICE
        for col in (6, 17, 18, 19, 20, 21):
            tr.cell(row=x, column=col).number_format = USD
        tr.cell(row=x, column=4).number_format = "0.00"
    tr.freeze_panes = "D2"
    tr.auto_filter.ref = f"A1:W{n + 1}"
    for op, fill in (("greaterThan", GF), ("lessThan", RF)):
        tr.conditional_formatting.add(f"U2:U{n + 1}", CellIsRule(operator=op, formula=["0"], fill=fill))

    # Summary
    for col, w in zip("ABCDE", (34, 16, 18, 12, 52)):
        sm.column_dimensions[col].width = w
    sm["A1"] = "US stocks trading P/L (eToro)"
    sm["A1"].font = Font(name=F, size=14, bold=True)
    sm["A2"] = f"Read automatically from your eToro account. Last updated {stamp} (Dubai)."
    sm["A2"].font = SMALL
    items = [
        ("Trades", f'=COUNT({T("U")})', "0", ""),
        ("Gross P/L (price moves)", f'=SUM({T("Q")})', USD, "Shares × price move (= eToro's profit figure)"),
        ("Spread (estimated)", f'=SUM({T("S")})', USD, "Shares × spread per share (Settings). Charged separately."),
        ("Net P/L after spread", f'=SUM({T("U")})', USD, "Best estimate of what really reached your account"),
        ("Winning trades", f'=COUNTIF({T("V")},"Win")', "0", "After spread"),
        ("Losing trades", f'=COUNTIF({T("V")},"Loss")', "0", "After spread"),
        ("Win rate", "=IFERROR(B8/B4,0)", "0.0%", ""),
        ("Average win", f'=IFERROR(AVERAGEIF({T("U")},">0"),0)', USD, ""),
        ("Average loss", f'=IFERROR(AVERAGEIF({T("U")},"<0"),0)', USD, ""),
        ("Profit factor", f'=IFERROR(SUMIF({T("U")},">0")/-SUMIF({T("U")},"<0"),0)', "0.00", "Above 1.0 = profitable"),
        ("Spread as % of gross P/L", '=IF(B5>0,-B6/B5,"gross ≤ 0")', "0%", "How much of the profit the spread took"),
        ("Average minutes held", f'=IFERROR(AVERAGE({T("K")}),0)', "0.0", ""),
    ]
    header(sm, 3, ["Measure", "Value", "", "", "Note"])
    for i, (lab, f, fmt, note) in enumerate(items):
        r = 4 + i
        sm.cell(row=r, column=1, value=lab).font = BOLD if "Net" in lab else BLACK
        c = sm.cell(row=r, column=2, value=f)
        c.font, c.number_format = (BOLD if "Net" in lab else BLACK), fmt
        sm.cell(row=r, column=5, value=note).font = SMALL
        for col in range(1, 6):
            sm.cell(row=r, column=col).border = THIN
    for op, fill in (("greaterThan", GF), ("lessThan", RF)):
        sm.conditional_formatting.add("B7", CellIsRule(operator=op, formula=["0"], fill=fill))
    r0 = 4 + len(items) + 1
    header(sm, r0, ["By group", "Trades", "Net after spread ($)", "Win rate", ""])
    groups = [(f"{s}", "B", s) for s in CFG["stocks"]] + [
        ("Long trades", "C", "Long"), ("Short trades", "C", "Short"),
        ("Engine signals (Yes)", "W", "Yes"), ("Own trades (No)", "W", "No")]
    for i, (lab, col, val) in enumerate(groups):
        r = r0 + 1 + i
        sm.cell(row=r, column=1, value=lab).font = BLACK
        sm.cell(row=r, column=2, value=f'=COUNTIFS({T(col)},"{val}")').number_format = "0"
        sm.cell(row=r, column=3, value=f'=SUMIFS({T("U")},{T(col)},"{val}")').number_format = USD
        sm.cell(row=r, column=4, value=f'=IFERROR(COUNTIFS({T(col)},"{val}",{T("V")},"Win")/B{r},0)').number_format = "0%"
        for c in range(1, 6):
            sm.cell(row=r, column=c).border = THIN
    rng = f"C{r0 + 1}:C{r0 + len(groups)}"
    for op, fill in (("greaterThan", GF), ("lessThan", RF)):
        sm.conditional_formatting.add(rng, CellIsRule(operator=op, formula=["0"], fill=fill))
    sm.freeze_panes = "A4"

    # Daily
    header(dy, 1, ["Date (Dubai)", "Trades", "Gross P/L ($)", "Spread est. ($)", "Net after spread ($)", "Running net ($)"],
           [16, 8, 13, 13, 15, 14])
    if trades:
        first = (trades[0]["open_t"] + pd.Timedelta(hours=4)).tz_localize(None).normalize()
        last = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=4)).tz_localize(None).normalize()
        days = pd.date_range(first, last, freq="D")
        for i, d in enumerate(days):
            x = i + 2
            dy.cell(row=x, column=1, value=d.to_pydatetime()).number_format = "ddd dd mmm yyyy"
            dy.cell(row=x, column=2, value=f'=COUNTIFS({T("J")},A{x})')
            for col, s in ((3, "Q"), (4, "S"), (5, "U")):
                dy.cell(row=x, column=col, value=f'=SUMIFS({T(s)},{T("J")},A{x})').number_format = USD
            dy.cell(row=x, column=6, value=f"=E{x}" if i == 0 else f"=F{x - 1}+E{x}").number_format = USD
            for col in range(1, 7):
                dy.cell(row=x, column=col).border = THIN
        for op, fill in (("greaterThan", GF), ("lessThan", RF)):
            dy.conditional_formatting.add(f"E2:E{len(days) + 1}", CellIsRule(operator=op, formula=["0"], fill=fill))
    dy.freeze_panes = "A2"

    rm.column_dimensions["A"].width = 24
    rm.column_dimensions["B"].width = 100
    info = [("What this is", "Every closed trade in your watched stocks on your eToro account, with the true result after costs."),
            ("Updates", "Rebuilt by the stock agent whenever a trade closes. Download it again from the website."),
            ("Spread", "eToro's profit figure does NOT include the spread. The workbook subtracts shares × spread per share "
                       "from the Settings sheet (measured live by the agent)."),
            ("Engine signal?", "Yes = same stock and direction, opened within 20 minutes after one of your 🟢/🔴 alerts."),
            ("Times", "eToro uses UTC. 'Open time (Dubai)' adds 4 hours; daily totals use the Dubai date."),
            ("Demo vs real", "eToro's trade history API returns your REAL account trades. Demo results are on the dashboard (in R)."),
            ("Privacy", "Password-protected because the website is public.")]
    for i, (k, v) in enumerate(info, 1):
        rm.cell(row=i, column=1, value=k).font = BOLD
        c = rm.cell(row=i, column=2, value=v)
        c.font, c.alignment = BLACK, Alignment(wrap_text=True, vertical="top")
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def encrypt(data, password):
    import msoffcrypto
    src, dst = io.BytesIO(data), io.BytesIO()
    msoffcrypto.OfficeFile(src).encrypt(password, dst)
    return dst.getvalue()


def main():
    if not etoro_on():
        print("P/L tracker skipped: eToro keys not set")
        return
    pw = os.getenv("XLSX_PASSWORD", "").strip()
    if not pw:
        print("P/L tracker skipped: XLSX_PASSWORD secret not set (needed because the repo is public)")
        return
    try:
        trades = closed_stock_trades()
    except Exception as e:  # noqa: BLE001
        print(f"P/L tracker: could not read eToro trades: {e}")
        return
    engine_match(trades)
    spreads = spread_table()
    key = hashlib.sha256(json.dumps(["v1", [(s, round(v, 2)) for s, v, _ in spreads]] +
                                    [[t["id"], t["net"], t["engine"]] for t in trades]).encode()).hexdigest()
    if HASH.exists() and HASH.read_text().strip() == key and XLSX.exists():
        print(f"P/L tracker: {len(trades)} stock trades, no change")
        return
    stamp = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%a %d %b %Y, %H:%M")
    XLSX.write_bytes(encrypt(build(trades, spreads, stamp), pw))
    HASH.write_text(key)
    print(f"P/L tracker updated: {len(trades)} stock trades")


if __name__ == "__main__":
    main()
