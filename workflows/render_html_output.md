# Workflow: render_html_output

## Objective
Read all cached results for a ticker and render a self-contained HTML dashboard to `output/report.html`. The report is the human-readable output of the entire MARKET SIGNUM pipeline.

## Tool
`tools/render_html.py`

## Prerequisites
All of the following `.tmp/` files must exist:
- `.tmp/<TICKER>.json` — stock data (from `fetch_stock_data.py`)
- `.tmp/<TICKER>_score.json` — final score (from `score_and_weight.py`)
- `.tmp/<TICKER>_dalio.json`, `_graham.json`, `_buffett.json`, `_lynch.json`, `_simons.json`

Agent files are optional — the renderer degrades gracefully if any are missing.

## Required Input
- `ticker` (string) — passed as CLI argument or to `render_html()`

## Expected Output
- `output/report.html` — fully self-contained HTML file (no external CDN)
- No images, no fonts from external servers — renders offline

## Layout

```
┌─────────────────────────────────────────────────────────┐
│  TICKER HEADER                                          │
│  Symbol | Company | Sector · Industry | Price | Date   │
├─────────────────────────────────────────────────────────┤
│  DALIO MACRO REGIME BANNER                              │
│  Regime icon + label · Dalio reasoning snippet         │
│  Weight pills (elevated/reduced vs neutral highlighted) │
├─────────────────────────────────────────────────────────┤
│  DALIO MACRO ANALYSIS (full card)                       │
├─────────────────────────────────────────────────────────┤
│  PERSONA ANALYSIS (3-column grid)                       │
│  Graham card | Buffett card | Lynch card                │
│  Each card: name + tag · badge · confidence · reasoning │
│            + dimension score bars                       │
├─────────────────────────────────────────────────────────┤
│  JIM SIMONS QUANT INDICATORS (indicator grid)           │
│  RSI · MACD · MA Cross · vs MA50 · vs MA200            │
│  BB Position · Vol Ratio · Momentum 30d/90d · vs 52W   │
├─────────────────────────────────────────────────────────┤
│  FINAL WEIGHTED VERDICT                                 │
│  Recommendation (BUY/WATCH/AVOID) · Score (0–1)        │
│  Score bar with AVOID/WATCH/BUY zones + needle marker  │
│  Consensus pills: which agents agree / disagree        │
│  Veto note (if deleveraging)                           │
└─────────────────────────────────────────────────────────┘
```

## Design System

### Colors
| State | Background | Accent | Text |
|---|---|---|---|
| BUY | `rgba(35,134,54,.18)` | `#3fb950` | white |
| WATCH | `rgba(158,106,3,.18)` | `#d29922` | white |
| AVOID | `rgba(218,54,51,.18)` | `#f85149` | white |
| Neutral/macro | `rgba(31,111,235,.18)` | `#58a6ff` | white |

Page background: `#0d1117`. Card background: `#161b22`.

### Signal badges
Color-coded consistently: green = buy, yellow = watch, red = avoid. Same palette used for card top-border accent, badge background, and consensus pills.

### Score bar
Horizontal bar divided into three zones:
- AVOID: 0–50% of bar width (red-tinted)
- WATCH: 50–70% (yellow-tinted)
- BUY: 70–100% (green-tinted)
- White needle positioned at `score × 100%`

### Regime banner weight pills
Agent weight pills are highlighted:
- `elevated` (green border) — weight raised above neutral baseline by > 3%
- `reduced` (red border) — weight lowered below neutral by > 3%
- Default — unchanged from neutral

### Dimension score bars
Per-agent dimension bars are centered at 0, extending left (negative) or right (positive). Red fill for negative, green for positive.

## Responsive Behavior
- Desktop-first, single-column below 600px
- Agent cards: CSS grid, `auto-fill` with `minmax(300px, 1fr)`
- Quant indicators: `auto-fill` with `minmax(160px, 1fr)`, collapses to 2-col on mobile

## Programmatic Usage
```python
from tools.render_html import render_html, build_html

# Simple: load from .tmp/ and write output/report.html
path = render_html("AAPL")

# Advanced: pass pre-loaded data
html_str = build_html(stock_data, score_result, agent_results)
Path("my_report.html").write_text(html_str)
```

## Security
All dynamic content is passed through `html.escape()` before insertion. Reasoning strings from agents are escaped to prevent any rendering issues.

## Edge Cases

| Situation | Behavior |
|---|---|
| `.tmp/<TICKER>.json` missing | Raises `FileNotFoundError` with instructions |
| `.tmp/<TICKER>_score.json` missing | Raises `FileNotFoundError` with instructions |
| Agent `.json` files missing | Those cards are omitted from the report |
| Reasoning is None | Card shows "—" in the reasoning section |
| Missing technicals | Quant indicator shows "—" for that field |

## Updates
- 2026-03-10: Initial version. Full self-contained HTML, dark theme, responsive layout, score bar with zone markers.
