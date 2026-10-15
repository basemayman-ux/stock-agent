"""Backtest: replays the live rules on the last ~60 days of real 5-minute data (Yahoo, free).

Same code as the live engine (strategy.prepare / score / plan and the journal exit rule),
with the spread per share from config (or the measured median once the agent has collected it).
Not covered: big US news days (no free history), so real results may differ a bit.
Results in R only (safe for the public log) -> docs/data/backtest.json, shown in the run log.
"""
import json
import os
import sys


import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import agent  # noqa: E402
import news  # noqa: E402
import strategy  # noqa: E402
from common import CFG, ET, OUT, load_json, market_status, save_json  # noqa: E402


def load_yahoo(sym):
    import yfinance as yf
    t = yf.Ticker(sym)
    m5 = t.history(period="60d", interval="5m", prepost=False, auto_adjust=False)
    h1 = t.history(period="730d", interval="60m", prepost=False, auto_adjust=False)
    out = []
    for df in (m5, h1):
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
        df.index = df.index.tz_convert("UTC")
        out.append(df)
    return out


def spread_for(sym):
    meas = load_json(OUT / "spreads.json", {}).get(sym) or []
    if len(meas) >= 20:
        return float(pd.Series(meas).median())
    return CFG.get("default_spread_per_share", {}).get(sym, 0.10)


def run(sym, m5, h1, earn):
    sp = spread_for(sym)
    journal = {"signals": []}
    times = m5.index[m5.index > m5.index[0] + pd.Timedelta(days=7)]
    for ts in times:
        now = (ts + pd.Timedelta(minutes=5, seconds=30)).to_pydatetime()
        mkt = market_status(now)
        flatten = (not mkt.get("open")) or mkt.get("to_close", 999) <= CFG["flatten_minutes_before_close"]
        hist5 = m5[m5.index <= ts].iloc[-700:]
        agent.update_journal(journal, sym, hist5, now, flatten)
        if not mkt.get("open") or mkt["since_open"] < CFG["skip_first_minutes"] or \
                mkt["to_close"] < CFG["skip_last_minutes"]:
            continue
        if agent.daily_lock(journal, sym, now):
            continue
        if news.earnings_block(sym, earn, now)[0]:
            continue
        ctx = strategy.prepare(hist5, h1[h1.index <= ts], now)
        if ctx is None:
            continue
        if agent.gap_block(ctx, now)[0]:
            continue
        best = max((strategy.score(ctx, d) + (d,) for d in ("LONG", "SHORT")), key=lambda x: x[0])
        sc, _, notes, d = best
        if sc < CFG["min_trade_score"]:
            continue
        last = float(ctx["df"]["close"].iloc[-1])
        if sp / last * 100 > CFG["max_spread_pct"]:
            continue
        pl, why = strategy.plan(ctx, d, last - sp / 2, last + sp / 2)
        if why:
            continue
        journal["signals"].append({"id": f"{sym}-{ts:%Y%m%d%H%M}", "sym": sym, "day": now.astimezone(ET).date().isoformat(),
                                   "time_utc": now.isoformat(), "bar": ts.isoformat(), "direction": d, "score": sc,
                                   "status": "open", "be": False,
                                   **{k: pl[k] for k in ("entry", "sl", "tp", "be_px", "R", "cost_r", "spread")}})
    return [s for s in journal["signals"] if s["status"] == "closed"]


def summary(sigs):
    n = len(sigs)
    if not n:
        return {"trades": 0}
    w = sum(1 for s in sigs if s["r"] > 0)
    net, gross = sum(s["r"] for s in sigs), sum(s["r_gross"] for s in sigs)
    pos, neg = sum(s["r"] for s in sigs if s["r"] > 0), -sum(s["r"] for s in sigs if s["r"] < 0)
    eq, peak, dd = 0, 0, 0
    for s in sigs:
        eq += s["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    res = pd.Series([s["result"] for s in sigs]).value_counts().to_dict()
    return {"trades": n, "win_rate": round(w / n * 100), "net_r": round(net, 2), "gross_r": round(gross, 2),
            "avg_r": round(net / n, 3), "profit_factor": round(pos / neg, 2) if neg else None,
            "max_drawdown_r": round(dd, 2), "spread_cost_r": round(gross - net, 2), "exits": res,
            "longs": sum(1 for s in sigs if s["direction"] == "LONG"),
            "shorts": sum(1 for s in sigs if s["direction"] == "SHORT")}


def main():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    earn = news.earnings_dates(CFG["stocks"], now)
    allsig, per = [], {}
    for sym in CFG["stocks"]:
        try:
            m5, h1 = load_yahoo(sym)
            sigs = run(sym, m5, h1, earn.get(sym, []))
            per[sym] = summary(sigs)
            per[sym]["days"] = int(len(set(m5.index.tz_convert(ET).date)))
            allsig += sigs
            print(f"{sym}: {per[sym]}")
        except Exception as e:  # noqa: BLE001
            per[sym] = {"error": str(e)[:200]}
            print(f"{sym}: FAILED {e}")
    allsig.sort(key=lambda s: s["time_utc"])
    tot = summary(allsig)
    out = {"run_utc": now.isoformat(), "total": tot, "per_stock": per,
           "rules": {k: CFG[k] for k in ("min_trade_score", "target_r", "be_at_r", "require_htf_trend", "allow_short")},
           "trades": [{k: s[k] for k in ("sym", "time_utc", "direction", "score", "result", "r")} for s in allsig]}
    save_json(OUT / "backtest.json", out)
    print("\n=== BACKTEST (last ~60 days, after spread, in R) ===")
    print(json.dumps(tot, indent=1))
    print("Note: news days are not filtered in the backtest (no free history). Positive net R and "
          "profit factor > 1.2 over 40+ trades would be encouraging; fewer trades = not proven yet.")


if __name__ == "__main__":
    main()
