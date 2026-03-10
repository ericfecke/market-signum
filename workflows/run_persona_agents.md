# Workflow: run_persona_agents

## Objective
Run all four stock-specific persona agents against a ticker once Dalio's `regime_flag` has been established. These agents can run in parallel — none depends on the others.

## Tools (run concurrently after Dalio returns)
- `tools/graham_agent.py`
- `tools/buffett_agent.py`
- `tools/lynch_agent.py`
- `tools/simons_agent.py`

## Prerequisite
`detect_macro_regime` must have completed and returned a `regime_flag`. Without it, `score_and_weight.py` defaults to neutral weights.

## Required Input (all four agents)
- `ticker` (string)
- `stock_data` (dict) — output from `fetch_stock_data.py`

## Execution Pattern
```python
# Run all four in parallel after Dalio completes
from tools.graham_agent  import run_graham_agent
from tools.buffett_agent import run_buffett_agent
from tools.lynch_agent   import run_lynch_agent
from tools.simons_agent  import run_simons_agent

graham  = run_graham_agent(ticker,  stock_data)
buffett = run_buffett_agent(ticker, stock_data)
lynch   = run_lynch_agent(ticker,   stock_data)
simons  = run_simons_agent(ticker,  stock_data)
```

## Output Contract (all four agents)
```json
{
  "agent":            "graham" | "buffett" | "lynch" | "simons",
  "ticker":           "AAPL",
  "signal":           "buy" | "watch" | "avoid",
  "confidence":       0.0–1.0,
  "reasoning":        "Agent-voice explanation of the signal",
  "dimension_scores": { ... agent-specific dimensions ... },
  "missing_fields":   [ "field_name", ... ],
  "run_at":           "2026-03-10T09:32:11"
}
```

Per-ticker output cached to `.tmp/<TICKER>_<agent>.json`.

---

## Agent Details

### Graham — Valuation Gatekeeper
**Philosophy:** Margin of safety above all. Never pays more than intrinsic value.

**Scoring dimensions:**
| Dimension | Weight | Metrics |
|---|---|---|
| `valuation` | 40% | P/E (pref < 15), Price-to-Book (pref < 1.5) |
| `balance_sheet` | 35% | D/E (flags > 0.5), Current Ratio (pref > 2.0) |
| `earnings_power` | 25% | EPS sign, Net Profit Margin |

**Signal thresholds (strict):**
| Score | Signal |
|---|---|
| ≥ 0.30 | buy |
| ≥ 0.05 | watch |
| < 0.05 | avoid |

**Personality:** Skeptical by default. Graham rarely says buy. When he does, the stock is genuinely cheap. If he passes, other agents carry more weight.

---

### Buffett — Quality & Moat Analysis
**Philosophy:** A wonderful company at a fair price beats a fair company at a wonderful price.

**Scoring dimensions:**
| Dimension | Weight | Metrics |
|---|---|---|
| `business_quality` | 40% | ROE (pref > 15%), Net Profit Margin |
| `moat_strength` | 30% | Gross Margin (> 50% = exceptional moat), Operating Margin |
| `cash_generation` | 20% | FCF sign + FCF yield vs market cap |
| `fair_value` | 10% | P/E (lenient — pays up for quality, avoids > 50) |

**Signal thresholds:**
| Score | Signal |
|---|---|
| ≥ 0.30 | buy |
| ≥ 0.05 | watch |
| < 0.05 | avoid |

**Key insight:** Gross margin > 50% is the strongest moat signal available from yfinance data (proxies for pricing power and switching costs).

---

### Lynch — Growth & Momentum Screening
**Philosophy:** Find growth before institutions do. PEG < 1.0 is almost always a buy.

**Scoring dimensions:**
| Dimension | Weight | Metrics |
|---|---|---|
| `growth` | 35% | Earnings growth (pref 15–30%), Revenue growth |
| `peg_value` | 30% | PEG ratio (strong buy < 0.5, buy < 1.0) |
| `discovery` | 20% | Institutional ownership (lower = more upside), Insider ownership |
| `momentum` | 15% | 30d price momentum as confirmation |

**Signal thresholds (optimistic):**
| Score | Signal |
|---|---|
| ≥ 0.20 | buy |
| ≥ -0.10 | watch |
| < -0.10 | avoid |

**Personality:** Most optimistic in the ensemble. Will flag a stock early. Counterbalanced by Graham and Dalio in risk-off environments via dynamic weighting.

---

### Simons — Quant Signals & Pattern Recognition
**Philosophy:** The market has patterns. Find them with math, not narrative.

**Scoring dimensions:**
| Dimension | Weight | Metrics |
|---|---|---|
| `momentum` | 30% | 30d (50%), 60d (30%), 90d (20%) price momentum |
| `oscillators` | 30% | RSI(14), MACD crossover + histogram |
| `trend_structure` | 25% | MA cross (golden/death), price vs MA50, price vs MA200 |
| `volume_bb` | 15% | Volume ratio 20d, Bollinger Band position |

**Signal thresholds (symmetric):**
| Score | Signal |
|---|---|
| ≥ 0.25 | buy |
| ≥ -0.25 | watch |
| < -0.25 | avoid |

**Key insight:** RSI 55–68 is Simons' "momentum zone" — confirmed uptrend without extreme overbought risk. MACD histogram magnitude provides conviction weight beyond just crossover direction.

**Divergence detection:** If Simons and Lynch disagree (opposite signals), flag the discrepancy in the final report — it means technical and fundamental momentum are misaligned.

---

## Confidence Calibration (all agents)
```
base    = 0.50 + |composite_score| × 0.42
penalty = min(0.25, missing_field_count × 0.04)
confidence = clamp(base - penalty, 0.25, 0.92)
```

Missing data always reduces confidence. The signal itself is unaffected — agents degrade gracefully by skipping the metric and noting it in `missing_fields`.

## LLM Reasoning (all agents)
If `ANTHROPIC_API_KEY` is set and `anthropic` package is installed, each agent calls Claude (`claude-sonnet-4-6`) to generate a persona-voiced 2–3 sentence reasoning summary.

If the key is absent or the API call fails, a rule-based fallback reasoning fires automatically. **The signal and confidence are identical in both cases** — the LLM only generates the human-readable text.

## Edge Cases & Degradation

| Situation | Behavior |
|---|---|
| `stock_data["error"]` is set | Returns `signal: "avoid"`, `confidence: 0.25`, logs error in reasoning |
| P/E is None (e.g. unprofitable company) | Graham/Buffett score that dimension 0; field added to `missing_fields` |
| PEG is None | Lynch skips PEG dimension; confidence penalized |
| Technical data missing | Simons scores 0 on that sub-dimension; rare since indicators are computed from price history |
| ETF / no fundamentals | Graham and Buffett will have mostly null fundamentals and low confidence; Simons unaffected |

## Updates
- 2026-03-10: Initial version. All four agents implemented with scoring, LLM reasoning, fallback, and graceful degradation.
