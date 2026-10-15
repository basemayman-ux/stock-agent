# Stock Agent: setup, day by day

Everything below is copy-paste into **Terminal** on your MacBook. Take one day at a time and
send me a screenshot after each day; I'll check the GitHub side myself.

> US market hours in Dubai: **17:30–00:00** now (US summer time). From **Mon 2 Nov 2026** (after US clocks change)
> it is **18:30–01:00**. Days 3 and 5 need the market open.

---

## Day 1 · Create the repo and the website (about 15 min, any time)

**1. Unpack the download.** This picks the right zip even if the browser renamed it to `stock-agent (1).zip`:

```bash
cd ~
Z=""; for f in ~/Downloads/stock-agent*.zip; do unzip -p "$f" stock-agent/VERSION 2>/dev/null | grep -q "v1-20261015" && Z="$f"; done; echo "Using: $Z"
unzip -o "$Z" -d ~ && cd ~/stock-agent && cat VERSION
```
You should see `stock-agent v1-20261015`. (If `~/stock-agent` already existed, check with me first.)

**2. Create the GitHub repo and push** (separate from gold-agent):

```bash
cd ~/stock-agent
gh repo create basemayman-ux/stock-agent --public --source=. --push
```

**3. Turn on the website (GitHub Pages from the docs folder):**

```bash
cd ~/stock-agent
gh api -X POST repos/basemayman-ux/stock-agent/pages -f "source[branch]=main" -f "source[path]=/docs"
```
After ~5 minutes: https://basemayman-ux.github.io/stock-agent/ shows "Waiting for the first run of the agent…". That is correct for now.

**4. New ntfy topic for stocks.** In the ntfy app on your Android: **+** → topic name:

```
stocks-bassem-323fa105c1
```
Then save it as a secret (run inside the folder):

```bash
cd ~/stock-agent
gh secret set NTFY_TOPIC --body "stocks-bassem-323fa105c1"
```

---

## Day 2 · eToro keys, secrets and the setup check (about 20 min)

**1. Create a NEW eToro key pair for the stock agent.** Separate keys mean you can revoke one without stopping gold.
api-portal.etoro.com → Settings → Trading → API Key Management → click **Clear** first, then tick only:
**Market Data Read · Trading-Real Read · Trading-Demo Read + Write**. Nothing else.

**2. Save the secrets.** Each command asks you to paste the value. Never paste keys in chat.

```bash
cd ~/stock-agent
gh secret set ETORO_API_KEY      # eToro "Public key"
gh secret set ETORO_USER_KEY     # eToro "Private key"
gh secret set ACCOUNT_EQUITY     # your eToro DEMO balance, numbers only, e.g. 100000
gh secret set XLSX_PASSWORD      # a password for the P/L workbook
gh secret list
```
`gh secret list` should show 5 names: ACCOUNT_EQUITY, ETORO_API_KEY, ETORO_USER_KEY, NTFY_TOPIC, XLSX_PASSWORD.
Do **not** set ETORO_ALLOW_REAL.

**3. Run the setup check.** It finds the 5 eToro instrument IDs, checks live prices, candles, spreads, earnings and the news calendar:

```bash
cd ~/stock-agent
gh workflow run check-setup.yml && sleep 8 && gh run watch $(gh run list --workflow=check-setup.yml -L1 --json databaseId -q '.[0].databaseId')
```
You get an ntfy message "SETUP CHECK PASSED". Screenshot it for me. (Prices work best when the market is open, but the check runs any time.)

**4. Backtest on the last 60 days of real data** (~5 min, any time):

```bash
cd ~/stock-agent
gh workflow run backtest.yml
```
I'll read the results from GitHub myself, or send me a screenshot of the run's last lines.

---

## Day 3 · Demo order test, one per stock (market OPEN, ~10 min)

Each test opens a 1-share BUY and a 1-share SHORT on **demo**, moves the stop, then closes both, and reports to ntfy:

```bash
cd ~/stock-agent
for S in NVDA AMZN TSLA MSFT META; do gh workflow run demo-test.yml -f stock=$S; sleep 90; done
```
You should get 5 ntfy messages "DEMO TEST … PASSED ✅". Check eToro demo: no positions left open.

---

## Day 4 · The 5-minute timer on cron-job.org (~10 min)

Same as your gold job, with a different URL and hours.

1. Your GitHub token must cover the new repo. If the gold job uses a *fine-grained* token:
   github.com → Settings → Developer settings → Fine-grained tokens → your token → **Repository access** → add **stock-agent**
   (permission **Actions: Read and write**). Classic tokens with `repo` scope work already.
2. cron-job.org → **Create cronjob**:
   * URL: `https://api.github.com/repos/basemayman-ux/stock-agent/actions/workflows/stock-agent.yml/dispatches`
   * Schedule: **custom**, every **5** minutes, hours **13–21**, days **Mon–Fri**, time zone **UTC**
     (covers the US session in both summer and winter time; the engine itself knows the exact hours and holidays).
   * Advanced → Request method **POST**; headers:
     `Authorization: Bearer <your token>` · `Accept: application/vnd.github+json`
   * Request body: `{"ref":"main"}`
3. Test alert:

```bash
cd ~/stock-agent
gh workflow run stock-agent.yml -f test_alert=true
```
ntfy: "Stock agent test alert". The website fills in within ~10 minutes.

---

## Day 5 onward · Demo for 2+ weeks

Nothing to do: signals arrive on ntfy, demo trades open and close by themselves, the dashboard tracks results in R.
After 2 weeks (and at least ~20 signals) we review together before any real money.

---

### Changing settings later
Edit `config.json` (e.g. add a stock to `"stocks"`, or `"allow_short": false`), then:

```bash
cd ~/stock-agent && git pull && git add config.json && git commit -m "settings" && git push
```

### Test without eToro (optional, on the Mac)
```bash
cd ~/stock-agent && pip3 install -r requirements.txt && python3 tests/simulate.py
```
Runs a full fake trading day against a simulated eToro and prints every alert and trade.
