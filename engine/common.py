"""Shared helpers: config, US market clock, eToro market data, ntfy alerts.

Public repo: never print balances, dollar P/L or keys from here.
"""
import json
import os
import re
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
OUT.mkdir(parents=True, exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text())
ET = ZoneInfo("America/New_York")
DUBAI = timezone(timedelta(hours=4))
SITE = "https://basemayman-ux.github.io/stock-agent/"

# ───────────────────────── US market clock ─────────────────────────
# NYSE full-day holidays and 1 pm early closes (update once a year).
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}


def et_now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(ET)


def session_times(d: date):
    """(open, close) as aware ET datetimes, or None when the market is shut that day."""
    if d.weekday() >= 5 or d.isoformat() in HOLIDAYS:
        return None
    close = dtime(13, 0) if d.isoformat() in EARLY_CLOSE else dtime(16, 0)
    return datetime.combine(d, dtime(9, 30), ET), datetime.combine(d, close, ET)


def market_status(now=None):
    """Returns dict: open (bool), phase, open/close times, minutes since open / to close."""
    t = et_now(now)
    st = session_times(t.date())
    if not st:
        return {"open": False, "phase": "closed (weekend/holiday)"}
    o, c = st
    info = {"open_et": o, "close_et": c, "since_open": (t - o).total_seconds() / 60,
            "to_close": (c - t).total_seconds() / 60}
    if t < o:
        info.update(open=False, phase="pre-market")
    elif t >= c:
        info.update(open=False, phase="after close")
    else:
        info.update(open=True, phase="open")
    return info


def hm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


# ───────────────────────── eToro market data ─────────────────────────
MD = "https://public-api.etoro.com/api/v1/market-data"


def etoro_on():
    return bool(os.getenv("ETORO_API_KEY", "").strip() and os.getenv("ETORO_USER_KEY", "").strip())


def headers():
    return {"x-request-id": str(uuid.uuid4()),
            "x-api-key": os.getenv("ETORO_API_KEY", "").strip(),
            "x-user-key": os.getenv("ETORO_USER_KEY", "").strip()}


def md_get(path, params=None):
    r = requests.get(MD + path, params=params, timeout=15, headers=headers())
    if r.status_code != 200:
        raise RuntimeError(f"eToro {path}: HTTP {r.status_code} {r.text[:150]}")
    return r.json()


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


_IDS_FILE = OUT / "instruments.json"


def instrument_id(sym):
    """eToro instrumentId for a stock symbol. Order of preference: config pin, cached lookup, live search."""
    sym = sym.upper()
    pinned = CFG.get("instrument_ids", {}).get(sym)
    if pinned:
        return int(pinned)
    try:
        cache = json.loads(_IDS_FILE.read_text())
    except Exception:  # noqa: BLE001
        cache = {}
    if sym in cache:
        return int(cache[sym]["id"])
    js = md_get("/search", {"internalSymbolFull": sym,
                            "fields": "instrumentId,internalSymbolFull,displayname", "pageSize": 10})
    for d in walk(js):
        if str(d.get("internalSymbolFull", "")).upper() == sym and d.get("instrumentId"):
            cache[sym] = {"id": int(d["instrumentId"]), "name": d.get("displayname") or d.get("displayName") or sym}
            _IDS_FILE.write_text(json.dumps(cache, indent=1, sort_keys=True))
            print(f"eToro {sym} instrument id: {cache[sym]['id']}")
            return cache[sym]["id"]
    raise RuntimeError(f"eToro search found no instrument for {sym}")


def candles(sym, interval="FiveMinutes", count=1000):
    js = md_get(f"/instruments/{instrument_id(sym)}/history/candles/asc/{interval}/{count}")
    rows = [c for g in js.get("candles", []) for c in g.get("candles", [])]
    if not rows:
        raise RuntimeError(f"eToro {sym} {interval}: no candles")
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("fromDate"), utc=True)
    df = df.rename(columns=str.lower)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df]
    df = df[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "high", "low", "close"])
    if "volume" not in df:
        df["volume"] = 0.0
    return df[~df.index.duplicated(keep="last")].sort_index()


def quotes(syms):
    """{sym: (bid, ask)} from eToro live rates, one request for all stocks."""
    ids = {instrument_id(s): s for s in syms}
    js = md_get("/instruments/rates", {"instrumentIds": ",".join(str(i) for i in ids)})
    out = {}
    for d in walk(js):
        iid = d.get("instrumentId", d.get("instrumentID"))
        if iid is not None and "bid" in d and "ask" in d and int(iid) in ids:
            out[ids[int(iid)]] = (float(d["bid"]), float(d["ask"]))
    if len(ids) == 1 and not out:              # single-instrument reply without an id field
        for d in walk(js):
            if "bid" in d and "ask" in d:
                out[next(iter(ids.values()))] = (float(d["bid"]), float(d["ask"]))
                break
    return out


def quote(sym):
    q = quotes([sym]).get(sym.upper())
    if not q:
        raise RuntimeError(f"eToro rates: no bid/ask for {sym}")
    return q


# ───────────────────────── alerts ─────────────────────────
def notify(text, kind="signal"):
    """Phone push via ntfy.sh. Topic comes from the NTFY_TOPIC secret (a NEW topic, not the gold one)."""
    try:
        Path("/tmp/force_commit").touch()          # publish the website on this run
    except Exception:  # noqa: BLE001
        pass
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        print(f"[ntfy skipped, NTFY_TOPIC not set] {text.splitlines()[0]}")
        return
    first = text.split("\n", 1)[0]
    title = re.sub(r"[^\w\s/().,:+%-]", "", first).strip()[:120] or "Stock alert"
    prio = {"signal": 5, "update": 4, "watch": 3}.get(kind, 3)
    tags = {"signal": ["rotating_light"], "update": ["dart"], "watch": ["eyes"]}.get(kind, ["bell"])
    try:
        r = requests.post("https://ntfy.sh/", timeout=20, json={
            "topic": topic, "title": title, "message": text, "priority": prio, "tags": tags, "click": SITE})
        print(f"ntfy ({kind}): HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        print(f"ntfy failed: {e}")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return default


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=1, default=str))
