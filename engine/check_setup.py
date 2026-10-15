"""Setup check (step 2): finds each stock's eToro instrumentId, checks live prices and candles,
and measures the spread. Safe for a public log: prices only, no account data.
Also checks the earnings and macro calendars."""
import sys
from datetime import datetime, timezone

import news
from common import CFG, candles, etoro_on, instrument_id, notify, quotes

now = datetime.now(timezone.utc)
lines, ok = [], True
if not etoro_on():
    print("❌ eToro keys missing: set ETORO_API_KEY and ETORO_USER_KEY secrets")
    sys.exit(1)

ids = {}
for s in CFG["stocks"]:
    try:
        ids[s] = instrument_id(s)
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"❌ {s}: id lookup failed: {e}")
try:
    qs = quotes(list(ids))
except Exception as e:  # noqa: BLE001
    qs, ok = {}, False
    lines.append(f"❌ live prices failed: {e}")

for s, iid in ids.items():
    try:
        m5 = candles(s, "FiveMinutes")
        h1 = candles(s, "OneHour")
        q = qs.get(s)
        sp = f"spread {q[1] - q[0]:.3f} ({(q[1] - q[0]) / q[1] * 100:.3f}%)" if q else "NO live price"
        vol = "with volume" if m5["volume"].sum() > 0 else "NO volume"
        lines.append(f"✅ {s} id {iid}: bid/ask {q[0]:.2f}/{q[1]:.2f} {sp}; "
                     f"5m bars {len(m5)} (last {m5.index[-1]:%d %b %H:%M} UTC, {vol}); 1h bars {len(h1)}"
                     if q else f"⚠️ {s} id {iid}: {sp}; 5m bars {len(m5)}; 1h bars {len(h1)}")
        if not q:
            ok = False
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"❌ {s}: candles failed: {e}")

try:
    ed = news.earnings_dates(CFG["stocks"], now)
    lines.append("Earnings: " + ", ".join(f"{s} {next((d for d in v if d >= now.date().isoformat()), '?')}"
                                          for s, v in ed.items()))
except Exception as e:  # noqa: BLE001
    lines.append(f"⚠️ earnings lookup failed: {e}")
try:
    lines.append(f"US news this week: {len(news.macro_events(now))} high-impact events")
except Exception as e:  # noqa: BLE001
    lines.append(f"⚠️ news calendar failed: {e}")

text = ("✅ STOCK AGENT SETUP CHECK PASSED" if ok else "❌ STOCK AGENT SETUP CHECK: problems") + "\n" + "\n".join(lines)
print(text)
print("\nInstrument ids (saved to docs/data/instruments.json):", ids)
notify(text, kind="update")
sys.exit(0 if ok else 1)
