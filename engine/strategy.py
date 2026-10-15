"""Setup scoring for one stock on 5-minute bars, with the 1-hour trend as the filter.

Score 0–100 (same idea as the gold agent):
  1-hour trend agrees 20 · structure 15 · liquidity sweep 15 · momentum 15 · VWAP 15
  · volume 10 · volatility 5 · session 5
News, earnings, spread, gaps and hours are hard blocks (handled in agent.py), not points.
The last closed bar must close in the trade's direction, otherwise the score is capped at 79
(can be WATCH, never a trade).
"""
from datetime import time as dtime

import numpy as np
import pandas as pd

from common import CFG, ET
from indicators import add_indicators, ema

W = {"trend": 20, "structure": 15, "sweep": 15, "momentum": 15, "vwap": 15, "volume": 10,
     "volatility": 5, "session": 5}


def regular_hours(df):
    """Keep only bars inside 09:30–16:00 New York time (drops pre/after-market)."""
    t = df.index.tz_convert(ET)
    mins = t.hour * 60 + t.minute
    return df[(mins >= 570) & (mins < 960)]


def closed_bars(df, minutes, now):
    return df[df.index + pd.Timedelta(minutes=minutes) <= now]


def session_vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].fillna(0)
    has_vol = vol.sum() > 0
    w = vol if has_vol else pd.Series(1.0, index=df.index)
    day = df.index.tz_convert(ET).date
    pv = (tp * w).groupby(day).cumsum()
    ww = w.groupby(day).cumsum().replace(0, np.nan)
    return (pv / ww).fillna(tp), has_vol


def htf_trend(h1):
    """1-hour trend: UP / DOWN / NEUTRAL from EMA20/50 and the EMA50 slope (regular hours only)."""
    h = regular_hours(h1)
    if len(h) < 60:
        return "NEUTRAL", {}
    c = h["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    slope = e50.iloc[-1] - e50.iloc[-6]
    info = {"ema20": round(e20.iloc[-1], 2), "ema50": round(e50.iloc[-1], 2)}
    if c.iloc[-1] > e50.iloc[-1] and e20.iloc[-1] > e50.iloc[-1] and slope > 0:
        return "UP", info
    if c.iloc[-1] < e50.iloc[-1] and e20.iloc[-1] < e50.iloc[-1] and slope < 0:
        return "DOWN", info
    return "NEUTRAL", info


def swings(df, k=2):
    """Fractal swing highs/lows (k bars each side). Returns lists of (timestamp, price)."""
    hs, ls = [], []
    h, lo = df["high"].values, df["low"].values
    for i in range(k, len(df) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            hs.append((df.index[i], h[i]))
        if lo[i] == min(lo[i - k:i + k + 1]):
            ls.append((df.index[i], lo[i]))
    return hs, ls


def prepare(m5, h1, now):
    """Builds the context shared by the LONG and SHORT scorers. Returns None if not enough data."""
    m5 = closed_bars(regular_hours(m5), 5, now)
    h1 = closed_bars(h1, 60, now)
    if len(m5) < 120:
        return None
    df = add_indicators(m5)
    df["vwap"], has_vol = session_vwap(df)
    if has_vol:
        med = df["volume"].rolling(78, min_periods=20).median().replace(0, np.nan)   # ~1 session
        df["rvol"] = df["volume"] / med
    today = now.astimezone(ET).date()
    days = df.index.tz_convert(ET).date
    tdy, prev = df[days == today], df[days < today]
    trend, tinfo = htf_trend(h1)
    prev_day = prev[prev.index.tz_convert(ET).date == prev.index.tz_convert(ET).date[-1]] if len(prev) else prev
    return {"df": df, "today": tdy, "prev_day": prev_day, "trend": trend, "trend_info": tinfo,
            "has_vol": has_vol, "now": now, "last_bar": df.index[-1]}


def score(ctx, d):
    """Score one direction. Returns (score, parts dict, notes list)."""
    df, tdy = ctx["df"], ctx["today"]
    s = 1 if d == "LONG" else -1
    last = df.iloc[-1]
    atr = float(last["atr14"])
    parts, notes = {k: 0 for k in W}, []

    # 1) higher-timeframe trend
    want = "UP" if d == "LONG" else "DOWN"
    if ctx["trend"] == want:
        parts["trend"] = W["trend"]
    notes.append(f"1h trend {ctx['trend']}")

    # 2) structure: higher low (lower high) + break of the latest swing
    look = df.iloc[-40:]
    hs, ls = swings(look.iloc[:-1])
    if d == "LONG" and len(ls) >= 2 and ls[-1][1] > ls[-2][1]:
        parts["structure"] += 8
    if d == "SHORT" and len(hs) >= 2 and hs[-1][1] < hs[-2][1]:
        parts["structure"] += 8
    recent = df.iloc[-6:]
    if d == "LONG" and hs and (recent["close"] > hs[-1][1]).any():
        parts["structure"] += 7
        notes.append("broke the last swing high")
    if d == "SHORT" and ls and (recent["close"] < ls[-1][1]).any():
        parts["structure"] += 7
        notes.append("broke the last swing low")

    # 3) liquidity sweep: a wick through an obvious level that closed back inside
    levels = []
    older = df.iloc[-40:-6]
    if len(older):
        levels.append(older["low"].min() if d == "LONG" else older["high"].max())
    if len(ctx["prev_day"]):
        levels.append(ctx["prev_day"]["low"].min() if d == "LONG" else ctx["prev_day"]["high"].max())
    if len(tdy) >= 3:
        orng = tdy.iloc[:3]                         # opening range = first 15 minutes
        levels.append(orng["low"].min() if d == "LONG" else orng["high"].max())
    swept = None
    for lv in levels:
        for _, b in recent.iterrows():
            if d == "LONG" and b["low"] < lv < b["close"] and last["close"] > lv:
                swept = lv
            if d == "SHORT" and b["high"] > lv > b["close"] and last["close"] < lv:
                swept = lv
    if swept is not None:
        parts["sweep"] = W["sweep"]
        notes.append(f"swept {swept:.2f} and reclaimed it")

    # 4) momentum
    r7, r7b = float(last["rsi7"]), float(df["rsi7"].iloc[-4])
    if (d == "LONG" and r7 > 50 and r7 > r7b) or (d == "SHORT" and r7 < 50 and r7 < r7b):
        parts["momentum"] += 8
    if s * (last["close"] - last["ema20"]) > 0 and s * (last["ema20"] - last["ema50"]) > 0:
        parts["momentum"] += 7

    # 5) VWAP: on the right side, and pulled back to it recently (not chasing)
    dist = s * (last["close"] - last["vwap"])
    if dist > 0:
        parts["vwap"] += 8
        near = (df["low"].iloc[-12:] <= df["vwap"].iloc[-12:] + 0.25 * atr) if d == "LONG" else \
               (df["high"].iloc[-12:] >= df["vwap"].iloc[-12:] - 0.25 * atr)
        if near.any():
            parts["vwap"] += 7
    if dist > 2.5 * atr:
        parts["vwap"] = 0
        notes.append("too far from VWAP (chasing)")

    # 6) volume on the trigger bar
    if ctx["has_vol"] and not np.isnan(last.get("rvol", np.nan)):
        rv = float(last["rvol"])
        parts["volume"] = 10 if rv >= 1.5 else 6 if rv >= 1.1 else 0
        notes.append(f"volume {rv:.1f}× normal")
    else:
        parts["volume"] = 5

    # 7) volatility: trending and a sensible bar range
    atr_pct = atr / last["close"] * 100
    if last["adx14"] >= 18 and 0.08 <= atr_pct <= 1.2:
        parts["volatility"] = W["volatility"]

    # 8) session: best liquidity away from the lunch lull
    t = ctx["now"].astimezone(ET).time()
    parts["session"] = 2 if dtime(11, 30) <= t < dtime(13, 30) else W["session"]

    total = int(sum(parts.values()))
    if s * (last["close"] - last["open"]) <= 0:
        total = min(total, 79)
        notes.append("last bar not in trade direction")
    if CFG.get("require_htf_trend", True) and parts["trend"] == 0:
        total = min(total, 79)
    if d == "SHORT" and not CFG.get("allow_short", True):
        total = min(total, 0)
    return total, parts, notes


def plan(ctx, d, bid, ask):
    """Entry / stop / 1R target, or (None, reason) if the trade isn't worth its costs."""
    df = ctx["df"]
    s = 1 if d == "LONG" else -1
    atr = float(df["atr14"].iloc[-1])
    entry = ask if d == "LONG" else bid
    spread = ask - bid
    swing = df["low"].iloc[-6:].min() if d == "LONG" else df["high"].iloc[-6:].max()
    sl = swing - s * 0.1 * atr
    min_r = max(CFG["min_stop_pct"] / 100 * entry, CFG["min_stop_spread_mult"] * spread, CFG["min_stop_atr"] * atr)
    r = s * (entry - sl)
    if r < min_r:
        r = min_r
        sl = entry - s * r
    if r > CFG["max_stop_atr"] * atr:
        return None, f"stop too wide ({r / atr:.1f} ATR)"
    cost_share = spread / r if r else 1
    if cost_share > CFG["max_cost_share_of_r"]:
        return None, f"spread is {cost_share:.0%} of the risk"
    tr, be = CFG.get("target_r", 1.0), CFG.get("be_at_r", 0.7)
    return {"entry": round(entry, 2), "sl": round(sl, 2), "tp": round(entry + s * tr * r, 2),
            "be_px": round(entry + s * be * r, 2), "R": round(r, 4), "spread": round(spread, 4),
            "cost_r": round(cost_share, 3), "atr": round(atr, 3)}, None
