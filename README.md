# Stock Agent (NVDA · AMZN · TSLA · MSFT · META)

A copy of the gold agent's proven setup, for US stocks, in its **own repo** so it can never affect `gold-agent`.

* **Engine**: `engine/agent.py` on GitHub Actions, every 5 min during the US session (17:30–00:00 Dubai; 18:30–01:00 in US winter time).
* **Prices**: eToro public API (5-min + 1-hour candles, live bid/ask). Instrument IDs are looked up automatically.
* **Alerts**: ntfy, **new private topic** (not the gold one).
* **Dashboard**: https://basemayman-ux.github.io/stock-agent/ (one card per stock).
* **P/L workbook**: `docs/data/Stocks_PL_tracker.xlsx`, password-protected, spread subtracted.
* **Auto-trade**: eToro **demo** first. Real needs `"mode": "real"` in config.json **and** the secret `ETORO_ALLOW_REAL=yes`.

## Rules (from the gold testing, adapted)

| Rule | Setting (config.json) |
|---|---|
| Trade only score ≥ 80; 👀 WATCH at 75–79 (DO NOT TRADE) | `min_trade_score`, `watch_min_score` |
| 1-hour trend must agree | `require_htf_trend` |
| Exit: all out at 1R, stop → entry at 0.7R | `target_r`, `be_at_r` |
| Skip first & last 15 min; flat 10 min before the close | `skip_first_minutes`, `skip_last_minutes`, `flatten_minutes_before_close` |
| No new trades the day before / of earnings | `earnings_dates` (auto from Yahoo, add your own) |
| Pause around big US news (CPI, jobs, FOMC…); pre-market releases → wait until 10:00 New York | `news_block_*` |
| Gap ≥ 3% at the open → wait until 10:30 New York | `gap_block_pct` |
| Minimum stop: 0.25% of price, 8× spread, 0.6 ATR; spread ≤ 15% of risk | `min_stop_*`, `max_cost_share_of_r` |
| Risk 0.5% (demo) · real: 0.25%, max 2 trades/day, stop after 1R lost | `risk_pct`, `autotrade.real_*` |
| Max 1 open trade per stock, 2 in total, 4 trades/day, 1 per stock/day, daily loss stop 2R | `autotrade.*` |

Defaults chosen for you (change any time in config.json): **CFDs** (so shorts are possible), **leverage 2**,
**intraday only** (no overnight fees, no gap risk). Longs-only: set `"allow_short": false`.

## Files

```
config.json                  all settings
engine/agent.py              signal engine (every 5 min)
engine/strategy.py           score 0–100, entry/stop/target
engine/news.py               earnings dates + US news calendar
engine/autotrade.py          eToro orders (demo/real), stop to entry, close before the bell
engine/etoro_trades.py       encrypted P/L workbook
engine/check_setup.py        step-2 check: ids, prices, candles, spreads
engine/demo_test.py          demo BUY + SHORT test for one stock
engine/backtest.py           last 60 days of real 5-min data, same rules
tests/simulate.py            full day against a FAKE eToro (run locally, no internet needed)
docs/index.html              dashboard
.github/workflows/           stock-agent, check-setup, demo-test, backtest
```

Public repo: logs and website never show balances, sizes or dollar P/L. Sizes go only to your private ntfy topic.
