# Workflow: detect_macro_regime

## Objective
Determine the current macroeconomic regime before any stock-specific agents run. The `regime_flag` returned here is the input that `score_and_weight.py` uses to re-scale the weights of every other agent in the ensemble.

## Tool
`tools/dalio_agent.py`

## Execution Order
**Always run this first.** Graham, Buffett, Lynch, and Simons can run in parallel after this returns. Without a `regime_flag`, `score_and_weight.py` defaults to neutral weights.

## Required Input
- `ticker` (string) — The stock symbol being analyzed. Not used for macro scoring, but provides context for the AI-generated reasoning text.
- `stock_data` (dict, optional) — Output from `fetch_stock_data.py`. Not used in scoring currently; reserved for future context enrichment.

## Expected Output

```json
{
  "agent":       "dalio",
  "ticker":      "AAPL",
  "signal":      "watch",
  "confidence":  0.72,
  "reasoning":   "The machine is tightening — the yield curve remains inverted...",
  "regime_flag": "risk-off",
  "macro_scores": {
    "rates":         -0.450,
    "inflation":     -0.200,
    "risk_appetite":  0.150,
    "debt_cycle":    -0.200
  },
  "macro_snapshot": { ... raw macro indicator values ... },
  "run_at": "2026-03-10T09:32:11"
}
```

## Macro Data Sources (all via yfinance, no API key required)

| Label | Symbol | Measures |
|---|---|---|
| `yield_10y` | `^TNX` | 10-year Treasury yield (rate trajectory) |
| `yield_short` | `^IRX` | 13-week T-bill (short-rate proxy for yield curve) |
| `vix` | `^VIX` | CBOE Volatility Index (fear / risk appetite) |
| `sp500` | `^GSPC` | S&P 500 (broad market trend) |
| `tlt` | `TLT` | Long-term Treasury bond ETF (debt cycle proxy) |
| `gld` | `GLD` | Gold ETF (inflation / safe-haven demand) |
| `uup` | `UUP` | US Dollar Index ETF (currency strength) |
| `hyg` | `HYG` | High-yield bond ETF (credit spreads / risk appetite) |

Data: 6 months of daily closes. Cached at `.tmp/_macro_snapshot.json` (6h TTL).

## Scoring: Four Dimensions

Each dimension returns a score in **[-1.0, +1.0]**:
- `+1.0` = strongly risk-on
- `-1.0` = strongly risk-off
- `0.0`  = neutral

### 1. Rates (weight: 30%)
| Signal | Score |
|---|---|
| 10Y yield surged > 12% in 1m | -0.60 |
| 10Y yield rising > 5% in 1m | -0.30 |
| 10Y yield falling < -4% in 1m | +0.25 |
| Yield curve deeply inverted (< -0.75%) | -0.50 |
| Yield curve inverted (< -0.25%) | -0.25 |
| Yield curve steep (> 1.50%) | +0.20 |

### 2. Inflation (weight: 20%)
| Signal | Score |
|---|---|
| Gold +12% in 3m | -0.45 |
| Gold +5% in 3m | -0.20 |
| Gold -8% in 3m | +0.25 |
| USD +6% in 3m | +0.30 |
| USD -6% in 3m | -0.30 |

### 3. Risk Appetite (weight: 35%)
| Signal | Score |
|---|---|
| VIX > 40 | -0.90 |
| VIX > 30 | -0.60 |
| VIX > 22 | -0.25 |
| VIX < 13 | +0.40 |
| HYG -4% in 1m (spreads widening) | -0.45 |
| HYG +2% in 1m (spreads tight) | +0.20 |
| S&P >12% below 50MA | -0.40 |
| S&P >5% above 50MA | +0.25 |

### 4. Debt Cycle (weight: 15%)
| Signal | Score |
|---|---|
| TLT +10% in 3m (bond rally) | +0.30 |
| TLT -10% in 3m (bond selloff) | -0.45 |
| TLT rally + VIX > 28 (flight-to-safety offset) | -0.15 |

## Regime Determination

```
composite = Σ (dimension_score × dimension_weight)

deleveraging : VIX > 38 OR 3+ dimensions score ≤ -0.45
risk-on      : composite ≥ +0.18
risk-off     : composite ≤ -0.18
neutral      : -0.18 < composite < +0.18
```

## Regime → Weight Adjustments (applied by score_and_weight.py)

| Regime | Graham | Buffett | Dalio | Lynch | Simons |
|---|---|---|---|---|---|
| neutral | 15% | 25% | 20% | 20% | 20% |
| risk-on | 5% | 25% | 20% | 40% | 30% |
| risk-off | 35% | 35% | 20% | 5% | 15% |
| deleveraging | Graham +30% base, Dalio veto on buys |

## Signal Mapping

| regime_flag | signal | Rationale |
|---|---|---|
| risk-on | buy | Macro tailwinds broadly favor equities |
| neutral | watch | No strong macro directional bias |
| risk-off | watch | Headwinds — other agents carry more weight |
| deleveraging | avoid | Capital preservation mode |

## Confidence Calibration

| Regime | Formula | Range |
|---|---|---|
| deleveraging | 0.65 + \|composite\| × 0.40 | capped at 0.92 |
| risk-on | 0.52 + composite × 0.70 | capped at 0.90 |
| risk-off | 0.52 + \|composite\| × 0.70 | capped at 0.90 |
| neutral | 0.68 − \|composite\| × 0.60 | floored at 0.38 |

## LLM Reasoning

If `ANTHROPIC_API_KEY` is set and the `anthropic` package is installed, the agent calls Claude (`claude-sonnet-4-6`) to generate a Dalio-voice 2–3 sentence reasoning summary. If the key is absent or the call fails, a rule-based fallback reasoning is used — output format is identical in both cases.

## Edge Cases & Degradation

| Situation | Behavior |
|---|---|
| yfinance fails to return a macro ticker | That indicator set to `None`; dimension still scored (at 0 from missing signals) |
| VIX data unavailable | Regime cannot trigger deleveraging via VIX threshold; multi-dimension check still applies |
| All dimensions return 0 | Regime = neutral, confidence ≈ 0.68 |
| ANTHROPIC_API_KEY not set | Fallback reasoning used; signal and regime are unaffected |

## Cache Behavior
- Macro snapshot: `.tmp/_macro_snapshot.json` — 6h TTL (shared across all tickers in a session)
- Per-ticker output: `.tmp/<TICKER>_dalio.json`
- Force fresh pull: `--no-cache` flag or `cache=False`

## Updates
- 2026-03-10: Initial version. Four-dimension scoring, deleveraging override, LLM reasoning with fallback.
