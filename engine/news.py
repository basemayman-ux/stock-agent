"""News filter: big US macro releases (Forex Factory calendar) and company earnings dates.

Earnings come from Yahoo (yfinance), cached in docs/data/earnings.json and refreshed once a day.
You can always pin dates by hand in config.json "earnings_dates": {"NVDA": ["2026-11-19"]}.
Rule: no new trades the US trading day BEFORE earnings or ON the earnings day.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from common import CFG, ET, OUT, hm, load_json, save_json, session_times

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL = OUT / "calendar.json"
EARN = OUT / "earnings.json"


# ───────────── macro ─────────────
def macro_events(now):
    """High-impact USD events this week (cached; refreshed at most every 6 hours)."""
    cache = load_json(CAL, {})
    fresh = cache.get("fetched") and (now - pd.Timestamp(cache["fetched"])).total_seconds() < 6 * 3600
    if not fresh:
        try:
            js = requests.get(FF_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).json()
            ev = [{"title": e.get("title", ""), "time": pd.Timestamp(e["date"]).tz_convert("UTC").isoformat()}
                  for e in js if e.get("country") == "USD" and e.get("impact") == "High" and e.get("date")]
            cache = {"fetched": now.isoformat(), "events": ev}
            save_json(CAL, cache)
        except Exception as e:  # noqa: BLE001
            print(f"Calendar fetch failed (using cache): {e}")
    return cache.get("events", [])          # every high-impact USD event counts


def macro_block(now):
    """Returns a reason string if a big US release blocks new trades right now, else None."""
    before = CFG.get("news_block_before_min", 30)
    after = CFG.get("news_block_after_min", 30)
    t_et = now.astimezone(ET)
    st = session_times(t_et.date())
    for e in macro_events(now):
        et = pd.Timestamp(e["time"]).to_pydatetime()
        ev_et = et.astimezone(ET)
        if st and ev_et.date() == t_et.date() and ev_et < st[0]:
            # pre-market release (CPI, jobs at 8:30 ET): the open is volatile, wait until e.g. 10:00 ET
            until = datetime.combine(t_et.date(), hm(CFG.get("premarket_news_block_until_et", "10:00")), ET)
            if t_et < until:
                return f"{e['title']} released pre-market; waiting until {until:%H:%M} ET"
            continue
        if "FOMC" in e["title"].upper() or "FEDERAL FUNDS" in e["title"].upper():
            if ev_et.date() == t_et.date() and t_et >= ev_et - timedelta(minutes=before):
                return f"{e['title']} today: no new trades from 30 min before until the close"
        if ev_et - timedelta(minutes=before) <= t_et <= ev_et + timedelta(minutes=after):
            return f"{e['title']} at {ev_et:%H:%M} ET"
    return None


def upcoming_macro(now, hours=36):
    out = []
    for e in macro_events(now):
        t = pd.Timestamp(e["time"])
        if now - timedelta(hours=1) <= t <= now + timedelta(hours=hours):
            out.append({"title": e["title"], "dubai": t.tz_convert("Asia/Dubai").strftime("%a %H:%M")})
    return out[:6]


# ───────────── earnings ─────────────
def _yahoo_dates(sym):
    import yfinance as yf
    t = yf.Ticker(sym)
    dates = set()
    try:
        df = t.get_earnings_dates(limit=8)
        if df is not None:
            for ts in df.index:
                dates.add(pd.Timestamp(ts).tz_convert(ET).date().isoformat())
    except Exception:  # noqa: BLE001
        pass
    if not dates:
        try:
            cal = t.calendar or {}
            for d in cal.get("Earnings Date", []) or []:
                dates.add(pd.Timestamp(d).date().isoformat())
        except Exception:  # noqa: BLE001
            pass
    return sorted(dates)


def earnings_dates(stocks, now):
    cache = load_json(EARN, {})
    today = now.astimezone(ET).date().isoformat()
    if cache.get("refreshed") != today:
        for s in stocks:
            try:
                d = _yahoo_dates(s)
                if d:
                    cache.setdefault("dates", {})[s] = d
            except Exception as e:  # noqa: BLE001
                print(f"Earnings lookup {s} failed: {e}")
        cache["refreshed"] = today
        save_json(EARN, cache)
    out = {}
    for s in stocks:
        out[s] = sorted(set(cache.get("dates", {}).get(s, [])) | set(CFG.get("earnings_dates", {}).get(s, [])))
    return out


def next_trading_day(d):
    d += timedelta(days=1)
    while not session_times(d):
        d += timedelta(days=1)
    return d


def earnings_block(sym, dates, now):
    """Reason string if sym is blocked for earnings today, else None. Also returns next date for display."""
    today = now.astimezone(ET).date()
    upcoming = [d for d in dates if d >= today.isoformat()]
    nxt = upcoming[0] if upcoming else None
    if not dates and CFG.get("block_if_earnings_unknown"):
        return "earnings date unknown", nxt
    if today.isoformat() in dates:
        return "earnings today", nxt
    if next_trading_day(today).isoformat() in dates:
        return "earnings next trading day", nxt
    return None, nxt


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print(earnings_dates(CFG["stocks"], now))
    print(upcoming_macro(now, 24 * 7))
