"""US stocks signal engine (runs every 5 minutes on GitHub Actions).

For each stock in config.json: eToro 5-minute + 1-hour candles and live bid/ask → blocks
(hours, news, earnings, gap, spread, daily limits) → setup score for LONG and SHORT →
🟢/🔴 SIGNAL (≥80), 👀 WATCH (≥75, do not trade) or nothing.
Writes docs/data/signal.json for the dashboard and docs/data/journal.json (paper results).
Signals are also handed to autotrade.py (eToro demo by default).

Public repo: logs and JSON never contain balances, dollar P/L or position sizes.
"""
import math
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import news  # noqa: E402
import strategy  # noqa: E402
from common import (CFG, DUBAI, ET, OUT, SITE, candles, etoro_on, hm, load_json, market_status,  # noqa: E402
                    notify, quotes, save_json)

JOURNAL = OUT / "journal.json"
SIGNAL = OUT / "signal.json"
SPREADS = OUT / "spreads.json"
STATE = OUT / "alerts_state.json"


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def size_shares(plan, risk_pct, leverage=None):
    """Shares to trade so that hitting the stop loses risk_pct of equity (capped by exposure)."""
    try:
        eq = float(os.getenv("ACCOUNT_EQUITY", "0").replace(",", ""))
    except ValueError:
        eq = 0
    if eq <= 0 or not plan:
        return 0.0
    sh = eq * risk_pct / 100 / plan["R"]
    cap_pct = CFG.get("autotrade", {}).get("max_position_pct_of_equity", 100)
    sh = min(sh, eq * cap_pct / 100 / plan["entry"])
    return math.floor(sh * 100) / 100


# ───────────────────────── paper journal ─────────────────────────
def update_journal(journal, sym, m5, now, flatten):
    """Follows every open paper signal bar by bar with the live exit rule:
    stop → entry once 0.7R is reached, all out at 1R, close at market before the bell."""
    for s in journal["signals"]:
        if s["sym"] != sym or s["status"] != "open":
            continue
        sign = 1 if s["direction"] == "LONG" else -1
        bars = strategy.closed_bars(m5, 5, now)
        bars = bars[bars.index > pd.Timestamp(s["bar"])]
        done = s.get("checked")                       # each bar is checked exactly once
        new_bars = bars[bars.index > pd.Timestamp(done)] if done else bars
        stop = s["entry"] if s.get("be") else s["sl"]
        for ts, b in new_bars.iterrows():
            s["checked"] = ts.isoformat()
            hi, lo = (b["high"], b["low"]) if sign > 0 else (-b["low"], -b["high"])
            st_, tp, be = sign * stop, sign * s["tp"], sign * s["be_px"]
            if lo <= st_:                                       # stop first if both in one bar
                s.update(status="closed", exit=stop, closed=ts.isoformat(),
                         result="breakeven" if s.get("be") else "stop")
                break
            if hi >= tp:
                s.update(status="closed", exit=s["tp"], closed=ts.isoformat(), result="target")
                break
            if not s.get("be") and hi >= be:
                s["be"] = True
                stop = s["entry"]
        if s["status"] == "open" and flatten and len(bars):
            s.update(status="closed", exit=float(bars["close"].iloc[-1]), closed=now.isoformat(), result="end of day")
        if s["status"] == "closed":
            gross = sign * (s["exit"] - s["entry"]) / s["R"]
            s["r_gross"] = round(gross, 3)
            s["r"] = round(gross - s.get("cost_r", 0), 3)          # after spread


def stats(journal, days=None, now=None):
    sigs = [s for s in journal["signals"] if s["status"] == "closed"]
    if days:
        cut = (now - timedelta(days=days)).isoformat()
        sigs = [s for s in sigs if s["time_utc"] >= cut]
    def block(lst):
        n = len(lst)
        w = sum(1 for s in lst if s["r"] > 0)
        tot = sum(s["r"] for s in lst)
        return {"trades": n, "wins": w, "win_rate": round(w / n * 100) if n else None,
                "net_r": round(tot, 2), "avg_r": round(tot / n, 3) if n else None}
    out = {"all": block(sigs)}
    for sym in CFG["stocks"]:
        out[sym] = block([s for s in sigs if s["sym"] == sym])
    return out


def day_key(now):
    return now.astimezone(ET).date().isoformat()


def daily_lock(journal, sym, now):
    today = [s for s in journal["signals"] if s["day"] == day_key(now)]
    lost = -sum(s.get("r", 0) for s in today if s.get("r", 0) < 0)
    if len(today) >= CFG["max_signals_per_day"]:
        return f"{len(today)} signals today (daily max)"
    if lost >= CFG["daily_loss_stop_r"]:
        return f"daily loss stop ({lost:.1f}R lost today)"
    mine = [s for s in today if s["sym"] == sym]
    if len(mine) >= CFG["max_signals_per_stock_per_day"]:
        return "already had a signal today"
    if any(s["status"] == "open" for s in journal["signals"] if s["sym"] == sym):
        return "signal still open"
    return None


# ───────────────────────── one stock ─────────────────────────
def gap_block(ctx, now):
    tdy, prev = ctx["today"], ctx["prev_day"]
    if not len(tdy) or not len(prev):
        return None, None
    gap = (tdy["open"].iloc[0] / prev["close"].iloc[-1] - 1) * 100
    until = datetime.combine(now.astimezone(ET).date(), hm(CFG.get("gap_block_until_et", "10:30")), ET)
    if abs(gap) >= CFG.get("gap_block_pct", 3.0) and now.astimezone(ET) < until:
        return f"opened with a {gap:+.1f}% gap; waiting until {until:%H:%M} ET", gap
    return None, gap


def analyze_stock(sym, now, mkt, q, journal, earn_dates, macro_reason, state):
    card = {"sym": sym, "status": "NO TRADE", "blocks": []}
    m5 = candles(sym, "FiveMinutes")
    h1 = candles(sym, "OneHour")
    flatten = mkt.get("open") is False or mkt.get("to_close", 999) <= CFG["flatten_minutes_before_close"]
    update_journal(journal, sym, m5, now, flatten and mkt.get("phase") != "pre-market")
    ctx = strategy.prepare(m5, h1, now)
    if ctx is None:
        card["blocks"].append("not enough price history")
        return card, None
    df = ctx["df"]
    last = df.iloc[-1]
    bid, ask = q if q else (float(last["close"]), float(last["close"]))
    card.update(price=r2((bid + ask) / 2), bid=r2(bid), ask=r2(ask), spread=r2(ask - bid, 3),
                spread_pct=r2((ask - bid) / ask * 100, 3), trend=ctx["trend"], vwap=r2(last["vwap"]),
                rsi7=r2(last["rsi7"], 0), atr=r2(last["atr14"], 3),
                day_change=r2((last["close"] / ctx["prev_day"]["close"].iloc[-1] - 1) * 100) if len(ctx["prev_day"]) else None,
                bar_utc=ctx["last_bar"].isoformat(),
                spark=[r2(x) for x in df["close"].iloc[-78:].tolist()])

    # hard blocks
    b = card["blocks"]
    if not mkt.get("open"):
        b.append(f"market {mkt['phase']}")
    else:
        if mkt["since_open"] < CFG["skip_first_minutes"]:
            b.append(f"first {CFG['skip_first_minutes']} min of the session")
        if mkt["to_close"] < CFG["skip_last_minutes"]:
            b.append(f"last {CFG['skip_last_minutes']} min of the session")
        age = (now - (ctx["last_bar"] + pd.Timedelta(minutes=5))).total_seconds() / 60
        if age > 15:
            b.append(f"price data {age:.0f} min old")
    if macro_reason:
        b.append(macro_reason)
    er, nxt = news.earnings_block(sym, earn_dates.get(sym, []), now)
    card["next_earnings"] = nxt
    if er:
        b.append(er)
    g, gap = gap_block(ctx, now)
    card["gap_pct"] = r2(gap)
    if g:
        b.append(g)
    if q and (ask - bid) / ask * 100 > CFG["max_spread_pct"]:
        b.append(f"spread too wide ({(ask - bid) / ask * 100:.2f}%)")
    lock = daily_lock(journal, sym, now)
    if lock:
        b.append(lock)
    if not q:
        b.append("no live eToro price")

    # score both directions
    best = None
    for d in ("LONG", "SHORT"):
        sc, parts, notes = strategy.score(ctx, d)
        card[d.lower()] = {"score": sc, "parts": parts, "notes": notes}
        if best is None or sc > best[1]:
            best = (d, sc, parts, notes)
    d, sc, parts, notes = best
    card.update(direction=d, score=sc)
    pl, why = strategy.plan(ctx, d, bid, ask)
    if why:
        card["plan_issue"] = why
    if sc >= CFG["min_trade_score"] and not b and pl:
        card["status"] = "SIGNAL"
        card["plan"] = pl
        return card, (d, sc, pl, notes, ctx)
    if sc >= CFG["watch_min_score"] and mkt.get("open"):
        card["status"] = "WATCH"
        key = f"watch_{sym}_{d}"
        lastw = state.get(key)
        temporary = all(x == macro_reason for x in b)      # only a news pause, the stock is otherwise tradable
        if temporary and (not lastw or (now - pd.Timestamp(lastw)).total_seconds() / 60 >= CFG["watch_cooldown_minutes"]):
            state[key] = now.isoformat()
            why_not = "; ".join(b) or (why or f"score {sc} below {CFG['min_trade_score']}")
            notify(f"👀 WATCH {sym} {d} (score {sc}) — DO NOT TRADE\n"
                   f"Price {card['price']} · 1h trend {ctx['trend']}\nNot a trade because: {why_not}",
                   kind="watch")
    return card, None


def fire_signal(sym, card, sig, now, journal):
    d, sc, pl, notes, ctx = sig
    rec = {"id": f"{sym}-{now:%Y%m%d%H%M}", "sym": sym, "day": day_key(now), "time_utc": now.isoformat(),
           "bar": ctx["last_bar"].isoformat(), "direction": d, "score": sc, "status": "open", "be": False,
           **{k: pl[k] for k in ("entry", "sl", "tp", "be_px", "R", "cost_r", "spread")}}
    journal["signals"].append(rec)
    shares = size_shares(pl, CFG["risk_pct"])
    dubai = now.astimezone(DUBAI)
    icon = "🟢" if d == "LONG" else "🔴"
    verb = "BUY" if d == "LONG" else "SELL (short)"
    notify(f"{icon} {verb} {sym} at {pl['entry']} — score {sc}\n"
           f"Stop {pl['sl']} · Target {pl['tp']} (1R) · move stop to entry at {pl['be_px']} (0.7R)\n"
           f"Size: {shares:g} shares ({CFG['risk_pct']}% risk)" + ("" if shares else " — set ACCOUNT_EQUITY") +
           f"\nSpread {pl['spread']:.2f} = {pl['cost_r']:.0%} of risk · {dubai:%H:%M} Dubai\n"
           f"Why: {'; '.join(notes[:4])}\nValid ~10 min. Close before the US close if still open.", kind="signal")
    return rec


def record_spreads(qs, mkt, now):
    if not mkt.get("open"):
        return
    sp = load_json(SPREADS, {})
    for sym, (bid, ask) in qs.items():
        lst = sp.setdefault(sym, [])
        lst.append(round(ask - bid, 4))
        sp[sym] = lst[-500:]
    save_json(SPREADS, sp)


def spread_summary():
    sp = load_json(SPREADS, {})
    return {s: {"median": round(float(pd.Series(v).median()), 4), "samples": len(v)} for s, v in sp.items() if v}


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if os.getenv("FAKE_NOW"):                       # tests only
        now = pd.Timestamp(os.getenv("FAKE_NOW")).to_pydatetime()
    mkt = market_status(now)
    journal = load_json(JOURNAL, {"signals": []})
    state = load_json(STATE, {})
    prev = load_json(SIGNAL, {})
    out = {"updated_utc": now.isoformat(), "updated_dubai": now.astimezone(DUBAI).strftime("%a %d %b, %H:%M"),
           "market": {"phase": mkt["phase"], "open": mkt.get("open", False)}, "stocks": [], "errors": []}
    if "close_et" in mkt:
        out["market"]["session_dubai"] = (f"{mkt['open_et'].astimezone(DUBAI):%H:%M}–"
                                          f"{mkt['close_et'].astimezone(DUBAI):%H:%M}")
    if not etoro_on():
        out["errors"].append("eToro keys not set (ETORO_API_KEY / ETORO_USER_KEY secrets)")
        save_json(SIGNAL, out)
        print(out["errors"][0])
        return

    open_paper = any(s["status"] == "open" for s in journal["signals"])
    at = None
    try:
        import autotrade as at
    except Exception as e:  # noqa: BLE001
        print(f"autotrade unavailable: {e}")
    open_auto = at.has_open() if at else False
    quiet = not mkt.get("open") and not open_paper and not open_auto
    if quiet and prev.get("stocks") and mkt["phase"] != "pre-market":
        # market shut and nothing to manage: keep the last cards, refresh the clock only
        prev.update(updated_utc=out["updated_utc"], updated_dubai=out["updated_dubai"], market=out["market"])
        save_json(SIGNAL, prev)
        print(f"Market {mkt['phase']}: nothing to do.")
        return

    try:
        qs = quotes(CFG["stocks"])
    except Exception as e:  # noqa: BLE001
        qs = {}
        out["errors"].append(f"live prices: {e}")
    record_spreads(qs, mkt, now)
    earn = news.earnings_dates(CFG["stocks"], now)
    macro = news.macro_block(now)
    fired = []
    for sym in CFG["stocks"]:
        try:
            card, sig = analyze_stock(sym, now, mkt, qs.get(sym), journal, earn, macro, state)
            if sig:
                rec = fire_signal(sym, card, sig, now, journal)
                fired.append((rec, sig[2]))
            out["stocks"].append(card)
            print(f"{sym}: {card['status']} {card.get('direction', '')} score {card.get('score')} "
                  f"blocks={card['blocks']}")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            out["stocks"].append({"sym": sym, "status": "ERROR", "blocks": [str(e)[:200]]})
            out["errors"].append(f"{sym}: {str(e)[:200]}")

    if at:
        try:
            for rec, pl in fired:
                at.on_signal(rec, pl, now)
            at.manage(journal, now, mkt, qs)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            notify(f"⚠️ Auto-trade error: {e}. Check eToro.", kind="update")
        out["autotrade"] = at.public_summary(now)

    out["stats"] = {"all_time": stats(journal), "last_14_days": stats(journal, 14, now)}
    out["recent"] = [{k: s.get(k) for k in ("sym", "time_utc", "direction", "score", "entry", "sl", "tp",
                                             "status", "result", "r")} for s in journal["signals"][-15:]][::-1]
    out["news"] = {"macro_now": macro, "upcoming": news.upcoming_macro(now)}
    out["spreads"] = spread_summary()
    out["config"] = {k: CFG[k] for k in ("min_trade_score", "watch_min_score", "risk_pct")}
    out["config"]["autotrade_mode"] = at.cfg()["mode"] if at else "off"
    journal["signals"] = journal["signals"][-1000:]
    save_json(JOURNAL, journal)
    save_json(STATE, state)
    save_json(SIGNAL, out)
    if os.getenv("TEST_ALERT", "").lower() == "true":
        notify(f"✅ Stock agent test alert — {len(out['stocks'])} stocks checked. Dashboard: {SITE}", kind="update")


if __name__ == "__main__":
    main()
