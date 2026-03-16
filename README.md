# MARKET SIGNUM

**Five investment minds. One NYSE universe. A ranked signal every trading day.**

→ **[Live Dashboard](https://ericfecke.github.io/market-signum)**

---

MARKET SIGNUM runs a multi-agent stock analysis system across the NYSE. Five persona agents — each modeled on a distinct investment philosophy — evaluate every stock independently. A sixth agent reads the macro environment and dynamically re-weights the ensemble based on where we are in the economic cycle. The results are aggregated into a final score, ranked, and rendered to a sortable dashboard that updates three times per trading day.

Probabilistic AI handles reasoning and signal generation. Deterministic Python handles data retrieval, scoring, filtering, and rendering. That separation is what keeps the system reliable at scale.

---

## The Five Agents

Each agent receives the same stock data object and returns `{ signal, confidence, reasoning }`. No agent sees another's output. The signals are aggregated by a separate scoring layer.

### Benjamin Graham — Valuation Gatekeeper

> *"Buy only when price is significantly below intrinsic value — margin of safety above all else."*

Graham is the skeptic of the ensemble. He evaluates P/E ratio (prefers ≤ 10), price-to-book (prefers ≤ 1.0), debt-to-equity (flags anything above 0.5), current ratio (requires ≥ 2), and earnings stability over 10 years. He rarely says buy — which is exactly why a Graham BUY signal is the most meaningful one in the system.

### Warren Buffett — Quality & Moat

> *"A wonderful company at a fair price beats a fair company at a wonderful price."*

Buffett looks for compounders: consistent ROE above 15%, margins above industry peers, evidence of durable competitive advantage (brand, switching costs, cost leadership), and positive free cash flow. He's more willing to pay up than Graham, but only for quality that earns it. Will flag a stock even at fair value if the moat is real.

### Ray Dalio — Macro Regime Detection

> *"Understand the machine. The economic cycle determines which assets win."*

Dalio doesn't analyze individual stocks. He reads the macro environment — debt cycle position, inflation trajectory, interest rate direction, credit spreads, VIX — and returns a `regime_flag` that shifts the weights of every other agent for the entire batch run. In a deleveraging regime, he holds veto power over BUY signals. In a risk-on environment, he amplifies Lynch and Simons. **He runs exactly once per batch scan.**

### Peter Lynch — Growth & Discovery

> *"Find growth before the institutions do."*

Lynch is the optimist. He screens for PEG ratio ≤ 1.0, earnings growth of 15–30% annually, low institutional ownership (more upside if he's early), and insider buying. He's the most likely to flag a stock early and the most likely to be wrong in a late-cycle environment — which is why Dalio's regime flag matters.

### Jim Simons — Quant Signals

> *"The market has patterns. Find them with math, not narrative."*

Simons has no opinion on the business. He looks at RSI, MACD crossovers, 50d/200d moving average positioning, Bollinger band placement, and statistical momentum across 30/60/90-day windows. He's the counterweight to narrative-heavy agents. When Lynch and Simons agree, momentum is confirmed. When they diverge, the score reflects that uncertainty.

---

## Dynamic Weighting

Base weights in a neutral macro regime:

| Agent | Base Weight |
|---|---|
| Graham | 15% |
| Buffett | 25% |
| Dalio | 20% |
| Lynch | 20% |
| Simons | 20% |

Dalio's `regime_flag` shifts these weights at runtime before any ticker is scored:

| Regime | Effect |
|---|---|
| **Risk-on** (easing, early cycle) | Lynch +20%, Simons +10%, Graham −10% |
| **Neutral** | Base weights unchanged |
| **Risk-off** (tightening, late cycle) | Graham +20%, Buffett +10%, Lynch −15%, Simons −5% |
| **Deleveraging** | Graham +30%, Dalio veto on all BUY signals |

Each agent's confidence score further modulates its effective weight — an agent with sparse or missing data contributes proportionally less to the final score.

**Score thresholds:**

| Score | Signal |
|---|---|
| 0.70 – 1.00 | 🟢 **BUY** — strong ensemble consensus |
| 0.50 – 0.69 | 🟡 **WATCH** — mixed signals, monitor |
| 0.00 – 0.49 | 🔴 **AVOID** — weak or negative consensus |

---

## Dashboard

The live dashboard at **[ericfecke.github.io/market-signum](https://ericfecke.github.io/market-signum)** is a fully self-contained HTML file — no server, no framework, no CDN dependencies.

```
┌──────────────────────────────────────────────────────────────────┐
│  MARKET SIGNUM                             Generated: 2026-03-16 │
│  NYSE Batch Scan · 847 stocks analyzed · ⚖️ NEUTRAL regime       │
├──────────────────────────────────────────────────────────────────┤
│  DALIO MACRO REGIME BANNER                                       │
│  Current regime · Reasoning · Active weight shifts               │
├──────────────────────────────────────────────────────────────────┤
│  🟢 BUY: 94  │  🟡 WATCH: 511  │  🔴 AVOID: 242                  │
├──────────────────────────────────────────────────────────────────┤
│  [BUY] [WATCH] [AVOID] [All]  [All Sectors ▾]  [Search…]        │
├──────────────────────────────────────────────────────────────────┤
│  #   Ticker  Score   Rec    G    Bu   Ly   Si   Sector    Cap    │
│  1   AAPL    0.821  🟢BUY  🟢   🟢   🟢   🟡  Technology  3.1T  │
│  ▼   [expanded: agent cards · confidence · score bar]            │
│  2   MSFT    0.789  🟢BUY  🟡   🟢   🟢   🟢  Technology  2.8T  │
└──────────────────────────────────────────────────────────────────┘
```

Every column is sortable. Filters compose (BUY + Technology = only BUY-rated tech stocks). Clicking any row expands a full per-stock breakdown showing each agent's signal, confidence, and effective weight under the current regime. Pin any stock to a persistent Watchlist stored in your browser's localStorage.

---

## How to Run Locally

### Prerequisites

```bash
pip install yfinance pandas numpy requests anthropic
```

`anthropic` is optional. If `ANTHROPIC_API_KEY` is set in `.env`, agents generate AI-written reasoning narratives. If it's absent, they fall back to deterministic rule-based reasoning. Signals and scores are never affected either way.

### Full NYSE scan

```bash
python tools/batch_runner.py
```

Fetches the current NYSE universe (~1,400 tickers), runs the pre-filter (market cap ≥ $500M, avg volume ≥ 500K shares), and runs the full five-agent pipeline on everything that passes. Output writes to `output/report.html`. A full scan takes roughly 45–60 minutes.

### Faster options

```bash
python tools/batch_runner.py --limit 100        # first 100 tickers (dev/test)
python tools/batch_runner.py --tickers AAPL,MSFT,NVDA  # specific tickers
python tools/batch_runner.py --delay 1.5        # slower pace if yfinance throttles
python tools/batch_runner.py --skip-filter      # bypass market cap / volume filter
```

### Preview the output

```bash
python -m http.server 8080 --directory output
# open http://localhost:8080
```

---

## How It's Scheduled

A GitHub Actions workflow runs three times per trading weekday:

| Run | Time (ET) |
|---|---|
| Open | 9:30 AM |
| Midday | 12:00 PM |
| Close | 4:30 PM |

After each scan, the updated `index.html` is committed and pushed to the repo, which triggers GitHub Pages to redeploy. The live dashboard is always the most recent completed scan.

Manual runs can be triggered from the Actions tab with configurable `limit`, `delay`, and `skip_filter` parameters.

---

## Architecture

The system is organized as a WAT stack — Workflows, Agents, Tools:

- **`workflows/`** — Markdown SOPs defining inputs, outputs, and edge cases for each pipeline step
- **`tools/`** — Python scripts that do the actual work (data fetching, agent logic, scoring, rendering)
- **Agents** — the orchestration layer that sequences the tools and synthesizes the outputs

```
tools/
  fetch_nyse_tickers.py   NYSE universe (SEC EDGAR → Wikipedia → NASDAQ Trader → cache → static fallback)
  fetch_stock_data.py     Per-ticker data via yfinance; 6h cache in .tmp/
  dalio_agent.py          Macro regime detection; runs once per batch
  graham_agent.py         Valuation analysis
  buffett_agent.py        Quality + moat analysis
  lynch_agent.py          Growth + discovery screening
  simons_agent.py         Quant signals + pattern recognition
  score_and_weight.py     Ensemble aggregator; applies regime weights + confidence modulation
  batch_runner.py         Full NYSE orchestrator
  render_html.py          Dashboard renderer; writes output/report.html and index.html
```

Data flows one way: `fetch_stock_data.py` → agents → `score_and_weight.py` → `render_html.py`. Agents never pull their own data.

---

## Disclaimer

MARKET SIGNUM is an educational project. Nothing here is investment advice. The signals are generated by AI agents interpreting publicly available data — treat them as a starting point for research, not a recommendation to buy or sell anything.
