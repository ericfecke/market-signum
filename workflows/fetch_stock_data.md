# Workflow: fetch_stock_data

## Objective
Pull all price, fundamental, and technical data for a given ticker and return a single standardized data object. This object is the sole data source for every persona agent in the ensemble.

## Tool
`tools/fetch_stock_data.py`

## Required Input
- `ticker` (string) — Stock symbol, e.g. `"AAPL"`. Case-insensitive.

## Optional Input
- `cache` (bool, default `True`) — If True, returns cached `.tmp/<TICKER>.json` when less than 6 hours old. Pass `False` to force a fresh pull.

## Expected Output
A dict with the following top-level keys:

| Key | Type | Description |
|---|---|---|
| `meta` | dict | Ticker, name, sector, price, exchange, fetch timestamp |
| `fundamentals` | dict | Valuation, quality, balance sheet, growth, ownership metrics |
| `technicals` | dict | RSI, MACD, Bollinger Bands, moving averages, momentum, volume |
| `price_history` | list[dict] | 1 year of daily OHLCV data |
| `missing_fields` | list[str] | Fields yfinance could not return — agents must handle None gracefully |

On hard failure (bad ticker, network error) returns `{"error": "message"}`.

## Execution Steps
1. Call `fetch_stock_data(ticker)` — or run `python tools/fetch_stock_data.py <TICKER>` from CLI.
2. Check for `"error"` key. If present, log and abort.
3. Check `missing_fields`. If critical fields are absent (e.g. `trailingPE` for Graham), note it in that agent's reasoning and reduce confidence.
4. Pass the full data object to Dalio first, then to Graham/Buffett/Lynch/Simons in parallel.

## Field Reference

### meta
```
ticker, name, sector, industry, country, exchange, currency,
price, previous_close, day_change_pct, fetched_at
```

### fundamentals (all percentages stored as % values, e.g. 18.5 = 18.5%)
```
Valuation:  pe_ratio, forward_pe, price_to_book, price_to_sales, peg_ratio, enterprise_to_ebitda
Quality:    return_on_equity, return_on_assets, profit_margin, operating_margin, gross_margin
Balance:    debt_to_equity, current_ratio, quick_ratio, total_cash, total_debt
Cash flow:  free_cash_flow, operating_cash_flow
Growth:     earnings_growth, revenue_growth, earnings_quarterly_growth
Size:       market_cap, enterprise_value, shares_outstanding, float_shares
Ownership:  institutional_ownership, insider_ownership
Dividends:  dividend_yield, payout_ratio
EPS:        eps_trailing, eps_forward
Analyst:    target_mean_price, target_high_price, target_low_price,
            analyst_recommendation, analyst_count
```

### technicals
```
rsi_14                     — RSI(14); >70 overbought, <30 oversold
macd.macd                  — MACD line value
macd.signal                — Signal line value
macd.histogram             — Histogram (macd - signal)
macd.crossover             — "bullish" | "bearish"
bollinger_bands.upper/middle/lower  — Band values ($)
bollinger_bands.position   — 0.0 (at lower) to 1.0 (at upper)
momentum.30d / 60d / 90d   — % price change over each window
ma_50, ma_200              — Simple moving averages ($)
price_vs_ma50, price_vs_ma200  — % above/below each MA
ma_cross                   — "golden" (MA50>MA200) | "death" (MA50<MA200)
volume_ratio_20d           — Current vol / 20-day avg vol
52w_high, 52w_low          — 52-week range
52w_pct_from_high          — % below 52-week high (negative)
52w_pct_from_low           — % above 52-week low (positive)
```

## Edge Cases & Degradation

| Situation | Behavior |
|---|---|
| Invalid ticker | Returns `{"error": "..."}`. Abort the run. |
| Field missing from yfinance | Field set to `None`, key added to `missing_fields`. Agents skip that metric and note it in reasoning. |
| < 50 days of history | `ma_50` is None. Simons should flag confidence as low. |
| < 200 days of history | `ma_200` is None. MA cross unavailable. |
| ETF / no fundamentals | Most fundamentals will be None. Graham/Buffett will have limited signal. |
| Stale market data (weekend/holiday) | yfinance returns last available close. This is expected behavior. |

## Cache Behavior
- Cached files stored at `.tmp/<TICKER>.json`
- Cache expires after 6 hours
- Force fresh data: `--no-cache` flag or `cache=False` argument
- Cache is regenerated automatically on expiry

## Known yfinance Quirks
- `debtToEquity` is returned as a raw ratio (not a percentage). A value of 0.72 means D/E = 0.72. Graham's threshold of 0.5 applies directly.
- `returnOnEquity`, `profitMargins`, etc. are raw decimals in yfinance. This tool multiplies by 100 before storing — all percentage fields in the output are in `%` units.
- Some fields (e.g. `pegRatio`, `freeCashflow`) are frequently missing for small-caps or non-US stocks.
- `institutionPercent` and `insiderPercent` are raw decimals; stored as %.

## Updates
- 2026-03-10: Initial version. All fields documented. Graceful degradation implemented.
