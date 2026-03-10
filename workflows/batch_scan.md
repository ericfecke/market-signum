# Workflow: batch_scan

## Objective
Run a full NYSE batch scan using the MARKET SIGNUM multi-agent pipeline. Fetch the universe of NYSE-listed stocks, apply a pre-filter, run all five persona agents against each qualifying stock, aggregate scores, and render a master dashboard ranking all analyzed stocks by MARKET SIGNUM score.

## Tool
`tools/batch_runner.py`

## Prerequisites
All persona agent tools must exist and be importable:
- `tools/fetch_nyse_tickers.py`
- `tools/fetch_stock_data.py`
- `tools/dalio_agent.py`
- `tools/graham_agent.py`, `buffett_agent.py`, `lynch_agent.py`, `simons_agent.py`
- `tools/score_and_weight.py`
- `tools/render_html.py`

Optional: `ANTHROPIC_API_KEY` in `.env` — enables LLM reasoning text. Without it, agents fall back to deterministic reasoning strings. Signals and scores are identical either way.

## Required Input
None for a full scan. Optional CLI flags:

| Flag | Description |
|---|---|
| `--limit N` | Process only the first N tickers (test runs) |
| `--tickers SYM,...` | Analyze a specific comma-separated list instead of the full NYSE universe |
| `--delay SECS` | Seconds to pause between tickers (default: 0.5) |
| `--no-cache` | Bypass all caches (stock data, ticker list, agent results) |
| `--skip-filter` | Skip the market cap / volume pre-filter |

## Expected Output
- `output/report.html` — master dashboard with all analyzed stocks
- Per-ticker cache files in `.tmp/`: `<TICKER>.json`, `<TICKER>_graham.json`, `<TICKER>_buffett.json`, `<TICKER>_lynch.json`, `<TICKER>_simons.json`, `<TICKER>_score.json`
- Dalio macro snapshot at `.tmp/_macro_snapshot.json` (shared, run once)
- Run summary printed to stdout

## Batch Run Sequence

```
1. fetch_nyse_tickers()
   ↓ NYSE universe (~3,000 tickers)
   ↓ Cached 24h to .tmp/nyse_tickers.csv

2. run_dalio_agent("_MACRO")
   ↓ regime_flag + macro snapshot
   ↓ Cached to .tmp/_macro_snapshot.json
   ↓ Shared across ALL tickers in the batch

3. For each ticker in universe:
   a. fetch_stock_data(ticker)     → .tmp/<TICKER>.json
   b. pre_filter(stock_data)       → pass or skip
      ├─ SKIP: market_cap < $500M
      ├─ SKIP: avg_vol_20d < 500k shares
      └─ PASS: proceed to agents
   c. run_graham_agent()           → .tmp/<TICKER>_graham.json
      run_buffett_agent()          → .tmp/<TICKER>_buffett.json
      run_lynch_agent()            → .tmp/<TICKER>_lynch.json
      run_simons_agent()           → .tmp/<TICKER>_simons.json
   d. score_and_weight()           → .tmp/<TICKER>_score.json
      (uses shared dalio_result)

4. render_html(all_results)        → output/report.html
```

## Pre-filter Logic

Both conditions must be met for a ticker to proceed to full analysis:

| Field | Source | Threshold |
|---|---|---|
| `fundamentals.market_cap` | fetch_stock_data | ≥ $500,000,000 |
| avg daily volume (20d) | computed from `price_history[-20:]` volumes | ≥ 500,000 shares |

**Rationale:** Small-cap and illiquid stocks often have incomplete yfinance data (missing fundamentals, thin price history), which produces unreliable agent signals. The filter ensures the pipeline runs on stocks where the data is likely to be complete and meaningful.

**Override:** `--skip-filter` bypasses both checks. Use this when analyzing specific tickers via `--tickers` where you want results regardless of size.

## Dalio Runs Once Per Batch

Dalio's macro analysis is **identical for all tickers** in a single batch because it uses macro tickers (^TNX, ^VIX, TLT, etc.), not individual stock data. Running it once and sharing the result is:
- Correct (the regime doesn't change between stocks)
- Efficient (avoids redundant API calls)
- Consistent (all tickers are scored against the same regime)

If the macro snapshot is fresh (< 6 hours old), it's reloaded from cache without a new fetch.

## Master Dashboard Layout

The output HTML (`output/report.html`) is a fully self-contained interactive page:

| Section | Description |
|---|---|
| Header | MARKET SIGNUM branding + generated timestamp |
| Regime Banner | Current Dalio macro regime with weight pills |
| Summary Bar | Total analyzed, BUY count, WATCH count, AVOID count |
| Filter Bar | Rec filter (All/BUY/WATCH/AVOID) · Sector dropdown · Search |
| Results Table | All stocks sorted by score descending, all columns sortable |
| Expandable Rows | Click any row to reveal full agent breakdown inline |

Sorting is client-side JavaScript — no server required. Default sort: score descending.

## Rate Limiting

yfinance makes HTTP requests to Yahoo Finance. On large batches (800+ tickers), it's common to hit rate limits. Recommended approach:

| Batch size | Recommended delay |
|---|---|
| < 100 tickers | `--delay 0.3` |
| 100–500 tickers | `--delay 0.5` (default) |
| 500+ tickers | `--delay 1.0` |
| Full NYSE (~2,500+) | `--delay 1.5` or run in off-peak hours |

If yfinance returns errors mid-run, the batch continues and errors are logged. Re-run with the same settings — cached results are reused (TTL 6h) and only missing/stale tickers are re-fetched.

## Edge Cases

| Situation | Behavior |
|---|---|
| SEC EDGAR unreachable | Falls back to Wikipedia S&P 500 (~500 tickers, NYSE only) |
| All ticker sources fail | Uses stale `.tmp/nyse_tickers.csv` if it exists; otherwise errors |
| fetch_stock_data returns `{"error": ...}` | Ticker counted as error, batch continues |
| Ticker fails pre-filter | Counted as filtered, no agents run, batch continues |
| Agent raises exception | Error logged, that agent's result is missing from score_and_weight |
| All agents fail for a ticker | score_and_weight returns 0.5 neutral fallback |
| render_html fails | Error printed; .tmp/ cache files still intact for manual re-render |
| Dalio unavailable | Defaults to neutral regime; all tickers scored with neutral weights |

## Typical Run Times

| Scope | Estimated time (delay 0.5s) |
|---|---|
| 50 tickers | ~2–3 minutes |
| 200 tickers | ~8–10 minutes |
| 500 tickers (pre-filtered ~300) | ~20–25 minutes |
| Full NYSE, pre-filtered (~800 pass) | ~45–60 minutes |

Most time is spent in yfinance data fetching, not agent reasoning. The 6-hour cache means a second run within the same session is much faster.

## Manual Re-render

If the batch completed but render_html failed (or you want to change the output format), re-render without re-running agents:

```python
# Not yet implemented as a standalone command — workaround:
# 1. Load all .tmp/<TICKER>_score.json files
# 2. Call render_html() with the assembled result list
```

## CLI Examples

```bash
# Full NYSE scan (recommended: run overnight)
python tools/batch_runner.py --delay 1.5

# Quick test with first 50 tickers from the universe
python tools/batch_runner.py --limit 50

# Analyze specific tickers only (bypass NYSE fetch)
python tools/batch_runner.py --tickers AAPL,MSFT,NVDA,JNJ,WMT

# Force fresh data (ignore all caches)
python tools/batch_runner.py --limit 100 --no-cache

# Test single ticker without pre-filter
python tools/batch_runner.py --tickers GME --skip-filter
```

## Updates
- 2026-03-10: Initial version. Full NYSE batch scan with pre-filter, Dalio once-per-batch, master dashboard.
