"""Places the engine's 🟢/🔴 stock signals on eToro automatically, and manages them.

Same proven order code as the gold agent, per stock:
  * market order (body uses instrumentId only), stop loss + 1R take profit on every order
  * fill confirmed by order lookup OR a new position for that stock in the portfolio
  * stop → entry once price reaches 0.7R (PATCH), everything closed before the US close

Safety:
  * config "autotrade.mode": off | demo | real. Real ALSO needs the secret ETORO_ALLOW_REAL=yes.
  * real mode uses the smaller real_* limits (0.25% risk, 2 trades/day, stop after 1R lost)
  * max one open trade per stock, a total cap, trades/day cap, daily loss stop in R,
    spread cap, exposure cap.
State: docs/data/autotrade.json (public repo: no balances, sizes or dollar P/L stored).
"""
import math
import os
import time
import uuid

import pandas as pd
import requests

from common import CFG, ET, OUT, instrument_id, load_json, notify, quote, save_json

API = "https://public-api.etoro.com"
STATE = OUT / "autotrade.json"
DEFAULTS = {"mode": "off", "leverage": 2, "max_open_total": 2, "max_open_per_stock": 1, "max_trades_per_day": 4,
            "max_trades_per_stock_per_day": 1, "daily_loss_stop_r": 2.0, "max_position_pct_of_equity": 100,
            "real_risk_pct": 0.25, "real_max_trades_per_day": 2, "real_daily_loss_stop_r": 1.0}


def etoro_on():
    return bool(os.getenv("ETORO_API_KEY", "").strip() and os.getenv("ETORO_USER_KEY", "").strip())


def cfg():
    c = {**DEFAULTS, **CFG.get("autotrade", {})}
    mode = str(c["mode"]).lower()
    if mode == "real" and os.getenv("ETORO_ALLOW_REAL", "").strip().lower() != "yes":
        print("Auto-trade: config says real but ETORO_ALLOW_REAL secret is not 'yes' -> using demo")
        mode = "demo"
    if mode not in ("demo", "real") or not etoro_on():
        mode = "off"
    c["mode"] = mode
    c["risk_pct"] = c["real_risk_pct"] if mode == "real" else CFG["risk_pct"]
    if mode == "real":
        c["max_trades_per_day"] = min(c["max_trades_per_day"], c["real_max_trades_per_day"])
        c["daily_loss_stop_r"] = min(c["daily_loss_stop_r"], c["real_daily_loss_stop_r"])
    return c


def paths(mode):
    d = mode == "demo"
    return {
        "open": "/api/v2/trading/execution/demo/orders" if d else "/api/v2/trading/execution/orders",
        "lookup": "/api/v2/trading/info/demo/orders:lookup" if d else "/api/v2/trading/info/orders:lookup",
        "close": ("/api/v1/trading/execution/demo/market-close-orders/positions/{}" if d
                  else "/api/v1/trading/execution/market-close-orders/positions/{}"),
        "edit": "/api/v2/trading/demo/positions/{}" if d else "/api/v2/trading/positions/{}",
        "pnl": "/api/v1/trading/info/demo/pnl" if d else "/api/v1/trading/info/real/pnl",
    }


def call(method, path, body=None, params=None, ref=None):
    r = requests.request(method, API + path, json=body, params=params, timeout=20, headers={
        "x-request-id": ref or str(uuid.uuid4()),
        "x-api-key": os.getenv("ETORO_API_KEY", "").strip(),
        "x-user-key": os.getenv("ETORO_USER_KEY", "").strip()})
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:700]}")
    return r.json() if r.text else {}


def load():
    return load_json(STATE, {"trades": []})


def save(st):
    st["trades"] = st["trades"][-300:]
    save_json(STATE, st)
    try:
        open("/tmp/force_commit", "a").close()
    except Exception:  # noqa: BLE001
        pass


def has_open():
    return any(t["status"] == "open" for t in load()["trades"])


def positions(p):
    js = call("GET", p["pnl"])
    pos = (js.get("clientPortfolio") or js).get("positions", [])
    out = {}
    for x in pos:
        pid = int(x.get("positionID", x.get("positionId", 0)) or 0)
        if pid:
            out[pid] = x
    return out


def open_ids(p):
    return set(positions(p))


def place(p, iid, direction, units, sl, tp, lev):
    """Market order; returns (positionId, fill price, units)."""
    ref = str(uuid.uuid4())
    body = {"action": "open", "transaction": "buy" if direction == "LONG" else "sellShort",
            "instrumentId": iid, "settlementType": "cfd",           # eToro: symbol OR instrumentId, not both
            "orderType": "mkt", "leverage": lev, "units": units, "orderCurrency": "usd",
            "stopLossRate": round(sl, 2), "takeProfitRate": round(tp, 2), "stopLossType": "fixed"}
    try:
        before = open_ids(p)
    except Exception:  # noqa: BLE001
        before = None
    resp = call("POST", p["open"], body=body, ref=ref) or {}
    oid = resp.get("orderId") or resp.get("orderID")
    last = f"order accepted (orderId {oid})"
    for _ in range(15):                                 # up to ~30 s
        time.sleep(2)
        try:
            o = call("GET", p["lookup"], params={"orderId": oid} if oid else {"referenceId": ref})
            st = o.get("status") or {}
            name = str(st.get("name", st.get("id", "")))
            ex = o.get("positionExecutions") or []
            if ex:
                od = ex[0].get("openingData") or {}
                return int(ex[0]["positionId"]), float(od.get("avgPrice") or 0), float(od.get("units") or units)
            if name.lower() in ("rejected", "canceled", "cancelled", "expired", "4", "7", "8"):
                raise RuntimeError(f"order {name}: {st.get('errorMessage') or st.get('errorCode') or ''}")
            last = f"lookup status '{name}'"
        except RuntimeError as e:
            if str(e).startswith("order "):
                raise
            last = f"lookup error: {e}"
        if before is not None:
            try:
                for pid, x in positions(p).items():
                    inst = int(x.get("instrumentID", x.get("instrumentId", -1)) or -1)
                    if pid not in before and inst == iid:
                        return pid, float(x.get("openRate") or 0), float(x.get("units") or units)
            except Exception as e:  # noqa: BLE001
                last += f" | portfolio error: {e}"
    raise RuntimeError(f"order not confirmed within 30 s ({last[:400]})")


def close_position(p, pid, iid):
    call("POST", p["close"].format(pid), body={"InstrumentID": iid})


def tag(mode):
    return "🧪 DEMO" if mode == "demo" else "💰 REAL"


def size(plan, c):
    try:
        eq = float(os.getenv("ACCOUNT_EQUITY", "0").replace(",", ""))
    except ValueError:
        eq = 0
    if eq <= 0:
        return 0.0
    sh = eq * c["risk_pct"] / 100 / plan["R"]
    sh = min(sh, eq * c["max_position_pct_of_equity"] / 100 / plan["entry"])
    return math.floor(sh * 100) / 100


def on_signal(rec, plan, now):
    """Called right after a 🟢/🔴 alert. Opens one position if every safety check passes."""
    c = cfg()
    if c["mode"] == "off":
        return
    st, p, mode, sym = load(), paths(c["mode"]), c["mode"], rec["sym"]
    today = now.astimezone(ET).date().isoformat()
    todays = [t for t in st["trades"] if t["day"] == today and t["mode"] == mode]
    open_all = [t for t in st["trades"] if t["status"] == "open" and t["mode"] == mode]
    lost_r = -sum(t.get("r", 0) for t in todays if t.get("r", 0) < 0)
    units = size(plan, c)
    why = None
    if units <= 0:
        why = "no position size (ACCOUNT_EQUITY secret not set)"
    elif sum(1 for t in open_all if t["sym"] == sym) >= c["max_open_per_stock"]:
        why = f"a {sym} auto-trade is already open"
    elif len(open_all) >= c["max_open_total"]:
        why = f"{len(open_all)} auto-trades already open (max {c['max_open_total']})"
    elif len(todays) >= c["max_trades_per_day"]:
        why = f"daily limit of {c['max_trades_per_day']} trades reached"
    elif sum(1 for t in todays if t["sym"] == sym) >= c["max_trades_per_stock_per_day"]:
        why = f"already traded {sym} today"
    elif lost_r >= c["daily_loss_stop_r"]:
        why = f"daily loss stop hit ({lost_r:.1f}R lost today)"
    else:
        try:
            bid, ask = quote(sym)
            if (ask - bid) / ask * 100 > CFG["max_spread_pct"]:
                why = f"spread too wide ({(ask - bid) / ask * 100:.2f}%)"
        except Exception as e:  # noqa: BLE001
            why = f"no eToro quote ({e})"
    if why:
        notify(f"{tag(mode)} {sym} auto-trade skipped: {why}.", kind="update")
        return
    d = rec["direction"]
    sign = 1 if d == "LONG" else -1
    iid = instrument_id(sym)
    t = {"sig": rec["id"], "sym": sym, "iid": iid, "day": today, "mode": mode, "dir": d, "sl": plan["sl"],
         "tp": plan["tp"], "R": plan["R"], "status": "open", "be": False, "opened": now.isoformat()}
    try:
        t["pos"], fill, filled = place(p, iid, d, units, plan["sl"], plan["tp"], c["leverage"])
    except Exception as e:  # noqa: BLE001
        notify(f"⚠️ {tag(mode)} {sym} auto-trade not opened: {e}", kind="signal")
        return
    t["entry"] = round(fill or plan["entry"], 2)
    t["be_px"] = round(t["entry"] + sign * CFG.get("be_at_r", 0.7) * plan["R"], 2)
    st["trades"].append(t)
    save(st)
    notify(f"{tag(mode)} 🤖 opened {d} {sym} {filled:g} shares at {t['entry']:.2f}\n"
           f"Stop {plan['sl']:.2f} · target {plan['tp']:.2f} (1R) · stop → entry at {t['be_px']:.2f}.",
           kind="update")


def _r_from_sig(t, journal):
    """Final R from the paper journal (same entry/stop/target, after spread), or None if not closed yet."""
    for s in journal.get("signals", []):
        if s.get("id") == t["sig"] and s.get("status") == "closed":
            return s.get("r", 0)
    return None


def _r_estimate(t, q):
    """Quick estimate when eToro has closed the position: near the target = +1R, else entry (0) or stop (-1)."""
    if q:
        bid, ask = q
        px = ask if t["dir"] == "SHORT" else bid
        sign = 1 if t["dir"] == "LONG" else -1
        if sign * (px - t["entry"]) >= 0.5 * t["R"]:
            return 1.0
    return 0.0 if t["be"] else -1.0


def manage(journal, now, mkt, qs):
    """Every run: stop → entry at 0.7R, flatten before the close, record closed trades."""
    st = load()
    changed = False
    for t in st["trades"]:                       # replace estimates with the journal result once known
        if t["status"] == "closed" and t.get("r_src") == "estimate":
            r = _r_from_sig(t, journal)
            if r is not None:
                t["r"], t["r_src"], changed = r, "journal", True
    live = [t for t in st["trades"] if t["status"] == "open"]
    if not live:
        if changed:
            save(st)
        return
    flatten = (not CFG.get("hold_overnight", False)) and (
        not mkt.get("open") or mkt.get("to_close", 999) <= CFG["flatten_minutes_before_close"])
    for t in live:
        mode, p = t["mode"], paths(t["mode"])
        try:
            ids = open_ids(p)
        except Exception as e:  # noqa: BLE001
            print(f"Auto-trade manage: {e}")
            continue
        if t["pos"] in ids:
            if flatten:
                if not mkt.get("open") and mkt.get("phase") != "after close":
                    continue                              # can't close while the market is shut
                try:
                    close_position(p, t["pos"], t["iid"])
                    notify(f"{tag(mode)} 🔔 {t['sym']} closed before the US close (intraday rule).", kind="update")
                except Exception as e:  # noqa: BLE001
                    notify(f"⚠️ {tag(mode)} could not close {t['sym']} before the close: {e}. Close it in eToro!",
                           kind="signal")
                continue
            if t["be"]:
                continue
            q = qs.get(t["sym"])
            try:
                q = q or quote(t["sym"])
            except Exception:  # noqa: BLE001
                q = None
            if q:
                bid, ask = q
                reached = bid >= t["be_px"] if t["dir"] == "LONG" else ask <= t["be_px"]
                if reached:
                    try:
                        call("PATCH", p["edit"].format(t["pos"]), body={"stopLossRate": round(t["entry"], 2)})
                        t["be"] = changed = True
                        notify(f"{tag(mode)} 📍 {t['sym']} reached 0.7R: stop moved to entry {t['entry']:.2f}.",
                               kind="update")
                    except Exception as e:  # noqa: BLE001
                        notify(f"⚠️ {tag(mode)} could not move {t['sym']} stop to entry: {e}. Do it in eToro.",
                               kind="signal")
        else:
            t["status"] = "closed"
            t["closed"] = pd.Timestamp(now).isoformat()
            r = _r_from_sig(t, journal)
            if r is None:
                q = qs.get(t["sym"])
                try:
                    q = q or quote(t["sym"])
                except Exception:  # noqa: BLE001
                    q = None
                r, t["r_src"] = _r_estimate(t, q), "estimate"
            t["r"] = r
            changed = True
            notify(f"{tag(mode)} 🤖 {t['sym']} {t['dir']} closed (≈{t['r']:+.2f}R).", kind="update")
    if changed:
        save(st)


def public_summary(now):
    """What the public dashboard may show: no sizes, no money."""
    c = cfg()
    st = load()
    today = now.astimezone(ET).date().isoformat()
    td = [t for t in st["trades"] if t["day"] == today]
    return {"mode": c["mode"], "open": [{"sym": t["sym"], "dir": t["dir"], "entry": t["entry"], "sl": t["sl"],
                                          "tp": t["tp"], "be": t["be"]} for t in st["trades"] if t["status"] == "open"],
            "today_trades": len(td), "today_r": round(sum(t.get("r", 0) for t in td), 2),
            "total_trades": len([t for t in st["trades"] if t["status"] == "closed"]),
            "total_r": round(sum(t.get("r", 0) for t in st["trades"] if t["status"] == "closed"), 2)}
