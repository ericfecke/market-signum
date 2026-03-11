#!/usr/bin/env python3
"""
render_html.py — HTML Dashboard Renderer for MARKET SIGNUM

Two modes:

  Batch (called by batch_runner.py):
    render_html(results: list[dict], dalio_result=dict)
    → master dashboard: sortable table + expandable per-stock breakdowns

  Single-ticker (backward compatible):
    render_html("AAPL")                        # loads .tmp/AAPL_*.json
    render_html("AAPL", output_path=Path(...)) # custom output path

Design rules:
  - Dark background (#0d1117), GitHub-inspired
  - Green = buy, yellow = watch, red = avoid — consistent everywhere
  - No external CDN — fully self-contained HTML (inline CSS + JS)
  - Renders in Chrome, Firefox, Safari, Edge without frameworks

CLI:
  python tools/render_html.py AAPL
  python tools/render_html.py AAPL --out path/to/custom.html
"""

import html
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT       = Path(__file__).parent.parent
TMP_DIR    = ROOT / ".tmp"
OUTPUT_DIR = ROOT / "output"
TMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

_AGENTS = ("dalio", "graham", "buffett", "lynch", "simons")

_AGENT_META = {
    "dalio":   {"name": "Ray Dalio",        "tag": "Macro · Regime",         "short": "Da"},
    "graham":  {"name": "Benjamin Graham",  "tag": "Value · Margin of Safety","short": "G"},
    "buffett": {"name": "Warren Buffett",   "tag": "Quality · Moat",          "short": "Bu"},
    "lynch":   {"name": "Peter Lynch",      "tag": "Growth · Discovery",      "short": "Ly"},
    "simons":  {"name": "Jim Simons",       "tag": "Quant · Pattern",         "short": "Si"},
}

_REGIME_META = {
    "risk-on":      {"icon": "📈", "label": "RISK-ON",      "cls": "risk-on"},
    "neutral":      {"icon": "⚖️",  "label": "NEUTRAL",      "cls": "neutral"},
    "risk-off":     {"icon": "🛡️",  "label": "RISK-OFF",     "cls": "risk-off"},
    "deleveraging": {"icon": "⚠️",  "label": "DELEVERAGING", "cls": "deleveraging"},
}

_NEUTRAL_WEIGHTS = {
    "graham": 0.15, "buffett": 0.25, "dalio": 0.20, "lynch": 0.20, "simons": 0.20
}

# Quick-reference card data for each persona — rendered in the dashboard info panels.
# Kept in sync with the reference cards in CLAUDE.md.
_PERSONA_REF = {
    "graham":  {
        "philosophy": "Buy only when price is significantly below intrinsic value — margin of safety above all else.",
        "metrics":    "P/E ratio · Price-to-book · Debt/Equity · Current ratio",
        "buy":        "P/E ≤ 10, P/B ≤ 1.0, D/E < 0.5, current ratio ≥ 2, earnings stable 10y",
        "avoid":      "P/E > 20, P/B > 2.0, D/E ≥ 0.5, current ratio < 1.5, or erratic earnings",
    },
    "buffett": {
        "philosophy": "A wonderful company at a fair price — durable moat and capital returns matter more than cheapness.",
        "metrics":    "Return on equity · Profit margin · Free cash flow · Moat evidence",
        "buy":        "ROE > 15% consistently, margins above peers, clear moat, FCF positive",
        "avoid":      "ROE < 10%, margins thin or shrinking, no competitive advantage, weak or negative FCF",
    },
    "lynch":   {
        "philosophy": "Find growth before the institutions do — understand the business, trust the earnings trajectory.",
        "metrics":    "PEG ratio · Earnings growth rate · Institutional ownership % · Insider activity",
        "buy":        "PEG ≤ 1.0, earnings growing 15–30% YoY, low institutional ownership, insiders buying",
        "avoid":      "PEG > 2.0, earnings growth stalling or negative, fully institutionally owned, insiders selling",
    },
    "simons":  {
        "philosophy": "The market has patterns — find them with math, not narrative. No opinions on the business.",
        "metrics":    "RSI · MACD crossover · 50d/200d MA cross · 30/60/90d momentum",
        "buy":        "RSI 40–65 trending up, bullish MACD crossover, golden cross (50d > 200d), positive momentum",
        "avoid":      "RSI > 75 (overbought) or < 25 with no reversal, bearish MACD, death cross, negative momentum",
    },
    "dalio":   {
        "philosophy": "Understand the machine — the debt/economic cycle determines which assets win.",
        "metrics":    "10Y yield trend · VIX · Credit spreads (TLT) · Inflation proxies",
        "buy":        "Easing rates, low/falling VIX, early-to-mid debt cycle → boosts Lynch & Simons weights",
        "avoid":      "Tightening, rising VIX, late cycle → boosts Graham & Buffett; deleveraging vetoes BUY calls",
    },
}

_SIG_NUM = {"buy": 2, "watch": 1, "avoid": 0}  # for signal column sort


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _e(v) -> str:
    """HTML-escape a value. None → empty string."""
    return html.escape(str(v)) if v is not None else "—"


def _ej(v) -> str:
    """Escape for use inside JS string literals (JSON-safe)."""
    if v is None:
        return ""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")


def _fmt_cap(v) -> str:
    """Format a market cap number to a human-readable string (e.g. 3.1T, 380B)."""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1e12:
        return f"{v/1e12:.1f}T"
    if v >= 1e9:
        return f"{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{v/1e6:.0f}M"
    return f"{v:,.0f}"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def _css() -> str:
    return """
:root {
  --bg:           #0d1117;
  --bg-card:      #161b22;
  --bg-card2:     #1c2128;
  --border:       #30363d;
  --border-light: #21262d;
  --text:         #e6edf3;
  --text-muted:   #8b949e;
  --text-faint:   #6e7681;

  --green:        #238636;
  --green-light:  #3fb950;
  --green-bg:     rgba(35,134,54,.18);

  --yellow:       #9e6a03;
  --yellow-light: #d29922;
  --yellow-bg:    rgba(158,106,3,.18);

  --red:          #da3633;
  --red-light:    #f85149;
  --red-bg:       rgba(218,54,51,.18);

  --blue:         #1f6feb;
  --blue-light:   #58a6ff;
  --blue-bg:      rgba(31,111,235,.18);

  --radius:       8px;
  --radius-lg:    12px;
  --font:         -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --font-mono:    "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.6;
  padding: 24px 16px 48px;
}

a { color: var(--blue-light); text-decoration: none; }
.mono { font-family: var(--font-mono); }

.container { max-width: 1200px; margin: 0 auto; }

/* ── Dashboard header ─────────────────────────────────────────────── */
.dash-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
  padding: 20px 24px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 12px;
}
.dash-title    { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; }
.dash-subtitle { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
.dash-meta     { text-align: right; font-size: 12px; color: var(--text-faint); }

/* ── Single-ticker header ─────────────────────────────────────────── */
.ticker-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 12px;
  padding: 20px 24px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 12px;
}
.header-left   { display: flex; flex-direction: column; gap: 4px; }
.ticker-symbol { font-size: 32px; font-weight: 700; letter-spacing: -0.5px; }
.company-name  { font-size: 15px; color: var(--text-muted); }
.sector-tag    {
  display: inline-block;
  padding: 2px 8px;
  background: var(--bg-card2);
  border: 1px solid var(--border);
  border-radius: 20px;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}
.header-right  { text-align: right; }
.price-large   { font-size: 28px; font-weight: 600; }
.change-pos    { color: var(--green-light);  font-size: 14px; margin-left: 6px; }
.change-neg    { color: var(--red-light);    font-size: 14px; margin-left: 6px; }
.change-flat   { color: var(--text-muted);   font-size: 14px; margin-left: 6px; }
.header-meta   { font-size: 12px; color: var(--text-faint); margin-top: 6px; }

/* ── Regime banner ────────────────────────────────────────────────── */
.regime-banner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  padding: 14px 20px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
  margin-bottom: 16px;
  font-size: 13px;
}
.regime-banner.risk-on      { background: var(--green-bg);  border-color: var(--green);  }
.regime-banner.neutral      { background: var(--blue-bg);   border-color: var(--blue);   }
.regime-banner.risk-off     { background: var(--yellow-bg); border-color: var(--yellow); }
.regime-banner.deleveraging { background: var(--red-bg);    border-color: var(--red);    }

.regime-left  { display: flex; align-items: center; gap: 10px; }
.regime-icon  { font-size: 20px; }
.regime-label { font-weight: 700; font-size: 15px; letter-spacing: 0.5px; }
.regime-sub   { color: var(--text-muted); font-size: 12px; }
.veto-pill    {
  background: var(--red-bg);
  color: var(--red-light);
  border: 1px solid var(--red);
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  margin-left: 8px;
}
.weight-pills { display: flex; flex-wrap: wrap; gap: 6px; }
.weight-pill  {
  padding: 3px 10px;
  border-radius: 20px;
  background: var(--bg-card2);
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}
.weight-pill.elevated { border-color: var(--green);  color: var(--green-light); }
.weight-pill.reduced  { border-color: var(--red);    color: var(--red-light);   }

/* ── Summary bar ──────────────────────────────────────────────────── */
.summary-bar {
  display: flex;
  gap: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 12px;
  overflow: hidden;
}
.summary-stat {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 14px 8px;
  border-right: 1px solid var(--border-light);
  gap: 2px;
}
.summary-stat:last-child { border-right: none; }
.summary-num   { font-size: 22px; font-weight: 700; line-height: 1; }
.summary-label { font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: 0.5px; }
.summary-num.buy   { color: var(--green-light); }
.summary-num.watch { color: var(--yellow-light); }
.summary-num.avoid { color: var(--red-light); }

/* ── Filter bar ───────────────────────────────────────────────────── */
.filter-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  padding: 12px 14px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 12px;
}
.filter-group { display: flex; gap: 4px; }
.filter-btn {
  padding: 5px 12px;
  border: 1px solid var(--border);
  border-radius: 20px;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all .15s;
  white-space: nowrap;
}
.filter-btn:hover { background: var(--bg-card2); color: var(--text); }
.filter-btn.active-all   { background: var(--bg-card2); color: var(--text); border-color: var(--border); }
.filter-btn.active-buy   { background: var(--green-bg); color: var(--green-light); border-color: var(--green); }
.filter-btn.active-watch { background: var(--yellow-bg); color: var(--yellow-light); border-color: var(--yellow); }
.filter-btn.active-avoid { background: var(--red-bg); color: var(--red-light); border-color: var(--red); }

.filter-select {
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-card2);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
}
.filter-search {
  flex: 1;
  min-width: 140px;
  padding: 5px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-card2);
  color: var(--text);
  font-size: 12px;
  outline: none;
}
.filter-search:focus { border-color: var(--blue); }
.filter-search::placeholder { color: var(--text-faint); }
.showing-count { font-size: 12px; color: var(--text-muted); padding: 4px 2px; }

/* ── Results table ────────────────────────────────────────────────── */
.results-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); }
.results-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.results-table thead { position: sticky; top: 0; z-index: 10; }
.results-table th {
  background: var(--bg-card);
  padding: 10px 12px;
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-faint);
  border-bottom: 2px solid var(--border);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}
.results-table th:hover { color: var(--text-muted); }
.results-table th.sort-desc::after { content: " ▼"; color: var(--blue-light); }
.results-table th.sort-asc::after  { content: " ▲"; color: var(--blue-light); }
.results-table th.no-sort { cursor: default; }

.results-table td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-light);
  vertical-align: middle;
}
.stock-row { cursor: pointer; transition: background .12s; }
.stock-row:hover   { background: var(--bg-card2); }
.stock-row.expanded { background: var(--bg-card2); }
.stock-row.expanded .expand-ind::after { content: "▼"; color: var(--blue-light); }
.expand-ind::after { content: "▶"; color: var(--text-faint); font-size: 11px; }

.rank-cell { color: var(--text-faint); font-size: 12px; width: 32px; }
.ticker-cell { font-weight: 600; }
.ticker-name { font-size: 11px; color: var(--text-muted); font-weight: 400; }

/* Mini score bar */
.mini-score      { display: flex; align-items: center; gap: 6px; }
.mini-score-num  { font-family: var(--font-mono); font-size: 13px; min-width: 36px; }
.mini-score-bar  { width: 52px; height: 5px; background: var(--border); border-radius: 3px; overflow: hidden; flex-shrink: 0; }
.mini-score-fill { height: 100%; border-radius: 3px; }
.mini-score-fill.buy   { background: var(--green-light); }
.mini-score-fill.watch { background: var(--yellow-light); }
.mini-score-fill.avoid { background: var(--red-light); }

/* Signal dot */
.sdot {
  display: inline-block;
  width: 9px; height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sdot.buy   { background: var(--green-light); }
.sdot.watch { background: var(--yellow-light); }
.sdot.avoid { background: var(--red-light); }
.sdot.na    { background: var(--border); }

/* ── Detail row ───────────────────────────────────────────────────── */
.detail-row td {
  padding: 0;
  border-bottom: 2px solid var(--border);
}
.detail-inner {
  padding: 20px 20px 24px;
  background: var(--bg);
  border-top: 1px solid var(--border-light);
}
.detail-loading { padding: 20px; color: var(--text-muted); font-size: 12px; }

/* ── Agent cards ──────────────────────────────────────────────────── */
.section-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-faint);
  margin-bottom: 12px;
}
.agents-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.agent-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.agent-card.signal-buy   { border-top: 3px solid var(--green);  }
.agent-card.signal-watch { border-top: 3px solid var(--yellow); }
.agent-card.signal-avoid { border-top: 3px solid var(--red);    }
.card-header { display: flex; justify-content: space-between; align-items: flex-start; }
.agent-name  { font-weight: 600; font-size: 14px; }
.agent-tag   { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
.badge {
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
  white-space: nowrap;
  flex-shrink: 0;
}
.badge-buy   { background: var(--green);  color: #fff; }
.badge-watch { background: var(--yellow); color: #fff; }
.badge-avoid { background: var(--red);    color: #fff; }
.confidence-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-muted);
}
.confidence-track {
  flex: 1; height: 5px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.confidence-fill   { height: 100%; border-radius: 3px; background: var(--blue-light); }
.confidence-val    { font-size: 11px; color: var(--text-muted); min-width: 28px; text-align: right; }
.reasoning         { font-size: 13px; color: var(--text-muted); line-height: 1.55; flex: 1; }
.dim-scores        { border-top: 1px solid var(--border-light); padding-top: 10px; }
.dim-row           { display: flex; align-items: center; gap: 6px; margin-bottom: 5px; font-size: 11px; }
.dim-label         { color: var(--text-faint); width: 110px; flex-shrink: 0; }
.dim-track         {
  flex: 1; height: 5px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
  position: relative;
}
.dim-fill-pos { position: absolute; height: 100%; left: 50%;  border-radius: 3px; background: var(--green-light); }
.dim-fill-neg { position: absolute; height: 100%; right: 50%; border-radius: 3px; background: var(--red-light);   }
.dim-val      { color: var(--text-muted); width: 38px; text-align: right; flex-shrink: 0; }

/* ── Simons quant bar ─────────────────────────────────────────────── */
.quant-bar { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px 20px; margin-bottom: 16px; }
.quant-indicators { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: 10px; }
.indicator { background: var(--bg-card2); border: 1px solid var(--border-light); border-radius: 6px; padding: 8px 12px; }
.ind-label { font-size: 10px; color: var(--text-faint); text-transform: uppercase; letter-spacing: 0.5px; }
.ind-value { font-size: 15px; font-weight: 600; margin: 2px 0; }
.ind-note  { font-size: 10px; color: var(--text-muted); }
.ind-value.bullish { color: var(--green-light); }
.ind-value.bearish { color: var(--red-light);   }
.ind-value.neutral { color: var(--text-muted);  }

/* ── Verdict / score bar ──────────────────────────────────────────── */
.verdict { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 24px; margin-bottom: 16px; }
.verdict-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; margin-bottom: 20px; }
.verdict-rec { font-size: 36px; font-weight: 700; letter-spacing: -0.5px; }
.verdict-rec.BUY   { color: var(--green-light); }
.verdict-rec.WATCH { color: var(--yellow-light); }
.verdict-rec.AVOID { color: var(--red-light);   }
.score-display  { text-align: right; }
.score-number   { font-size: 28px; font-weight: 600; font-family: var(--font-mono); }
.score-label    { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
.score-bar-wrap { margin-bottom: 20px; }
.score-track    { height: 18px; border-radius: 9px; overflow: hidden; display: flex; position: relative; border: 1px solid var(--border); }
.score-zone-avoid { flex: 50;  background: rgba(218,54,51,.25);  }
.score-zone-watch { flex: 20;  background: rgba(158,106,3,.25);  }
.score-zone-buy   { flex: 30;  background: rgba(35,134,54,.25);  }
.score-zone-label { display: flex; font-size: 10px; color: var(--text-faint); margin-top: 4px; }
.score-zone-label span { flex: 1; }
.score-zone-label span:nth-child(2) { flex: 0.4; }
.score-needle-wrap { position: relative; height: 0; }
.score-needle { position: absolute; top: -22px; width: 3px; height: 22px; background: #fff; border-radius: 2px; transform: translateX(-50%); box-shadow: 0 0 6px rgba(255,255,255,.6); }
.consensus-row   { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.consensus-group { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.consensus-label { font-size: 11px; color: var(--text-faint); }
.consensus-agent { padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.consensus-agent.buy   { background: var(--green-bg);  color: var(--green-light);  border: 1px solid var(--green);  }
.consensus-agent.watch { background: var(--yellow-bg); color: var(--yellow-light); border: 1px solid var(--yellow); }
.consensus-agent.avoid { background: var(--red-bg);    color: var(--red-light);    border: 1px solid var(--red);    }

/* ── Agent info reference panel ───────────────────────────────────── */
.info-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 50%;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 10px;
  line-height: 1;
  padding: 1px 4px;
  margin-left: 5px;
  vertical-align: middle;
  transition: color .15s, border-color .15s, background .15s;
  flex-shrink: 0;
}
.info-btn:hover  { color: var(--blue-light); border-color: var(--blue-light); }
.info-btn.active { color: var(--blue-light); border-color: var(--blue-light); background: var(--blue-bg); }

.agent-ref-panel {
  display: none;
  border-top: 1px solid var(--border-light);
  padding-top: 10px;
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.5;
}
.agent-ref-panel.visible { display: block; }
.ref-row {
  display: flex;
  gap: 8px;
  padding: 3px 0;
  border-bottom: 1px solid var(--border-light);
}
.ref-row:last-child  { border-bottom: none; }
.ref-label { color: var(--text-faint); flex-shrink: 0; width: 70px; font-weight: 600; padding-top: 1px; }
.ref-value { color: var(--text-muted); }
.ref-buy   { color: var(--green-light) !important; }
.ref-avoid { color: var(--red-light)   !important; }

/* ── Personas section ──────────────────────────────────────────────── */
.personas-section { border:1px solid var(--border); border-radius:8px; margin-bottom:16px; overflow:hidden; }
.personas-header  { display:flex; justify-content:space-between; align-items:center; padding:10px 14px; background:var(--bg-card); cursor:pointer; user-select:none; }
.personas-title   { font-size:11px; font-weight:700; letter-spacing:.08em; color:var(--text-faint); text-transform:uppercase; }
.personas-toggle  { font-size:11px; color:var(--blue-light); background:none; border:none; cursor:pointer; padding:2px 6px; }
.personas-body    { display:none; padding:14px; background:var(--bg); }
.personas-body.open { display:block; }
.personas-grid    { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }
.persona-card     { background:var(--bg-card); border:1px solid var(--border); border-radius:6px; padding:12px; font-size:11px; line-height:1.5; }
.persona-card-name{ font-size:13px; font-weight:700; color:var(--text); margin-bottom:2px; }
.persona-card-tag { color:var(--text-faint); font-size:10px; margin-bottom:8px; }
.persona-row      { display:flex; gap:6px; padding:3px 0; border-bottom:1px solid var(--border-light); }
.persona-row:last-child { border-bottom:none; }
.persona-lbl      { color:var(--text-faint); flex-shrink:0; width:64px; font-weight:600; padding-top:1px; font-size:10px; }
.persona-val      { color:var(--text-muted); }
.persona-buy      { color:var(--green-light) !important; }
.persona-avoid    { color:var(--red-light)   !important; }

/* ── Compact agent tiles (expanded rows) ───────────────────────────── */
.agents-compact   { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:12px; }
.agent-tile       { background:var(--bg-card2); border:1px solid var(--border); border-radius:6px; padding:10px 12px; }
.agent-tile.signal-buy   { border-left:3px solid var(--green-light); }
.agent-tile.signal-watch { border-left:3px solid var(--yellow-light); }
.agent-tile.signal-avoid { border-left:3px solid var(--red-light); }
.tile-name        { font-size:11px; font-weight:700; color:var(--text); margin-bottom:4px; }
.tile-tag         { font-size:10px; color:var(--text-faint); margin-bottom:6px; }
.tile-conf        { font-size:10px; color:var(--text-muted); margin-top:5px; }
.tile-wt          { font-size:10px; color:var(--text-faint); }

/* ── Compact score row (expanded rows) ────────────────────────────── */
.score-row        { display:flex; align-items:center; gap:14px; padding:10px 0; border-top:1px solid var(--border); flex-wrap:wrap; }
.score-row-num    { font-size:22px; font-weight:700; color:var(--text); line-height:1; }
.score-row-label  { font-size:10px; color:var(--text-faint); text-transform:uppercase; margin-top:2px; }
.score-needle-mini{ position:relative; height:6px; border-radius:3px; flex:1; min-width:80px; overflow:visible;
                    background:linear-gradient(to right, var(--red-light) 0% 49%, var(--yellow-light) 49% 70%, var(--green-light) 70% 100%); }
.sneedle-dot      { position:absolute; top:50%; transform:translate(-50%,-50%);
                    width:10px; height:10px; border-radius:50%; background:var(--text); border:2px solid var(--bg); z-index:1; }
.score-veto       { font-size:11px; color:var(--red-light); margin-top:4px; width:100%; }

/* ── Footer ───────────────────────────────────────────────────────── */
footer { text-align: center; font-size: 11px; color: var(--text-faint); margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border-light); }

/* ── Responsive ───────────────────────────────────────────────────── */
@media (max-width: 700px) {
  .ticker-header, .dash-header { flex-direction: column; }
  .header-right, .dash-meta    { text-align: left; }
  .agents-grid                 { grid-template-columns: 1fr; }
  .quant-indicators            { grid-template-columns: repeat(2, 1fr); }
  .summary-bar                 { flex-wrap: wrap; }
  .summary-stat                { flex: 1 1 40%; }
  .filter-bar                  { flex-direction: column; align-items: stretch; }
  .filter-group                { flex-wrap: wrap; }
}
"""


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

def _js() -> str:
    return r"""
// ── Helpers ──────────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '—';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmtCap(v) {
  if (!v) return '—';
  v = Number(v);
  if (v >= 1e12) return (v/1e12).toFixed(1) + 'T';
  if (v >= 1e9)  return (v/1e9).toFixed(1) + 'B';
  if (v >= 1e6)  return (v/1e6).toFixed(0) + 'M';
  return v.toLocaleString();
}

// ── Sort + filter state ───────────────────────────────────────────────────
let sortCol = 'score', sortDir = -1;
let filterRec = 'all', filterSector = 'all', filterSearch = '';

// ── Row cache (built once on DOMContentLoaded; avoids querySelectorAll) ───
let _rowCache      = null;   // Array of .stock-row elements
let _rowMap        = null;   // Map<ticker, stock-row element> for O(1) lookup
let _openDetailRow = null;   // Currently-visible detail <tr>, or null
let _openStockRow  = null;   // Currently-expanded stock <tr>, or null

function sortBy(col) {
  if (sortCol === col) { sortDir = -sortDir; }
  else { sortCol = col; sortDir = (['ticker','sector'].includes(col)) ? 1 : -1; }
  applyView();
}

function setRecFilter(val) {
  filterRec = val;
  document.querySelectorAll('.filter-btn[data-rec]').forEach(b => {
    b.className = 'filter-btn' + (b.dataset.rec === val ? ' active-' + (val === 'all' ? 'all' : val.toLowerCase()) : '');
  });
  applyView();
}

function setSectorFilter(val) {
  filterSector = val;
  applyView();
}

let _searchTimer = null;
function setSearch(val) {
  filterSearch = val.toLowerCase().trim();
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(applyView, 16);
}

function togglePersonas() {
  const body = document.getElementById('personas-body');
  const btn  = document.getElementById('personas-toggle');
  if (!body) return;
  const open = body.classList.toggle('open');
  if (btn) btn.textContent = open ? '\u25b2 Collapse' : '\u25bc Expand';
}

// ── Apply current view (sort + filter) ───────────────────────────────────
function applyView() {
  // Use cached row list; fall back to live query on very first call
  const allRows = _rowCache || Array.from(document.querySelectorAll('.stock-row'));

  // Track which row was open so we can restore it after reorder
  const openSR = _openStockRow;
  const openDR = _openDetailRow;

  // 1. Filter
  const visible = allRows.filter(r => {
    const rec    = r.dataset.rec;
    const sector = r.dataset.sector;
    const ticker = r.dataset.ticker.toLowerCase();
    const name   = (r.dataset.name || '').toLowerCase();
    if (filterRec !== 'all' && rec !== filterRec) return false;
    if (filterSector !== 'all' && sector !== filterSector) return false;
    if (filterSearch && !ticker.includes(filterSearch) && !name.includes(filterSearch)) return false;
    return true;
  });

  // 2. Sort
  const sigNum = {buy: 2, watch: 1, avoid: 0};
  const getVal = r => {
    switch (sortCol) {
      case 'score':   return parseFloat(r.dataset.score);
      case 'ticker':  return r.dataset.ticker;
      case 'rec':     return sigNum[r.dataset.rec.toLowerCase()] ?? 1;
      case 'sector':  return r.dataset.sector;
      case 'price':   return parseFloat(r.dataset.price) || 0;
      case 'mktcap':  return parseFloat(r.dataset.mktcap) || 0;
      case 'g':       return sigNum[r.dataset.graham.toLowerCase()] ?? 1;
      case 'bu':      return sigNum[r.dataset.buffett.toLowerCase()] ?? 1;
      case 'ly':      return sigNum[r.dataset.lynch.toLowerCase()] ?? 1;
      case 'si':      return sigNum[r.dataset.simons.toLowerCase()] ?? 1;
      default:        return 0;
    }
  };
  const strCols = new Set(['ticker','sector']);
  visible.sort((a, b) => {
    const av = getVal(a), bv = getVal(b);
    const cmp = strCols.has(sortCol) ? String(av).localeCompare(String(bv)) : (Number(av) - Number(bv));
    return cmp * sortDir;
  });

  // 3. Hide all stock rows + the single open detail row (O(1) instead of querySelectorAll)
  allRows.forEach(r => { r.style.display = 'none'; });
  if (openDR) openDR.style.display = 'none';

  // 4. Batch-append sorted/filtered rows via DocumentFragment (single reflow)
  const tbody = document.querySelector('.results-table tbody');
  const frag  = document.createDocumentFragment();
  visible.forEach((row, i) => {
    row.style.display = '';
    row.querySelector('.rank-cell').textContent = i + 1;
    frag.appendChild(row);
    const dr = document.getElementById('dr-' + row.dataset.ticker);
    if (dr) frag.appendChild(dr);
  });
  tbody.appendChild(frag);

  // 5. Restore expanded row if it's still in the visible set
  if (openSR && visible.includes(openSR)) {
    if (openDR) openDR.style.display = '';
    openSR.classList.add('expanded');
  } else {
    // Expanded row was filtered out — clear trackers
    _openDetailRow = null;
    _openStockRow  = null;
  }

  // 6. Update sort header indicators
  document.querySelectorAll('th[data-col]').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.col === sortCol) th.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
  });

  // 7. Update showing count
  const countEl = document.getElementById('showing-count');
  if (countEl) countEl.textContent = `Showing ${visible.length} of ${allRows.length} stocks`;
}

// ── Detail row toggle + lazy build ───────────────────────────────────────
function toggleDetail(ticker) {
  // O(1) lookup via Map; fall back to querySelector if cache not yet ready
  const sr = (_rowMap && _rowMap.get(ticker))
    || document.querySelector(`.stock-row[data-ticker="${CSS.escape(ticker)}"]`);
  if (!sr) return;

  const alreadyOpen = (_openStockRow === sr);

  // Close currently-open row (O(1) — no querySelectorAll scan)
  if (_openDetailRow) { _openDetailRow.style.display = 'none'; _openDetailRow = null; }
  if (_openStockRow)  { _openStockRow.classList.remove('expanded'); _openStockRow = null; }

  if (alreadyOpen) return;   // clicked same row → just collapse, done

  // Get or create the detail row (only ~1 DOM node created per unique ticker ever opened)
  let dr = document.getElementById('dr-' + ticker);
  if (!dr) {
    dr = document.createElement('tr');
    dr.className = 'detail-row';
    dr.id = 'dr-' + ticker;
    dr.style.display = 'none';
    const td = document.createElement('td');
    td.colSpan = 13;
    const inner = document.createElement('div');
    inner.className = 'detail-inner';
    inner.id = 'di-' + ticker;
    inner.innerHTML = '<div class="detail-loading">Loading\u2026</div>';
    td.appendChild(inner);
    dr.appendChild(td);
    sr.after(dr);   // insert immediately after its stock-row
  }

  // Lazy-build detail content on first open
  if (!dr.dataset.built) {
    const inner = dr.querySelector('.detail-inner');
    if (inner) inner.innerHTML = buildDetailHTML(ticker);
    dr.dataset.built = '1';
  }

  dr.style.display = '';
  sr.classList.add('expanded');
  _openDetailRow = dr;
  _openStockRow  = sr;
}

// ── Detail HTML builder (from DETAIL_DATA) ───────────────────────────────
const AGENT_META = {
  dalio:   {name: 'Ray Dalio',       tag: 'Macro · Regime'},
  graham:  {name: 'Benjamin Graham', tag: 'Value · Margin of Safety'},
  buffett: {name: 'Warren Buffett',  tag: 'Quality · Moat'},
  lynch:   {name: 'Peter Lynch',     tag: 'Growth · Discovery'},
  simons:  {name: 'Jim Simons',      tag: 'Quant · Pattern'},
};
const REGIME_META = {
  'risk-on':     {icon:'📈', label:'RISK-ON',      cls:'risk-on'},
  'neutral':     {icon:'⚖️', label:'NEUTRAL',      cls:'neutral'},
  'risk-off':    {icon:'🛡️', label:'RISK-OFF',     cls:'risk-off'},
  'deleveraging':{icon:'⚠️', label:'DELEVERAGING', cls:'deleveraging'},
};

// ── Compact agent tile builder ────────────────────────────────────────
function agentCard(key, data, contrib) {
  if (!data) return '';
  const m      = AGENT_META[key] || {name: key, tag: ''};
  const signal = (data.signal || 'watch').toLowerCase();
  const conf   = ((data.confidence || 0) * 100).toFixed(0);
  const ewt    = contrib ? ((contrib.effective_weight || 0) * 100).toFixed(1) + '%' : '';
  return `<div class="agent-tile signal-${signal}">
  <div class="tile-name">${esc(m.name)}</div>
  <div class="tile-tag">${esc(m.tag)}</div>
  <span class="badge badge-${signal}">${signal.toUpperCase()}</span>
  <div class="tile-conf">${conf}% confidence</div>
  ${ewt ? `<div class="tile-wt">${ewt} weight</div>` : ''}
</div>`;
}

// ── Compact detail row builder ────────────────────────────────────────
function buildDetailHTML(ticker) {
  const d = DETAIL_DATA[ticker];
  if (!d) return '<div style="padding:20px;color:var(--text-muted)">No data for ' + esc(ticker) + '</div>';

  const agents   = d.agents   || {};
  const contribs = d.contributions || {};

  const tilesHtml = ['graham','buffett','lynch','simons'].map(a =>
    agentCard(a, agents[a], contribs[a])
  ).join('');

  const score = d.score || 0;
  const rec   = (d.recommendation || 'watch').toLowerCase();
  const pct   = (score * 100).toFixed(2);
  const rm    = REGIME_META[d.regime_flag] || REGIME_META['neutral'];
  const vetoNote = d.veto
    ? '<div class="score-veto">\u26a1 Deleveraging veto applied \u2014 buy signals capped to watch</div>'
    : '';

  return `<div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">${rm.icon} Macro: <strong>${rm.label}</strong></div>
<div class="agents-compact">${tilesHtml}</div>
<div class="score-row">
  <div><div class="score-row-num">${score.toFixed(3)}</div><div class="score-row-label">weighted score</div></div>
  <span class="badge badge-${rec}" style="font-size:13px;padding:4px 10px">${rec.toUpperCase()}</span>
  <div class="score-needle-mini"><div class="sneedle-dot" style="left:${pct}%"></div></div>
</div>${vetoNote}`;
}

// ── Init ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // Build row cache and O(1) ticker lookup map once; reused by every applyView/toggleDetail call
  _rowCache = Array.from(document.querySelectorAll('.stock-row'));
  _rowMap   = new Map(_rowCache.map(r => [r.dataset.ticker, r]));
  applyView();
});
"""


# ---------------------------------------------------------------------------
# Shared section builders (single-ticker + detail rows)
# ---------------------------------------------------------------------------

def _dim_bar_html(dim: str, score: float) -> str:
    pct  = abs(score) * 50
    sign = "+" if score >= 0 else ""
    fill = (
        f'<div class="dim-fill-pos" style="width:{pct:.1f}%"></div>'
        if score >= 0 else
        f'<div class="dim-fill-neg" style="width:{pct:.1f}%"></div>'
    )
    return f"""
    <div class="dim-row">
      <span class="dim-label">{_e(dim.replace("_"," "))}</span>
      <div class="dim-track">{fill}</div>
      <span class="dim-val">{sign}{score:.2f}</span>
    </div>"""


def _build_agent_card(agent: str, result: dict, contribution: dict | None = None) -> str:
    meta   = _AGENT_META.get(agent, {"name": agent, "tag": ""})
    signal = (result.get("signal") or "watch").lower()
    conf   = float(result.get("confidence") or 0)
    rsn    = _e(result.get("reasoning", ""))
    dims   = result.get("dimension_scores") or {}

    dim_rows    = "".join(_dim_bar_html(k, v) for k, v in dims.items())
    weight_info = ""
    if contribution:
        ew = contribution.get("effective_weight", 0)
        weight_info = f'<span class="confidence-val" title="Effective weight">{ew:.1%} wt</span>'

    # Quick-reference info panel — panel_id uses agent key only (single-ticker: one per page)
    ref      = _PERSONA_REF.get(agent)
    panel_id = f"ref-{agent}"
    info_btn = (
        f'<button class="info-btn" onclick="toggleRef(event,\'{panel_id}\')" title="Quick reference">ⓘ</button>'
        if ref else ""
    )
    ref_panel = f"""<div class="agent-ref-panel" id="{panel_id}">
    <div class="ref-row"><span class="ref-label">Philosophy</span><span class="ref-value">{_e(ref['philosophy'])}</span></div>
    <div class="ref-row"><span class="ref-label">Metrics</span><span class="ref-value">{_e(ref['metrics'])}</span></div>
    <div class="ref-row"><span class="ref-label">🟢 Buy</span><span class="ref-value ref-buy">{_e(ref['buy'])}</span></div>
    <div class="ref-row"><span class="ref-label">🔴 Avoid</span><span class="ref-value ref-avoid">{_e(ref['avoid'])}</span></div>
  </div>""" if ref else ""

    return f"""
<div class="agent-card signal-{_e(signal)}">
  <div class="card-header">
    <div>
      <div class="agent-name">{_e(meta['name'])}{info_btn}</div>
      <div class="agent-tag">{_e(meta['tag'])}</div>
    </div>
    <span class="badge badge-{_e(signal)}">{signal.upper()}</span>
  </div>
  {ref_panel}
  <div class="confidence-row">
    <span>Confidence</span>
    <div class="confidence-track">
      <div class="confidence-fill" style="width:{conf*100:.0f}%"></div>
    </div>
    <span class="confidence-val">{conf:.0%}</span>
    {weight_info}
  </div>
  <p class="reasoning">{rsn}</p>
  <div class="dim-scores">{dim_rows}</div>
</div>"""


def _classify_ind(label: str, value) -> str:
    if value is None:
        return "neutral"
    lbl = label.lower()
    if "macd" in lbl or "cross" in lbl:
        return "bullish" if ("bullish" in str(value).lower() or "golden" in str(value).lower()) else "bearish"
    if "rsi" in lbl:
        v = float(value)
        return "bullish" if v > 60 else "bearish" if v < 40 else "neutral"
    if "momentum" in lbl or "vs ma" in lbl or "52w" in lbl:
        try:
            return "bullish" if float(str(value).replace("%","")) > 0 else "bearish"
        except ValueError:
            return "neutral"
    return "neutral"


def _build_simons_quant(simons_result: dict, stock_data: dict) -> str:
    t   = stock_data.get("technicals") or {}
    mom = t.get("momentum") or {}
    bb  = t.get("bollinger_bands") or {}
    mcd = t.get("macd") or {}

    def _ind(label: str, value, note: str = "") -> str:
        cls = _classify_ind(label, value)
        return f"""
    <div class="indicator">
      <div class="ind-label">{_e(label)}</div>
      <div class="ind-value {cls}">{_e(value) if value is not None else '—'}</div>
      <div class="ind-note">{_e(note)}</div>
    </div>"""

    rsi    = t.get("rsi_14")
    pvma50 = t.get("price_vs_ma50")
    pvma200= t.get("price_vs_ma200")
    vol_r  = t.get("volume_ratio_20d")
    m30    = mom.get("30d")
    m90    = mom.get("90d")
    hi52   = t.get("52w_pct_from_high")

    indicators = (
        _ind("RSI (14)",     rsi,                              "overbought >70, oversold <30") +
        _ind("MACD",         mcd.get("crossover"),             f"histogram: {mcd.get('histogram','—')}") +
        _ind("MA Cross",     t.get("ma_cross"),                "golden = bullish, death = bearish") +
        _ind("vs MA50",      f"{pvma50:+.1f}%"  if pvma50  is not None else None, "% above/below 50d avg") +
        _ind("vs MA200",     f"{pvma200:+.1f}%" if pvma200 is not None else None, "% above/below 200d avg") +
        _ind("BB Position",  bb.get("position"),               "0=lower band, 1=upper band") +
        _ind("Vol Ratio",    f"{vol_r:.1f}×"    if vol_r   is not None else None, "vs 20-day avg volume") +
        _ind("Momentum 30d", f"{m30:+.1f}%"     if m30     is not None else None, "") +
        _ind("Momentum 90d", f"{m90:+.1f}%"     if m90     is not None else None, "") +
        _ind("vs 52W High",  f"{hi52:+.1f}%"    if hi52    is not None else None, "")
    )

    simons_signal = (simons_result.get("signal") or "watch").lower()
    simons_conf   = float(simons_result.get("confidence") or 0)

    # Quick-reference panel for Simons (single-ticker mode; panel_id is page-unique)
    simons_ref = _PERSONA_REF.get("simons")
    si_info_btn = (
        '<button class="info-btn" onclick="toggleRef(event,\'ref-simons\')" title="Quick reference">ⓘ</button>'
        if simons_ref else ""
    )
    si_ref_panel = f"""<div class="agent-ref-panel" id="ref-simons">
    <div class="ref-row"><span class="ref-label">Philosophy</span><span class="ref-value">{_e(simons_ref['philosophy'])}</span></div>
    <div class="ref-row"><span class="ref-label">Metrics</span><span class="ref-value">{_e(simons_ref['metrics'])}</span></div>
    <div class="ref-row"><span class="ref-label">🟢 Buy</span><span class="ref-value ref-buy">{_e(simons_ref['buy'])}</span></div>
    <div class="ref-row"><span class="ref-label">🔴 Avoid</span><span class="ref-value ref-avoid">{_e(simons_ref['avoid'])}</span></div>
  </div>""" if simons_ref else ""

    return f"""
<div class="quant-bar">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
    <div class="section-title" style="margin:0">JIM SIMONS — QUANT INDICATORS{si_info_btn}</div>
    <span class="badge badge-{_e(simons_signal)}">{simons_signal.upper()} · {simons_conf:.0%}</span>
  </div>
  {si_ref_panel}
  <div class="quant-indicators">{indicators}</div>
</div>"""


def _build_verdict(score_result: dict) -> str:
    score  = score_result.get("final_score", 0)
    rec    = score_result.get("recommendation", "WATCH")
    veto   = score_result.get("deleveraging_veto_applied", False)
    needle = score * 100

    consensus = score_result.get("consensus") or {}
    cons_html = []
    for sig in ("buy", "watch", "avoid"):
        agents = consensus.get(sig) or []
        if agents:
            pills = "".join(f'<span class="consensus-agent {sig}">{_e(a)}</span>' for a in agents)
            cons_html.append(
                f'<div class="consensus-group"><span class="consensus-label">{sig.upper()}</span>{pills}</div>'
            )

    veto_note = ""
    if veto:
        veto_note = '<div style="margin-top:12px;font-size:12px;color:var(--red-light)">⚡ Deleveraging veto applied — buy signals capped to watch</div>'

    return f"""
<div class="verdict">
  <div class="section-title">FINAL WEIGHTED VERDICT</div>
  <div class="verdict-header">
    <div class="verdict-rec {rec}">{rec}</div>
    <div class="score-display">
      <div class="score-number">{score:.3f}</div>
      <div class="score-label">weighted score (0–1)</div>
    </div>
  </div>
  <div class="score-bar-wrap">
    <div class="score-track">
      <div class="score-zone-avoid"></div>
      <div class="score-zone-watch"></div>
      <div class="score-zone-buy"></div>
    </div>
    <div class="score-needle-wrap">
      <div class="score-needle" style="left:{needle:.2f}%"></div>
    </div>
    <div class="score-zone-label">
      <span>AVOID (0–0.49)</span>
      <span>WATCH</span>
      <span style="text-align:right">BUY (0.70–1.0)</span>
    </div>
  </div>
  <div class="consensus-row">{''.join(cons_html)}</div>
  {veto_note}
</div>"""


def _build_regime_banner(dalio_result: dict, score_result: dict) -> str:
    regime  = score_result.get("regime_flag", "neutral")
    meta    = _REGIME_META.get(regime, _REGIME_META["neutral"])
    weights = score_result.get("applied_weights") or {}
    veto    = score_result.get("deleveraging_veto_applied", False)

    pill_html = []
    for agent, w in weights.items():
        nw = _NEUTRAL_WEIGHTS.get(agent, 0)
        if w > nw + 0.03:
            cls = "elevated"
        elif w < nw - 0.03:
            cls = "reduced"
        else:
            cls = ""
        pill_html.append(
            f'<span class="weight-pill {cls}">{agent.capitalize()} {w:.0%}</span>'
        )

    veto_badge = '<span class="veto-pill">⚡ Veto Active</span>' if veto else ""

    return f"""
<div class="regime-banner {meta['cls']}">
  <div class="regime-left">
    <span class="regime-icon">{meta['icon']}</span>
    <div>
      <div><span class="regime-label">{meta['label']} REGIME</span>{veto_badge}</div>
      <div class="regime-sub">Dalio: <strong>{_e(dalio_result.get("signal","—"))}</strong>
        · {_e((dalio_result.get("reasoning") or "")[:90])}…</div>
    </div>
  </div>
  <div class="weight-pills">{''.join(pill_html)}</div>
</div>"""


def _build_header(stock_data: dict) -> str:
    m     = stock_data.get("meta") or {}
    price = m.get("price", 0)
    chg   = m.get("day_change_pct")

    if chg is None:
        chg_html = ""
    elif chg >= 0:
        chg_html = f'<span class="change-pos">▲ {chg:.2f}%</span>'
    else:
        chg_html = f'<span class="change-neg">▼ {abs(chg):.2f}%</span>'

    return f"""
<header class="ticker-header">
  <div class="header-left">
    <div class="ticker-symbol">{_e(m.get("ticker",""))}</div>
    <div class="company-name">{_e(m.get("name",""))}</div>
    <span class="sector-tag">{_e(m.get("sector",""))} · {_e(m.get("industry",""))}</span>
  </div>
  <div class="header-right">
    <div class="price-large">{_e(m.get("currency","USD"))} {_e(price)}{chg_html}</div>
    <div class="header-meta">{_e(m.get("exchange",""))} · {_e((m.get("fetched_at",""))[:10])}</div>
  </div>
</header>"""


# ---------------------------------------------------------------------------
# Single-ticker full HTML builder
# ---------------------------------------------------------------------------

def build_html(stock_data: dict, score_result: dict, agent_results: dict) -> str:
    """Build a self-contained single-ticker report HTML string."""
    ticker   = score_result.get("ticker", "")
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    dalio    = agent_results.get("dalio") or {}
    contribs = score_result.get("agent_contributions") or {}

    dalio_card  = _build_agent_card("dalio", dalio, contribs.get("dalio"))
    other_cards = "".join(
        _build_agent_card(a, agent_results.get(a) or {}, contribs.get(a))
        for a in ("graham", "buffett", "lynch")
        if a in agent_results
    )
    simons = agent_results.get("simons") or {}

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET SIGNUM — {_e(ticker)}</title>
<style>{_css()}</style>
</head>
<body>
<div class="container">

{_build_header(stock_data)}
{_build_regime_banner(dalio, score_result)}

<div class="section-title">DALIO MACRO ANALYSIS</div>
<div class="agents-grid" style="margin-bottom:20px">{dalio_card}</div>

<div class="section-title">PERSONA ANALYSIS</div>
<div class="agents-grid">{other_cards}</div>

{_build_simons_quant(simons, stock_data) if simons else ""}
{_build_verdict(score_result)}

<footer>
  MARKET SIGNUM · Generated {now_str} · Data via yfinance · Educational use only — not investment advice
</footer>
</div>
<script>{_js()}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Master dashboard builders
# ---------------------------------------------------------------------------

def _signal_dot(signal: str | None, title: str = "") -> str:
    cls = (signal or "").lower() if signal else "na"
    t   = f' title="{_e(title)}"' if title else ""
    return f'<span class="sdot {cls}"{t}></span>'


def _extract_detail_data(result: dict) -> dict:
    """
    Extract the compact data needed to build an expandable detail row in JS.
    Price history is excluded (large, not needed for detail view).
    """
    stock_data   = result.get("stock_data")   or {}
    agent_results= result.get("agent_results") or {}
    score_result = result.get("score_result")  or {}

    meta = stock_data.get("meta") or {}

    # Compact per-agent data — signal + confidence only (reasoning/dims removed for performance)
    agents = {}
    for a in _AGENTS:
        ar = agent_results.get(a)
        if ar:
            agents[a] = {
                "signal":     ar.get("signal"),
                "confidence": ar.get("confidence"),
            }

    return {
        "name":           meta.get("name", result["ticker"]),
        "sector":         meta.get("sector", ""),
        "price":          meta.get("price"),
        "regime_flag":    score_result.get("regime_flag", "neutral"),
        "agents":         agents,
        "score":          score_result.get("final_score", 0),
        "recommendation": score_result.get("recommendation", "WATCH"),
        "veto":           score_result.get("deleveraging_veto_applied", False),
        "contributions":  {
            k: {"effective_weight": v.get("effective_weight", 0)}
            for k, v in (score_result.get("agent_contributions") or {}).items()
        },
    }


def _build_table_row(rank: int, result: dict) -> str:
    ticker = result["ticker"]
    sd     = result.get("stock_data")   or {}
    sr     = result.get("score_result") or {}
    ar     = result.get("agent_results") or {}

    meta  = sd.get("meta")         or {}
    fund  = sd.get("fundamentals") or {}

    name    = meta.get("name", ticker)
    sector  = meta.get("sector", "Unknown")
    price   = meta.get("price")
    mkt_cap = fund.get("market_cap")
    score   = sr.get("final_score", 0)
    rec     = sr.get("recommendation", "WATCH")
    regime  = sr.get("regime_flag", "neutral")

    g_sig  = (ar.get("graham",  {}) or {}).get("signal", "")
    bu_sig = (ar.get("buffett", {}) or {}).get("signal", "")
    ly_sig = (ar.get("lynch",   {}) or {}).get("signal", "")
    si_sig = (ar.get("simons",  {}) or {}).get("signal", "")

    score_pct = f"{score * 100:.1f}"
    rec_lc    = rec.lower()
    rm        = _REGIME_META.get(regime, _REGIME_META["neutral"])
    price_str = f"${price:.2f}" if price is not None else "—"
    cap_str   = _fmt_cap(mkt_cap)

    # Compact company name for table (truncate at 22 chars)
    name_short = (name[:22] + "…") if len(name) > 22 else name

    return f"""<tr class="stock-row"
  data-ticker="{_e(ticker)}"
  data-score="{score:.4f}"
  data-rec="{_e(rec)}"
  data-sector="{_e(sector)}"
  data-price="{price or 0}"
  data-mktcap="{mkt_cap or 0}"
  data-name="{_e(name)}"
  data-graham="{_e(g_sig)}"
  data-buffett="{_e(bu_sig)}"
  data-lynch="{_e(ly_sig)}"
  data-simons="{_e(si_sig)}"
  onclick="toggleDetail('{_ej(ticker)}')"
>
  <td class="rank-cell">{rank}</td>
  <td class="ticker-cell">{_e(ticker)}<br><span class="ticker-name">{_e(name_short)}</span></td>
  <td><div class="mini-score"><span class="mini-score-num">{score:.3f}</span><div class="mini-score-bar"><div class="mini-score-fill {rec_lc}" style="width:{score_pct}%"></div></div></div></td>
  <td><span class="badge badge-{rec_lc}">{_e(rec)}</span></td>
  <td>{_signal_dot(g_sig, g_sig.upper() if g_sig else "N/A")}</td>
  <td>{_signal_dot(bu_sig, bu_sig.upper() if bu_sig else "N/A")}</td>
  <td>{_signal_dot(ly_sig, ly_sig.upper() if ly_sig else "N/A")}</td>
  <td>{_signal_dot(si_sig, si_sig.upper() if si_sig else "N/A")}</td>
  <td><span title="{rm['label']}">{rm['icon']}</span></td>
  <td>{_e(sector)}</td>
  <td class="mono">{_e(price_str)}</td>
  <td class="mono">{_e(cap_str)}</td>
  <td><span class="expand-ind"></span></td>
</tr>"""


def _build_dashboard_html(results: list[dict], dalio_result: dict | None) -> str:
    """Build the master dashboard HTML string for a batch run."""
    if not dalio_result:
        dalio_result = {"signal": "watch", "confidence": 0.5, "regime_flag": "neutral", "reasoning": ""}

    now_str    = datetime.now().strftime("%Y-%m-%d %H:%M")
    regime     = dalio_result.get("regime_flag", "neutral")
    rm         = _REGIME_META.get(regime, _REGIME_META["neutral"])

    # Sort results by score descending (default view)
    sorted_results = sorted(results, key=lambda r: r.get("score_result", {}).get("final_score", 0), reverse=True)

    # Counts
    buy_n   = sum(1 for r in results if (r.get("score_result") or {}).get("recommendation") == "BUY")
    watch_n = sum(1 for r in results if (r.get("score_result") or {}).get("recommendation") == "WATCH")
    avoid_n = sum(1 for r in results if (r.get("score_result") or {}).get("recommendation") == "AVOID")
    total   = len(results)

    # Unique sectors for filter dropdown
    sectors = sorted(set(
        (r.get("stock_data") or {}).get("meta", {}).get("sector", "Unknown")
        for r in results
    ))
    sector_options = "\n".join(
        f'<option value="{_e(s)}">{_e(s)}</option>' for s in sectors
    )

    # Dalio regime banner (simplified — uses dalio_result directly)
    regime_banner_html = f"""
<div class="regime-banner {rm['cls']}" style="margin-bottom:12px">
  <div class="regime-left">
    <span class="regime-icon">{rm['icon']}</span>
    <div>
      <div><span class="regime-label">{rm['label']} REGIME</span></div>
      <div class="regime-sub">Dalio macro: <strong>{_e(dalio_result.get("signal","—"))}</strong>
        · {_e((dalio_result.get("reasoning") or "")[:100])}…</div>
    </div>
  </div>
  <div class="weight-pills">
    {_build_weight_pills(regime)}
  </div>
</div>"""

    # Table rows
    rows_html = "\n".join(
        _build_table_row(i + 1, r) for i, r in enumerate(sorted_results)
    )

    # Embedded detail data as JSON
    detail_data: dict[str, dict] = {}
    for r in sorted_results:
        ticker = r["ticker"]
        detail_data[ticker] = _extract_detail_data(r)

    detail_json = json.dumps(detail_data, ensure_ascii=False, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET SIGNUM — NYSE Batch Scan {now_str[:10]}</title>
<style>{_css()}</style>
</head>
<body>
<div class="container">

<!-- Header -->
<header class="dash-header">
  <div>
    <div class="dash-title">MARKET SIGNUM</div>
    <div class="dash-subtitle">NYSE Batch Scan · {total} stocks analyzed · {rm['icon']} {rm['label']} regime</div>
  </div>
  <div class="dash-meta">
    Generated: {_e(now_str)}<br>
    Data via yfinance · Educational use only
  </div>
</header>

{regime_banner_html}

{_build_personas_html()}

<!-- Summary bar -->
<div class="summary-bar">
  <div class="summary-stat">
    <div class="summary-num">{total}</div>
    <div class="summary-label">Analyzed</div>
  </div>
  <div class="summary-stat">
    <div class="summary-num buy">{buy_n}</div>
    <div class="summary-label">🟢 BUY</div>
  </div>
  <div class="summary-stat">
    <div class="summary-num watch">{watch_n}</div>
    <div class="summary-label">🟡 WATCH</div>
  </div>
  <div class="summary-stat">
    <div class="summary-num avoid">{avoid_n}</div>
    <div class="summary-label">🔴 AVOID</div>
  </div>
</div>

<!-- Filter bar -->
<div class="filter-bar">
  <div class="filter-group">
    <button class="filter-btn active-all" data-rec="all"   onclick="setRecFilter('all')">All</button>
    <button class="filter-btn"            data-rec="BUY"   onclick="setRecFilter('BUY')">🟢 BUY</button>
    <button class="filter-btn"            data-rec="WATCH" onclick="setRecFilter('WATCH')">🟡 WATCH</button>
    <button class="filter-btn"            data-rec="AVOID" onclick="setRecFilter('AVOID')">🔴 AVOID</button>
  </div>
  <select class="filter-select" onchange="setSectorFilter(this.value)">
    <option value="all">All Sectors</option>
    {sector_options}
  </select>
  <input class="filter-search" type="text" placeholder="Search ticker or company…" oninput="setSearch(this.value)">
</div>
<div class="showing-count" id="showing-count">Showing {total} of {total} stocks</div>

<!-- Results table -->
<div class="results-wrap">
<table class="results-table">
  <thead>
    <tr>
      <th class="no-sort">#</th>
      <th data-col="ticker"  onclick="sortBy('ticker')">Ticker</th>
      <th data-col="score"   onclick="sortBy('score')">Score</th>
      <th data-col="rec"     onclick="sortBy('rec')">Rec</th>
      <th data-col="g"       onclick="sortBy('g')"  title="Graham">G</th>
      <th data-col="bu"      onclick="sortBy('bu')" title="Buffett">Bu</th>
      <th data-col="ly"      onclick="sortBy('ly')" title="Lynch">Ly</th>
      <th data-col="si"      onclick="sortBy('si')" title="Simons">Si</th>
      <th class="no-sort"    title="Dalio Regime">Regime</th>
      <th data-col="sector"  onclick="sortBy('sector')">Sector</th>
      <th data-col="price"   onclick="sortBy('price')">Price</th>
      <th data-col="mktcap"  onclick="sortBy('mktcap')">Mkt Cap</th>
      <th class="no-sort"></th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
</div>

<footer>
  MARKET SIGNUM · Generated {_e(now_str)} · {_e(total)} stocks · Data via yfinance · Educational use only — not investment advice
</footer>
</div>

<script>
const DETAIL_DATA = {detail_json};
{_js()}
</script>
</body>
</html>"""


def _build_weight_pills(regime: str) -> str:
    """Render weight pills for the regime banner in the master dashboard."""
    _RAW = {
        "neutral":     {"graham": 15, "buffett": 25, "dalio": 20, "lynch": 20, "simons": 20},
        "risk-on":     {"graham":  5, "buffett": 25, "dalio": 20, "lynch": 40, "simons": 30},
        "risk-off":    {"graham": 35, "buffett": 35, "dalio": 20, "lynch":  5, "simons": 15},
        "deleveraging":{"graham": 45, "buffett": 25, "dalio": 20, "lynch": 20, "simons": 20},
    }
    raw = _RAW.get(regime, _RAW["neutral"])
    total = sum(raw.values())
    pills = []
    for agent, rw in raw.items():
        w   = rw / total
        nw  = _NEUTRAL_WEIGHTS[agent]
        cls = "elevated" if w > nw + 0.03 else "reduced" if w < nw - 0.03 else ""
        pills.append(f'<span class="weight-pill {cls}">{agent.capitalize()} {w:.0%}</span>')
    return "".join(pills)


def _build_personas_html() -> str:
    """Collapsible Personas reference section — server-rendered, batch dashboard only."""
    agent_order = ["graham", "buffett", "dalio", "lynch", "simons"]
    cards = []
    for key in agent_order:
        ref  = _PERSONA_REF.get(key, {})
        meta = _AGENT_META.get(key, {"name": key, "tag": ""})
        buy_lbl   = "Risk-on"  if key == "dalio" else "Buy when"
        avoid_lbl = "Risk-off" if key == "dalio" else "Avoid when"
        cards.append(f"""<div class="persona-card">
  <div class="persona-card-name">{_e(meta['name'])}</div>
  <div class="persona-card-tag">{_e(meta['tag'])}</div>
  <div class="persona-row">
    <span class="persona-lbl">Philosophy</span>
    <span class="persona-val">{_e(ref.get('philosophy', ''))}</span>
  </div>
  <div class="persona-row">
    <span class="persona-lbl">Metrics</span>
    <span class="persona-val">{_e(ref.get('metrics', ''))}</span>
  </div>
  <div class="persona-row">
    <span class="persona-lbl persona-buy">&#x1F7E2; {_e(buy_lbl)}</span>
    <span class="persona-val persona-buy">{_e(ref.get('buy', ''))}</span>
  </div>
  <div class="persona-row">
    <span class="persona-lbl persona-avoid">&#x1F534; {_e(avoid_lbl)}</span>
    <span class="persona-val persona-avoid">{_e(ref.get('avoid', ''))}</span>
  </div>
</div>""")
    cards_html = "\n".join(cards)
    return f"""<div class="personas-section">
  <div class="personas-header" onclick="togglePersonas()">
    <span class="personas-title">&#128100; Persona Reference</span>
    <button class="personas-toggle" id="personas-toggle">&#9660; Expand</button>
  </div>
  <div class="personas-body" id="personas-body">
    <div class="personas-grid">
{cards_html}
    </div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Private renderers
# ---------------------------------------------------------------------------

def _render_single(ticker: str, output_path: Path | None = None) -> Path:
    """Load from .tmp/ files and write a single-ticker report."""
    ticker = ticker.upper()

    def _load(name: str) -> dict | None:
        p = TMP_DIR / f"{ticker}_{name}.json"
        return json.loads(p.read_text()) if p.exists() else None

    stock_path = TMP_DIR / f"{ticker}.json"
    if not stock_path.exists():
        raise FileNotFoundError(
            f"Stock data not found: {stock_path}\nRun: python tools/fetch_stock_data.py {ticker}"
        )

    stock_data   = json.loads(stock_path.read_text())
    score_result = _load("score")
    if score_result is None:
        raise FileNotFoundError(
            f"Score not found: .tmp/{ticker}_score.json\nRun: python tools/score_and_weight.py {ticker}"
        )

    agent_results = {a: _load(a) for a in _AGENTS if _load(a)}

    html_content = build_html(stock_data, score_result, agent_results)

    out = output_path or (OUTPUT_DIR / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_content, encoding="utf-8")

    # Also write to project root as index.html for GitHub Pages
    (ROOT / "index.html").write_text(html_content, encoding="utf-8")

    return out


def _render_batch(
    results: list[dict],
    dalio_result: dict | None,
    output_path: Path | None = None,
) -> Path:
    """Build the master dashboard for a batch run and write to disk."""
    html_content = _build_dashboard_html(results, dalio_result)
    out = output_path or (OUTPUT_DIR / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_content, encoding="utf-8")

    # Also write to project root as index.html for GitHub Pages
    (ROOT / "index.html").write_text(html_content, encoding="utf-8")

    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_html(results_or_ticker, dalio_result=None, output_path=None) -> Path:
    """
    Flexible render entry point — handles both batch and single-ticker mode.

    Batch mode (called by batch_runner.py):
        render_html(results: list[dict], dalio_result=dict)

    Single-ticker mode (CLI or manual pipeline):
        render_html("AAPL")
        render_html("AAPL", output_path=Path("custom.html"))
        render_html("AAPL", Path("custom.html"))   ← positional compat
    """
    if isinstance(results_or_ticker, str):
        # Single ticker — detect output_path from either argument position
        out = output_path or (dalio_result if isinstance(dalio_result, Path) else None)
        return _render_single(results_or_ticker, out)
    else:
        # Batch mode
        return _render_batch(results_or_ticker, dalio_result, output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python render_html.py <TICKER> [--out path/to/report.html]")
        sys.exit(1)

    ticker = sys.argv[1]
    out    = None

    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out = Path(sys.argv[idx + 1])

    print(f"\nRendering report for {ticker.upper()}...")
    try:
        path = render_html(ticker, output_path=out)
        print(f"  [OK] Report written -> {path}")
        print(f"  Open in browser: file:///{path.as_posix()}\n")
    except FileNotFoundError as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
