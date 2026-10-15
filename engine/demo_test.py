"""DEMO test of the eToro auto-trade path for ONE stock: BUY then SHORT,
each: open 1 share -> move stop -> close. Always uses the DEMO account, whatever config.json says.
Usage: python demo_test.py NVDA [keep]
Results go to ntfy and the run log (no money amounts)."""
import sys
import time

import autotrade as at
from common import instrument_id, notify, quote

sym = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()
keep = "keep" in sys.argv[2:]
p = at.paths("demo")
steps = []


def report():
    ok = all("FAILED" not in s for s in steps)
    text = f"🧪 DEMO TEST {sym} (buy + short) " + ("PASSED ✅" if ok else "FAILED ❌") + "\n" + "\n".join(steps)
    print(text)
    notify(text, kind="update")
    sys.exit(0 if ok else 1)


try:
    iid = instrument_id(sym)
    bid, ask = quote(sym)
    steps.append(f"{sym} id {iid}. Price OK: sell {bid:.2f} / buy {ask:.2f} (spread {ask - bid:.3f})")
except Exception as e:  # noqa: BLE001
    steps.append(f"Price FAILED: {e}")
    report()

for direction in ("LONG", "SHORT"):
    tg = "BUY" if direction == "LONG" else "SHORT"
    sign = 1 if direction == "LONG" else -1
    bid, ask = quote(sym)
    px = ask if direction == "LONG" else bid
    sl, tp = round(px * (1 - sign * 0.03), 2), round(px * (1 + sign * 0.03), 2)
    try:
        pos, fill, units = at.place(p, iid, direction, 1.0, sl, tp, 1)
        steps.append(f"{tg}: opened {units:g} share at {fill:.2f} (stop {sl}, target {tp})")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tg}: open FAILED: {e}")
        continue
    time.sleep(5)
    try:
        new_sl = round(fill * (1 - sign * 0.02), 2)
        at.call("PATCH", p["edit"].format(pos), body={"stopLossRate": new_sl})
        steps.append(f"{tg}: stop moved to {new_sl} OK")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tg}: move stop FAILED: {e}")
    if keep:
        steps.append(f"{tg}: left open on purpose, close it in eToro")
        continue
    time.sleep(20)
    try:
        at.close_position(p, pos, iid)
        closed = False
        for _ in range(6):
            time.sleep(3)
            if pos not in at.open_ids(p):
                closed = True
                break
        steps.append(f"{tg}: closed OK" if closed else f"{tg}: close FAILED (still open, close it in eToro)")
    except Exception as e:  # noqa: BLE001
        steps.append(f"{tg}: close FAILED: {e} (close it in eToro)")

report()
