# MARKET SIGNUM — Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This project builds a multi-agent stock analysis system that operates in two modes: **single-ticker analysis** for on-demand deep dives, and **NYSE batch scanning** to rank the entire exchange by MARKET SIGNUM score. Each mode uses the same five-agent ensemble and scoring logic — only the orchestration and output rendering differ.

Probabilistic AI handles reasoning and signal generation. Deterministic code handles data retrieval, scoring, pre-filtering, and HTML output. That separation is what keeps the system reliable at scale.

---

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and edge case handling
- Workflows:
  - `workflows/fetch_stock_data.md` — pull price, fundamentals, and technicals via yfinance
  - `workflows/detect_macro_regime.md` — run Dalio macro analysis and return regime flag
  - `workflows/run_persona_agents.md` — execute each persona agent against a ticker
  - `workflows/score_and_weight.md` — aggregate signals, apply dynamic weights, produce final score
  - `workflows/render_html_output.md` — write final analysis to a clean HTML dashboard
  - `workflows/batch_scan.md` — orchestrate a full NYSE batch run with pre-filtering

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You coordinate the ensemble, run tools in sequence, and synthesize signals into a final recommendation.
- You do not fetch data directly. You do not render HTML directly. You read the relevant workflow, gather required inputs, and execute the appropriate tool.
- In single-ticker mode: run all five agents, score, render a per-ticker report.
- In batch mode: delegate orchestration to `batch_runner.py`, which handles pre-filtering and per-ticker pipelines autonomously. Your role is to initiate the run and interpret the master dashboard output.

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` that do the actual work
- `tools/fetch_nyse_tickers.py` — pulls the current list of NYSE-listed stocks; returns ticker symbols with basic metadata for pre-filtering
- `tools/fetch_stock_data.py` — pulls price history, fundamentals, and technicals via yfinance for a single ticker
- `tools/dalio_agent.py` — Ray Dalio macro regime detection; **run once per batch** (output is macro-level, not ticker-specific)
- `tools/graham_agent.py` — Benjamin Graham valuation analysis
- `tools/buffett_agent.py` — Warren Buffett quality + moat analysis
- `tools/lynch_agent.py` — Peter Lynch growth + momentum screening
- `tools/simons_agent.py` — Jim Simons quant signals + pattern recognition
- `tools/score_and_weight.py` — aggregates signals, applies Dalio regime weighting, outputs final score
- `tools/batch_runner.py` — orchestrates a full NYSE scan: fetches tickers, applies pre-filter, runs the full pipeline per stock, calls the renderer when complete
- `tools/render_html.py` — renders the master dashboard (`output/report.html`): all analyzed stocks ranked by score in a sortable table with expandable per-stock breakdowns
- API keys and credentials stored in `.env`

---

## The Five Persona Agents

Each agent receives the same stock data object and returns a standardized signal object: `{ signal, confidence, reasoning }`.

### Benjamin Graham — Valuation Gatekeeper
**Philosophy:** Margin of safety above all else. A stock is only worth buying when it trades significantly below intrinsic value.

**What he evaluates:**
- P/E ratio vs. market average (prefers below 15)
- Price-to-book ratio (prefers under 1.5)
- Debt-to-equity (prefers low, flags anything above 0.5)
- Consistent earnings over 10 years
- Current ratio above 2 (financial stability)

**Signal behavior:** Skeptical by default. Graham rarely says buy. He functions as a valuation floor — if he flags a stock, it's genuinely cheap. If he passes, the other agents carry more weight. In a batch scan, a Graham "buy" is a rare, meaningful signal worth surfacing.

**Output:** `{ signal: "buy" | "watch" | "avoid", confidence: 0–1, reasoning: string }`

> **Quick Reference — Benjamin Graham**
> | | |
> |---|---|
> | **Philosophy** | Buy only when price is significantly below intrinsic value — margin of safety above all else. |
> | **Key metrics** | P/E ratio · Price-to-book · Debt/Equity · Current ratio |
> | **🟢 BUY when** | P/E ≤ 10, P/B ≤ 1.0, D/E < 0.5, current ratio ≥ 2, earnings stable over 10 years |
> | **🔴 AVOID when** | P/E > 20, P/B > 2.0, D/E ≥ 0.5, current ratio < 1.5, or earnings history erratic |

---

### Warren Buffett — Quality & Moat Analysis
**Philosophy:** A wonderful company at a fair price beats a fair company at a wonderful price.

**What he evaluates:**
- Return on equity (prefers above 15% consistently)
- Profit margins vs. industry peers
- Evidence of durable competitive advantage (brand, switching costs, cost advantage)
- Free cash flow generation
- Management quality signals (buybacks, capital allocation history)
- Reasonable P/E given quality (willing to pay more than Graham for quality)

**Signal behavior:** More willing to buy than Graham. Looks for compounders. Will flag a stock even at fair value if the moat is strong.

**Output:** `{ signal: "buy" | "watch" | "avoid", confidence: 0–1, reasoning: string }`

> **Quick Reference — Warren Buffett**
> | | |
> |---|---|
> | **Philosophy** | A wonderful company at a fair price — durable moat and capital returns matter more than cheapness. |
> | **Key metrics** | Return on equity · Profit margin · Free cash flow · Moat evidence |
> | **🟢 BUY when** | ROE > 15% consistently, margins above peers, clear moat (brand/switching costs/cost advantage), FCF positive |
> | **🔴 AVOID when** | ROE < 10%, margins thin or shrinking, no competitive advantage, weak or negative free cash flow |

---

### Ray Dalio — Macro Regime Detection
**Philosophy:** Understand the machine. The economic cycle determines which assets win, not individual stock picking alone.

**What he evaluates:**
- Current debt cycle position (early, mid, late, deleveraging)
- Inflation environment (rising, stable, falling)
- Interest rate trajectory (tightening, neutral, easing)
- Currency strength signals
- Whether the current regime favors equities broadly

**Special role — Dynamic Weight Modifier:**
Dalio's output does not produce a signal for an individual stock. It returns a `regime_flag` that adjusts weights across the entire ensemble for every ticker in the run. **In batch mode, Dalio runs exactly once** — the macro snapshot and regime_flag are cached and shared across all tickers.

| Regime | Effect on Weights |
|---|---|
| Risk-on (easing, early cycle) | Lynch +20%, Simons +10%, Graham -10%, Buffett neutral |
| Neutral | Base weights unchanged |
| Risk-off (tightening, late cycle) | Graham +20%, Buffett +10%, Lynch -15%, Simons -5% |
| Deleveraging | Graham +30%, Dalio veto power on buys |

**Output:** `{ signal: "buy" | "watch" | "avoid", confidence: 0–1, reasoning: string, regime_flag: "risk-on" | "neutral" | "risk-off" | "deleveraging" }`

> **Quick Reference — Ray Dalio**
> | | |
> |---|---|
> | **Philosophy** | Understand the machine — the debt/economic cycle determines which assets win, not stock picking alone. |
> | **Key indicators** | 10Y yield trend · VIX · Credit spreads (TLT) · Inflation proxies · Debt cycle position |
> | **🟢 Risk-on** | Easing rates, low/falling VIX, early-to-mid debt cycle → boosts Lynch (+20%) and Simons (+10%) weights |
> | **🔴 Risk-off / Deleveraging** | Tightening, rising VIX, late cycle → boosts Graham (+20–30%) and Buffett (+10%); deleveraging vetoes BUY calls |

---

### Peter Lynch — Growth & Momentum Screening
**Philosophy:** Invest in what you understand. Find growth before the institutions do.

**What he evaluates:**
- PEG ratio (price/earnings to growth — prefers under 1.0)
- Earnings growth rate (looks for 15–30% annual growth)
- Whether the business is understandable and consumer-facing
- Institutional ownership percentage (lower = more upside if he's early)
- Revenue growth consistency
- Insider buying signals

**Signal behavior:** Most optimistic of the ensemble. Most likely to flag a rising stock early. Counterbalanced by Graham and Dalio in risk-off environments.

**Output:** `{ signal: "buy" | "watch" | "avoid", confidence: 0–1, reasoning: string }`

> **Quick Reference — Peter Lynch**
> | | |
> |---|---|
> | **Philosophy** | Find growth before the institutions do — understand the business, trust the earnings trajectory. |
> | **Key metrics** | PEG ratio · Earnings growth rate · Institutional ownership % · Insider activity |
> | **🟢 BUY when** | PEG ≤ 1.0, earnings growing 15–30% YoY, low institutional ownership, insiders buying |
> | **🔴 AVOID when** | PEG > 2.0, earnings growth stalling or negative, fully institutionally owned, insiders selling |

---

### Jim Simons — Quant Signals & Pattern Recognition
**Philosophy:** The market has patterns. Find them with math, not narrative.

**What he evaluates:**
- RSI (relative strength index — flags overbought/oversold)
- MACD crossover signals
- 50-day and 200-day moving average positioning
- Volume trend vs. price trend divergence
- Bollinger band positioning
- Statistical momentum over 30/60/90-day windows

**Signal behavior:** No opinion on the business. Purely statistical. Acts as a pattern-based counterweight to narrative-heavy agents. If Simons and Lynch both say buy, momentum is confirmed. If they diverge, flag for review.

**Output:** `{ signal: "buy" | "watch" | "avoid", confidence: 0–1, reasoning: string }`

> **Quick Reference — Jim Simons**
> | | |
> |---|---|
> | **Philosophy** | The market has patterns — find them with math, not narrative. No opinions on the business. |
> | **Key metrics** | RSI · MACD crossover · 50d/200d MA cross · 30/60/90d momentum |
> | **🟢 BUY when** | RSI 40–65 trending up, bullish MACD crossover, golden cross (50d > 200d), positive momentum across windows |
> | **🔴 AVOID when** | RSI > 75 (overbought) or < 25 with no reversal, bearish MACD, death cross (50d < 200d), negative multi-window momentum |

---

## Dynamic Weighting Logic

Base weights (neutral regime):
- Graham: 15%
- Buffett: 25%
- Dalio: 20%
- Lynch: 20%
- Simons: 20%

Dalio's `regime_flag` shifts weights at runtime per the table above. `tools/score_and_weight.py` handles this calculation. Weights are further modulated by each agent's confidence — a low-confidence agent (sparse data, many missing fields) counts proportionally less.

**Final score thresholds:**
- 0.70–1.0 → **BUY** (strong consensus)
- 0.50–0.69 → **WATCH** (mixed signals, monitor)
- 0.00–0.49 → **AVOID** (weak or negative consensus)

---

## Batch Scanner: fetch_nyse_tickers.py

**Purpose:** Retrieve the current universe of NYSE-listed stocks and return a list of ticker symbols for the batch runner to process.

**Data source:** Pulls from a combination of sources in priority order:
1. SEC EDGAR company tickers JSON (`https://www.sec.gov/files/company_tickers.json`) — filtered to NYSE exchange
2. Wikipedia NYSE Composite constituent list (fallback)
3. A local cached CSV at `.tmp/nyse_tickers.csv` if the above sources are unreachable

**Output:** A list of dicts: `[ { ticker, name, exchange, sector }, ... ]`

**Cache:** Results are written to `.tmp/nyse_tickers.csv`. Cache TTL is 24 hours (ticker list changes slowly).

---

## Batch Scanner: batch_runner.py

**Purpose:** Orchestrate a full NYSE scan. For every ticker that passes the pre-filter, run the complete five-agent pipeline and collect results for the dashboard.

### Pre-filter (applied before running any agents)

Both conditions must be met to proceed to full analysis:

| Filter | Threshold | Field |
|---|---|---|
| Market cap | ≥ $500M | `fundamentals.market_cap` |
| Avg volume (20d) | ≥ 500,000 shares | computed from `technicals.volume_ratio_20d` × yfinance avg vol |

The pre-filter uses a **lightweight data pull** — `fetch_stock_data()` is called, which populates the cache. If the stock passes, the cached data is used immediately by the agents. If it fails, the ticker is skipped and logged; no agent time is wasted.

### Batch run sequence

```
1. fetch_nyse_tickers()          → ticker universe
2. run_dalio_agent()             → regime_flag (once, cached for entire batch)
3. For each ticker in universe:
     a. fetch_stock_data(ticker)  → stock data (lightweight pull)
     b. pre_filter(stock_data)    → pass / skip
     c. [if pass] run graham, buffett, lynch, simons in sequence
     d. score_and_weight()        → final score
     e. write .tmp/<TICKER>_score.json
4. render_html()                 → output/report.html (master dashboard)
```

### Error handling
- Per-ticker errors (bad data, yfinance timeout, missing fields) are caught and logged; the batch continues
- Tickers with `fetch_stock_data()` returning `{"error": ...}` are counted as skipped, not failures
- A run summary is printed on completion: `N tickers fetched → M passed filter → K scored → report written`

### Rate limiting
- yfinance requests are throttled with a configurable delay between tickers (default: 0.5s)
- Configurable via `BATCH_DELAY_SECONDS` environment variable or CLI flag `--delay`

### CLI
```
python tools/batch_runner.py                   # full NYSE scan
python tools/batch_runner.py --limit 100       # test run, first 100 tickers
python tools/batch_runner.py --tickers AAPL,MSFT,NVDA  # specific list
python tools/batch_runner.py --delay 1.0       # slower rate to avoid yfinance throttling
```

---

## HTML Output Spec — Master Dashboard

Final output renders to `output/report.html`. The `tools/render_html.py` script handles all rendering. In batch mode this is a single master dashboard covering every analyzed stock; in single-ticker mode it renders a focused per-stock view within the same template.

### Master Dashboard Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  MARKET SIGNUM                            Generated: 2026-03-10 │
│  NYSE Batch Scan                                                │
├─────────────────────────────────────────────────────────────────┤
│  DALIO MACRO REGIME BANNER                                      │
│  Current regime · Regime reasoning · Active weight shifts       │
├─────────────────────────────────────────────────────────────────┤
│  SUMMARY BAR                                                    │
│  Tickers analyzed: N  │  🟢 BUY: X  │  🟡 WATCH: Y  │  🔴 AVOID: Z │
├─────────────────────────────────────────────────────────────────┤
│  FILTER BAR                                                     │
│  [All / BUY / WATCH / AVOID]  [All Sectors ▾]  [Search... 🔍]  │
├─────────────────────────────────────────────────────────────────┤
│  RANKED RESULTS TABLE (sortable by any column)                  │
│                                                                 │
│  #  Ticker  Score  Rec    G   Bu   Ly   Si  Regime  Sector  Cap │
│  ─────────────────────────────────────────────────────────────  │
│  1  AAPL    0.821  🟢BUY  🟢  🟢  🟢  🟡  ⚖️ Neu  Tech   3.1T │
│  ▼  [expanded row — full agent breakdown, reasoning, dim bars]  │
│  2  MSFT    0.789  🟢BUY  🟡  🟢  🟢  🟢  ⚖️ Neu  Tech   2.8T │
│  3  JNJ     0.731  🟢BUY  🟢  🟢  🟡  🟡  ⚖️ Neu  Health 380B │
│  ...                                                            │
└─────────────────────────────────────────────────────────────────┘
```

### Table Columns

| Column | Description | Sortable |
|---|---|---|
| # | Rank by score (descending) | — |
| Ticker | Stock symbol | ✓ (A–Z) |
| Score | Final weighted score 0–1 | ✓ |
| Rec | BUY / WATCH / AVOID badge | ✓ |
| G | Graham signal badge | ✓ |
| Bu | Buffett signal badge | ✓ |
| Ly | Lynch signal badge | ✓ |
| Si | Simons signal badge | ✓ |
| Regime | Dalio regime icon | ✓ |
| Sector | Company sector | ✓ (A–Z) |
| Price | Current price | ✓ |
| Mkt Cap | Market capitalization | ✓ |

Sorting is client-side JavaScript — no server required. Default sort: Score descending.

### Expandable Rows

Clicking any table row expands it to reveal the full per-stock breakdown inline:

```
▼ AAPL — Apple Inc.                                Score: 0.821  🟢 BUY

  [Dalio Regime Banner — same macro context for all rows]

  [Agent Cards — Graham | Buffett | Lynch | Simons]
    Each card: signal badge · confidence bar · reasoning text · dimension score bars

  [Simons Quant Indicator Grid]
    RSI · MACD · MA Cross · Momentum · Bollinger · Volume

  [Score Bar]
    [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░▼░░]
     AVOID                  WATCH           BUY

  [Consensus: Graham ✓  Buffett ✓  Lynch ✓  |  Simons ⚡]
```

Only one row expands at a time. Clicking the same row collapses it. Clicking a different row switches the expansion.

### Filter Bar Behavior
- **Recommendation filter:** shows only rows matching the selected signal (BUY / WATCH / AVOID / All)
- **Sector filter:** dropdown of all sectors present in the result set
- **Search:** filters ticker symbol and company name (client-side, instant)
- All filters compose — e.g., BUY + Technology shows only tech stocks with BUY signal
- Active filter state is reflected in the summary bar (e.g., "Showing 12 of 847 stocks")

### Design Rules
- Dark background, clean table layout
- Green = buy, yellow = watch, red = avoid — consistent across all elements
- Score column shows a mini score bar (color gradient) in addition to the number
- Agent signal columns use compact colored dots/badges that don't wrap
- Expanded rows inherit the same card design as the single-ticker report
- No external CDN dependencies — fully self-contained HTML
- Renders in Chrome, Firefox, Safari, Edge without JavaScript frameworks

---

## How to Operate

### Single-Ticker Mode
```
1. fetch_stock_data(ticker)
2. run_dalio_agent(ticker, stock_data)        ← run first
3. run_graham/buffett/lynch/simons in parallel
4. score_and_weight(all_results)
5. render_html(ticker)                        → output/report.html
```

### Batch Scanner Mode
```
1. python tools/batch_runner.py               ← handles everything below automatically
   a. fetch_nyse_tickers()
   b. run_dalio_agent() once (regime cached)
   c. For each ticker: fetch → pre-filter → agents → score
   d. render_html()                           → output/report.html (master dashboard)
```

### Operational Rules
- **Always run Dalio first.** In batch mode, `batch_runner.py` enforces this. In manual mode, run `dalio_agent.py` before any other agent.
- **Never let agents pull their own data.** All stock data flows from `fetch_stock_data.py`. Agents receive a pre-built data object.
- **Pre-filter before running agents.** Don't waste pipeline time on stocks that don't meet the minimum market cap / volume threshold.
- **Degrade gracefully.** Missing yfinance fields reduce confidence but don't abort the run. Log to `missing_fields` and continue.
- **One Dalio run per batch.** The macro snapshot is shared. Running Dalio per ticker wastes time and produces identical results.
- **Cache aggressively.** yfinance data is cached 6h in `.tmp/`. NYSE ticker list is cached 24h. Reuse between runs within the TTL.

---

## File Structure

```
market-signum/
│
├── tools/
│   ├── fetch_nyse_tickers.py    # pulls NYSE universe; outputs .tmp/nyse_tickers.csv
│   ├── fetch_stock_data.py      # per-ticker data pull; outputs .tmp/<TICKER>.json
│   ├── dalio_agent.py           # macro regime; outputs .tmp/_macro_snapshot.json
│   ├── graham_agent.py          # valuation; outputs .tmp/<TICKER>_graham.json
│   ├── buffett_agent.py         # quality+moat; outputs .tmp/<TICKER>_buffett.json
│   ├── lynch_agent.py           # growth; outputs .tmp/<TICKER>_lynch.json
│   ├── simons_agent.py          # quant; outputs .tmp/<TICKER>_simons.json
│   ├── score_and_weight.py      # aggregator; outputs .tmp/<TICKER>_score.json
│   ├── batch_runner.py          # orchestrator; calls all of the above in sequence
│   └── render_html.py           # master dashboard renderer; writes output/report.html
│
├── workflows/
│   ├── fetch_stock_data.md
│   ├── detect_macro_regime.md
│   ├── run_persona_agents.md
│   ├── score_and_weight.md
│   ├── render_html_output.md
│   └── batch_scan.md            # SOP for full NYSE batch run
│
├── output/
│   └── report.html              # master dashboard (overwritten on each run)
│
├── .tmp/                        # auto-generated cache; never commit
│   ├── nyse_tickers.csv         # NYSE universe (24h TTL)
│   ├── _macro_snapshot.json     # Dalio macro data (6h TTL)
│   ├── <TICKER>.json            # per-ticker stock data (6h TTL)
│   ├── <TICKER>_dalio.json      # per-ticker Dalio result
│   ├── <TICKER>_graham.json
│   ├── <TICKER>_buffett.json
│   ├── <TICKER>_lynch.json
│   ├── <TICKER>_simons.json
│   └── <TICKER>_score.json      # final score per ticker
│
├── .env                         # ANTHROPIC_API_KEY (optional, for LLM reasoning)
├── .gitignore
├── CLAUDE.md                    # this file — agent instructions
└── CLAUDE_portfolio_indicator.md  # original spec, retained for reference
```

---

## Dependencies

Install all required packages before running any tool:

```bash
pip install -r requirements.txt
```

### Package Summary

| Package | Used by | Notes |
|---|---|---|
| `yfinance` | `fetch_stock_data.py`, `dalio_agent.py` | Primary market data source — no API key required |
| `pandas` | `fetch_stock_data.py`, `fetch_nyse_tickers.py` | Data processing and Wikipedia ticker fallback |
| `requests` | `fetch_nyse_tickers.py` | HTTP calls to SEC EDGAR, Wikipedia, and NASDAQ Trader |
| `numpy` | indirect | Required by yfinance and pandas; not directly imported |
| `anthropic` | all agent files | **Optional** — LLM reasoning text; agents fall back to rule-based if key absent |

### LLM Reasoning (Optional)

Add `ANTHROPIC_API_KEY` to `.env` to enable AI-generated reasoning narratives in agent outputs. If the key is absent or the API call fails, all agents fall back to deterministic rule-based reasoning automatically. Signals and confidence scores are **never** affected by the LLM call.

### Standard Library (no install needed)

`json`, `csv`, `io`, `html`, `sys`, `os`, `time`, `argparse`, `pathlib`, `datetime` — all stdlib, included with Python.

---

## Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke (data gap, bad signal, pre-filter too aggressive, rendering issue)
2. Fix the tool
3. Verify the fix works on a known ticker (`python tools/fetch_stock_data.py AAPL`)
4. Update the relevant workflow markdown with the new approach
5. Move on with a more robust system

If a persona agent is consistently wrong in a specific market regime, adjust its weighting logic or metric thresholds and document the change in the workflow file.

If the pre-filter is excluding stocks that should be analyzed (e.g., a known mid-cap with thin yfinance data), add a `--force` flag to `batch_runner.py` that bypasses the filter for specific tickers.

---

## Bottom Line

You coordinate five distinct investment minds and one quant model across a universe of NYSE stocks. Your job is:

**Single-ticker:** fetch → Dalio → agents → score → render. Clear signal in under 60 seconds.

**Batch mode:** kick off `batch_runner.py`, let it run, then interpret the master dashboard. The ranked table tells you where the consensus opportunities are right now, under the current macro regime.

No narrative fluff. No hedging for the sake of hedging. Each agent gives a signal, the math aggregates it, the output shows it clearly — whether for one stock or eight hundred.

Stay pragmatic. Stay reliable. Keep learning.
