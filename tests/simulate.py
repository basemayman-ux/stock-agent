"""Offline rehearsal: runs the real engine + auto-trader for a whole simulated US session
against a FAKE eToro (prices, orders, stops, closes) and a fake ntfy. Nothing leaves this computer.

Usage (from the repo folder):  python tests/simulate.py
Runs on a throw-away copy of the repo, so your docs/data files are not touched.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="stocksim_"))
shutil.copytree(SRC, WORK / "repo", ignore=shutil.ignore_patterns(".git", "__pycache__"))
for f in (WORK / "repo" / "docs" / "data").glob("*"):
    if f.name != ".gitkeep":
        f.unlink()
if os.getenv("SIM_TARGET_R"):                       # e.g. 5 = targets never hit, tests the end-of-day close
    cf = WORK / "repo" / "config.json"
    c = json.loads(cf.read_text())
    c["target_r"], c["be_at_r"] = float(os.environ["SIM_TARGET_R"]), float(os.environ["SIM_TARGET_R"])
    cf.write_text(json.dumps(c))
sys.path.insert(0, str(WORK / "repo" / "engine"))
os.environ.update(ETORO_API_KEY="fake", ETORO_USER_KEY="fake", ACCOUNT_EQUITY="10000", NTFY_TOPIC="sim")

SIM_DAY = pd.Timestamp("2026-10-14")                 # a Wednesday, US daylight time (open 13:30 UTC)
SYMS = {"NVDA": (1001, 185.0), "AMZN": (1002, 220.0), "TSLA": (1003, 430.0), "MSFT": (1004, 510.0),
        "META": (1005, 720.0)}
SPREAD = {"NVDA": 0.04, "AMZN": 0.05, "TSLA": 0.12, "MSFT": 0.10, "META": 0.20}
rng = np.random.default_rng(int(os.getenv("SEED", "7")))


def make_bars(base):
    """20 sessions of regular-hours 5-minute bars with intraday trends, pullbacks and a little noise."""
    days = pd.bdate_range(SIM_DAY - pd.Timedelta(days=30), SIM_DAY)
    rows, px = [], base
    for d in days:
        start = pd.Timestamp(d.date()).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
        drift = rng.normal(0, 0.0006)
        for i in range(78):
            ts = start + pd.Timedelta(minutes=5 * i)
            wave = 0.0012 * np.sin(i / 7)
            ret = drift + rng.normal(0, 0.0018) + wave * 0.2
            o = px
            c = px * (1 + ret)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0009)))
            lo = min(o, c) * (1 - abs(rng.normal(0, 0.0009)))
            v = abs(rng.normal(1e5, 3e4)) * (1.6 if i < 6 or i > 70 else 1.0) * (2.2 if rng.random() < 0.07 else 1)
            rows.append((ts.tz_convert("UTC"), o, h, lo, c, v))
            px = c
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).set_index("t")
    return df


BARS = {s: make_bars(b) for s, (_, b) in SYMS.items()}
ID2SYM = {i: s for s, (i, _) in SYMS.items()}
NOW = {"t": None}
POS, ORDERS, NTFY = {}, {}, []
NEXT = {"pos": 5000, "ord": 900}


def price_at(sym):
    df = BARS[sym]
    last = df[df.index <= NOW["t"]]
    mid = float(last["close"].iloc[-1]) if len(last) else float(df["open"].iloc[0])
    return mid - SPREAD[sym] / 2, mid + SPREAD[sym] / 2


class Resp:
    def __init__(self, js, code=200):
        self._js, self.status_code = js, code
        self.text = json.dumps(js)

    def json(self):
        return self._js


def candles_js(sym, interval):
    df = BARS[sym]
    df = df[df.index < NOW["t"]]
    if interval == "OneHour":
        df = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                    "volume": "sum"}).dropna()
    df = df.iloc[-1000:]
    return {"candles": [{"candles": [{"fromDate": t.isoformat(), "open": r.open, "high": r.high, "low": r.low,
                                      "close": r.close, "volume": r.volume} for t, r in df.iterrows()]}]}


def check_stops():
    """Fake eToro closes positions that hit their stop or target."""
    for pid, p in list(POS.items()):
        bid, ask = price_at(p["sym"])
        px = bid if p["isBuy"] else ask
        hit_sl = px <= p["sl"] if p["isBuy"] else px >= p["sl"]
        hit_tp = px >= p["tp"] if p["isBuy"] else px <= p["tp"]
        if hit_sl or hit_tp:
            POS.pop(pid)


def fake(method, url, params=None, json_body=None):
    path = urlparse(url).path
    params = params or {}
    if "ntfy.sh" in url:
        NTFY.append(json_body["message"])
        return Resp({})
    if "faireconomy" in url:
        ev = [{"title": "Crude Oil Inventories", "country": "USD", "impact": "High",
               "date": (SIM_DAY.tz_localize("America/New_York") + pd.Timedelta(hours=10, minutes=30)).isoformat()}]
        return Resp(ev)
    if path.endswith("/market-data/search"):
        s = params["internalSymbolFull"]
        return Resp({"items": [{"instrumentId": SYMS[s][0], "internalSymbolFull": s, "displayname": s}]})
    if path.endswith("/instruments/rates"):
        ids = [int(x) for x in str(params["instrumentIds"]).split(",")]
        return Resp({"rates": [dict(instrumentId=i, **dict(zip(("bid", "ask"), price_at(ID2SYM[i])))) for i in ids]})
    if "/history/candles/" in path:
        iid = int(path.split("/instruments/")[1].split("/")[0])
        return Resp(candles_js(ID2SYM[iid], path.split("/asc/")[1].split("/")[0]))
    check_stops()
    if path.endswith("/demo/orders") and method == "POST":
        b = json_body
        assert "symbol" not in b and b["instrumentId"] in ID2SYM and b["stopLossRate"] and b["takeProfitRate"]
        sym = ID2SYM[b["instrumentId"]]
        bid, ask = price_at(sym)
        NEXT["pos"] += 1
        NEXT["ord"] += 1
        POS[NEXT["pos"]] = {"sym": sym, "isBuy": b["transaction"] == "buy", "sl": b["stopLossRate"],
                            "tp": b["takeProfitRate"], "units": b["units"], "open": ask if b["transaction"] == "buy" else bid}
        ORDERS[NEXT["ord"]] = NEXT["pos"]
        return Resp({"orderId": NEXT["ord"]})
    if "orders:lookup" in path:
        pid = ORDERS.get(int(params.get("orderId", 0)))
        p = POS.get(pid, {})
        return Resp({"status": {"name": "Executed"}, "positionExecutions": [
            {"positionId": pid, "openingData": {"avgPrice": p.get("open"), "units": p.get("units")}}]})
    if path.endswith("/demo/pnl"):
        return Resp({"clientPortfolio": {"positions": [{"positionID": k, "instrumentID": SYMS[v["sym"]][0],
                                                        "openRate": v["open"], "units": v["units"]}
                                                       for k, v in POS.items()]}})
    if "/demo/positions/" in path and method == "PATCH":
        POS[int(path.rsplit("/", 1)[1])]["sl"] = json_body["stopLossRate"]
        return Resp({})
    if "market-close-orders/positions/" in path:
        POS.pop(int(path.rsplit("/", 1)[1]), None)
        return Resp({})
    return Resp({"error": f"unmocked {method} {path}"}, 404)


import requests  # noqa: E402

requests.get = lambda url, params=None, timeout=None, headers=None: fake("GET", url, params)
requests.post = lambda url, json=None, timeout=None, headers=None, **k: fake("POST", url, None, json)
requests.request = lambda method, url, json=None, params=None, timeout=None, headers=None: fake(method, url, params, json)
time.sleep = lambda s: None

import news  # noqa: E402

news._yahoo_dates = lambda s: ["2026-10-15"] if s == "TSLA" else ["2026-10-29"]   # TSLA reports tomorrow
import agent  # noqa: E402

t = SIM_DAY.tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=20)
end = SIM_DAY.tz_localize("America/New_York") + pd.Timedelta(hours=16, minutes=10)
runs = 0
while t <= end:
    NOW["t"] = t.tz_convert("UTC") + pd.Timedelta(seconds=40)
    os.environ["FAKE_NOW"] = NOW["t"].isoformat()
    check_stops()
    agent.main()
    runs += 1
    t += timedelta(minutes=5)

D = WORK / "repo" / "docs" / "data"
sig = json.loads((D / "signal.json").read_text())
jr = json.loads((D / "journal.json").read_text())
at = json.loads((D / "autotrade.json").read_text()) if (D / "autotrade.json").exists() else {"trades": []}
print("\n================ SIMULATION SUMMARY ================")
print(f"runs: {runs}, signals: {len(jr['signals'])}, auto-trades: {len(at['trades'])}, "
      f"positions left open at fake eToro: {len(POS)}")
for s in jr["signals"]:
    print(f"  {s['sym']} {s['direction']} score {s['score']} entry {s['entry']} sl {s['sl']} tp {s['tp']} "
          f"-> {s['status']} {s.get('result')} r={s.get('r')}")
for tr in at["trades"]:
    print(f"  AUTO {tr['sym']} {tr['dir']} {tr['status']} be={tr['be']} r={tr.get('r')}")
print("TSLA blocks at the end:", next(c for c in sig["stocks"] if c["sym"] == "TSLA")["blocks"])
print(f"ntfy messages: {len(NTFY)}")
for m in NTFY[:12]:
    print("  ---\n  " + m.replace("\n", "\n  "))
public = json.dumps(sig) + json.dumps(at)
assert "shares" not in json.dumps(at) and "units" not in json.dumps(at), "sizes leaked into public file"
assert not POS, "positions left open after the close"
print("\nPublic files contain no position sizes ✅; all positions closed by the bell ✅")
print("work dir:", WORK)
