#!/usr/bin/env python3
"""
buffett_agent.py — Warren Buffett Quality & Moat Analysis

Buffett looks for wonderful companies at fair prices. Unlike Graham,
he is willing to pay more for a business with a durable competitive
advantage (moat), consistent high ROE, and strong free cash flow.
He is more willing to buy than Graham but more selective than Lynch.

Scoring dimensions (all in [-1, +1]):
  business_quality  (40%) — ROE, net profit margin
  moat_strength     (30%) — gross margin, operating margin
  cash_generation   (20%) — free cash flow presence and yield
  fair_value        (10%) — P/E (lenient — pays up for quality)

Signal thresholds:
  score >= 0.30 → buy   (wonderful company at fair-to-good price)
  score >= 0.05 → watch (quality signals present but price or data insufficient)
  score <  0.05 → avoid

Output contract:
  {
    "agent":            "buffett",
    "ticker":           str,
    "signal":           "buy" | "watch" | "avoid",
    "confidence":       0.0–1.0,
    "reasoning":        str,
    "dimension_scores": { business_quality, moat_strength,
                          cash_generation, fair_value },
    "missing_fields":   [ str, ... ],
    "run_at":           ISO timestamp
  }

Usage:
  python buffett_agent.py AAPL
  python buffett_agent.py AAPL --no-cache
  ANTHROPIC_API_KEY=sk-ant-... python buffett_agent.py AAPL
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

CLAUDE_MODEL = "claude-sonnet-4-6"

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_roe(roe) -> tuple[float, str]:
    """Return on equity — Buffett's primary quality metric."""
    if roe is None:  return  0.00, "ROE unavailable"
    if roe > 25:     return +0.90, f"ROE {roe:.1f}% — exceptional business economics"
    if roe > 20:     return +0.60, f"ROE {roe:.1f}% — strong, consistently above Buffett's threshold"
    if roe > 15:     return +0.30, f"ROE {roe:.1f}% — meets Buffett's 15% minimum"
    if roe > 10:     return -0.10, f"ROE {roe:.1f}% — below Buffett's preferred threshold"
    if roe > 0:      return -0.50, f"ROE {roe:.1f}% — poor returns on equity"
    return                  -1.00, f"ROE {roe:.1f}% — destroying shareholder value"


def _score_profit_margin(margin) -> tuple[float, str]:
    if margin is None: return  0.00, "Net margin unavailable"
    if margin > 20:    return +0.70, f"Net margin {margin:.1f}% — premium business with pricing power"
    if margin > 15:    return +0.40, f"Net margin {margin:.1f}% — healthy, consistent profitability"
    if margin > 10:    return +0.20, f"Net margin {margin:.1f}% — solid profitability"
    if margin > 5:     return  0.00, f"Net margin {margin:.1f}% — adequate but not exceptional"
    if margin >= 0:    return -0.20, f"Net margin {margin:.1f}% — thin margins, no pricing power"
    return                    -0.80, f"Net margin {margin:.1f}% — unprofitable"


def _score_gross_margin(gm) -> tuple[float, str]:
    """
    Gross margin is Buffett's best proxy for moat.
    High gross margins signal pricing power and switching costs.
    """
    if gm is None: return  0.00, "Gross margin unavailable"
    if gm > 55:    return +0.80, f"Gross margin {gm:.1f}% — exceptional moat (software/pharma/luxury tier)"
    if gm > 40:    return +0.50, f"Gross margin {gm:.1f}% — strong moat, meaningful pricing power"
    if gm > 30:    return +0.20, f"Gross margin {gm:.1f}% — moderate moat"
    if gm > 20:    return -0.10, f"Gross margin {gm:.1f}% — thin competitive advantage"
    return                -0.50, f"Gross margin {gm:.1f}% — commodity-like economics, no moat evident"


def _score_operating_margin(om) -> tuple[float, str]:
    if om is None: return  0.00, "Operating margin unavailable"
    if om > 25:    return +0.50, f"Operating margin {om:.1f}% — best-in-class operational efficiency"
    if om > 15:    return +0.30, f"Operating margin {om:.1f}% — above-average operating leverage"
    if om > 8:     return +0.10, f"Operating margin {om:.1f}% — acceptable operating efficiency"
    if om >= 0:    return -0.10, f"Operating margin {om:.1f}% — thin operating margins"
    return                -0.70, f"Operating margin {om:.1f}% — operating at a loss"


def _score_fcf(fcf, market_cap) -> tuple[float, str]:
    """
    Free cash flow — the lifeblood of a Buffett business.
    FCF yield (FCF / market cap) contextualizes the absolute number.
    """
    if fcf is None:
        return 0.00, "Free cash flow unavailable"
    if fcf <= 0:
        return -0.60, f"FCF negative (${fcf:,.0f}) — consuming rather than generating cash"

    if market_cap and market_cap > 0:
        yield_pct = fcf / market_cap * 100
        if yield_pct > 5:    return +0.70, f"FCF yield {yield_pct:.1f}% — excellent cash generation relative to price"
        if yield_pct > 3:    return +0.50, f"FCF yield {yield_pct:.1f}% — solid free cash flow yield"
        if yield_pct > 1.5:  return +0.30, f"FCF yield {yield_pct:.1f}% — positive but modest FCF yield"
        return                      +0.10, f"FCF positive but low yield ({yield_pct:.1f}%) relative to market cap"

    # FCF positive but market cap unavailable — still a positive signal
    return +0.40, f"FCF positive (${fcf:,.0f}) — cash-generative business"


def _score_pe_buffett(pe) -> tuple[float, str]:
    """
    Buffett's P/E lens — more lenient than Graham's.
    He pays up for quality but won't chase speculation.
    """
    if pe is None:  return  0.00, "P/E unavailable"
    if pe <= 0:     return -0.60, f"P/E negative — company unprofitable"
    if pe < 15:     return +0.40, f"P/E {pe} — cheap for quality, Buffett would be interested"
    if pe < 25:     return +0.20, f"P/E {pe} — fair valuation for a quality business"
    if pe < 35:     return  0.00, f"P/E {pe} — acceptable if moat and growth justify it"
    if pe < 50:     return -0.20, f"P/E {pe} — getting expensive even for quality"
    return                 -0.50, f"P/E {pe} — price embeds very high expectations"


def _score_buffett(data: dict) -> tuple[float, dict, dict, list]:
    """
    Score stock on Buffett's quality + moat criteria.
    Returns: (composite [-1,+1], dim_scores, notes, missing_fields)
    """
    f    = data.get("fundamentals", {})
    missing: list[str] = []

    def _get(key):
        v = f.get(key)
        if v is None:
            missing.append(key)
        return v

    roe    = _get("return_on_equity")
    margin = _get("profit_margin")
    gm     = _get("gross_margin")
    om     = _get("operating_margin")
    fcf    = _get("free_cash_flow")
    mktcap = f.get("market_cap")  # not a scored metric, used for FCF yield
    pe     = _get("pe_ratio")

    roe_s,  roe_n  = _score_roe(roe)
    mgn_s,  mgn_n  = _score_profit_margin(margin)
    gm_s,   gm_n   = _score_gross_margin(gm)
    om_s,   om_n   = _score_operating_margin(om)
    fcf_s,  fcf_n  = _score_fcf(fcf, mktcap)
    pe_s,   pe_n   = _score_pe_buffett(pe)

    business_quality = roe_s * 0.55 + mgn_s * 0.45
    moat_strength    = gm_s  * 0.60 + om_s  * 0.40
    cash_generation  = fcf_s
    fair_value       = pe_s

    composite = (
        business_quality * 0.40 +
        moat_strength    * 0.30 +
        cash_generation  * 0.20 +
        fair_value       * 0.10
    )

    scores = {
        "business_quality": round(business_quality, 3),
        "moat_strength":    round(moat_strength, 3),
        "cash_generation":  round(cash_generation, 3),
        "fair_value":       round(fair_value, 3),
    }
    notes = {
        "business_quality": [roe_n, mgn_n],
        "moat_strength":    [gm_n, om_n],
        "cash_generation":  [fcf_n],
        "fair_value":       [pe_n],
    }

    return round(max(-1.0, min(1.0, composite)), 3), scores, notes, missing


# ---------------------------------------------------------------------------
# Signal + confidence
# ---------------------------------------------------------------------------

def _to_signal(score: float) -> str:
    if score >= 0.30: return "buy"
    if score >= 0.05: return "watch"
    return "avoid"


def _to_confidence(score: float, missing: list) -> float:
    base    = 0.50 + abs(score) * 0.42
    penalty = min(0.25, len(missing) * 0.04)
    return round(max(0.25, min(0.92, base - penalty)), 2)


# ---------------------------------------------------------------------------
# LLM reasoning
# ---------------------------------------------------------------------------

def _build_prompt(ticker: str, data: dict, score: float,
                  dim_scores: dict, notes: dict) -> str:
    f = data.get("fundamentals", {})

    def _fmt(key, suffix=""):
        v = f.get(key)
        return f"{v}{suffix}" if v is not None else "N/A"

    all_notes = [n for ns in notes.values() for n in ns]
    note_text  = "\n".join(f"  - {n}" for n in all_notes)

    return f"""You are Warren Buffett evaluating {ticker} as a potential long-term holding.

Key data:
  Return on equity:   {_fmt("return_on_equity", "%")}
  Net profit margin:  {_fmt("profit_margin", "%")}
  Gross margin:       {_fmt("gross_margin", "%")}
  Operating margin:   {_fmt("operating_margin", "%")}
  Free cash flow:     {_fmt("free_cash_flow")}
  P/E ratio:          {_fmt("pe_ratio")}

Your analysis identified:
{note_text}

Composite quality score: {score:+.3f} (scale: -1 = avoid, +1 = strong buy)
Signal: {_to_signal(score).upper()}

Write 2–3 sentences as Buffett explaining your verdict on {ticker}:
- Lead with your view on the competitive moat or lack thereof
- Reference specific metrics (ROE, margins, FCF) that drove your decision
- Speak in Buffett's voice: patient, long-term, focused on business quality and compounding

Maximum 80 words. Do not mention score values numerically."""


def _generate_reasoning(ticker: str, data: dict, score: float,
                        dim_scores: dict, notes: dict) -> str:
    if _ANTHROPIC_AVAILABLE and _API_KEY:
        try:
            client  = anthropic.Anthropic(api_key=_API_KEY)
            message = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user",
                           "content": _build_prompt(ticker, data, score, dim_scores, notes)}],
            )
            return message.content[0].text.strip()
        except Exception as exc:
            print(f"  [Buffett] LLM call failed ({exc}), using fallback.", file=sys.stderr)

    return _fallback_reasoning(score, dim_scores, notes, data)


def _fallback_reasoning(score: float, dim_scores: dict,
                        notes: dict, data: dict) -> str:
    f   = data.get("fundamentals", {})
    roe = f.get("return_on_equity")
    gm  = f.get("gross_margin")
    fcf = f.get("free_cash_flow")

    if score >= 0.30:
        verdict = "This business demonstrates the quality characteristics I look for in a long-term holding."
    elif score >= 0.05:
        verdict = "There are elements of quality here but the picture is incomplete or the price is not yet compelling."
    else:
        verdict = "This business does not meet my standards for a quality compounder at any reasonable price."

    details = []
    if roe is not None:
        details.append(f"ROE of {roe:.1f}% {'demonstrates strong business economics' if roe > 15 else 'indicates below-par capital efficiency'}")
    if gm is not None:
        details.append(f"gross margin of {gm:.1f}% {'suggests a durable moat' if gm > 40 else 'indicates limited pricing power'}")
    if fcf is not None:
        details.append("free cash flow is positive — the business funds itself" if fcf > 0
                       else "negative free cash flow is a concern for long-term compounding")

    detail_str = "; ".join(details[:2])
    return f"{verdict} {detail_str}.".strip()


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def run_buffett_agent(ticker: str, stock_data: dict, cache: bool = True) -> dict:
    """
    Run Buffett quality/moat analysis on pre-fetched stock data.

    Args:
        ticker:     Stock symbol.
        stock_data: Output dict from fetch_stock_data(). Must not be None.
        cache:      Unused (scoring is deterministic). Kept for API consistency.

    Returns: Standardized agent result dict.
    """
    ticker = ticker.upper().strip()

    if "error" in stock_data:
        return {
            "agent": "buffett", "ticker": ticker,
            "signal": "avoid", "confidence": 0.25,
            "reasoning": f"Data fetch error — cannot evaluate: {stock_data['error']}",
            "dimension_scores": {}, "missing_fields": [], "run_at": datetime.now().isoformat(),
        }

    print(f"  [Buffett] Scoring quality and moat indicators...")
    score, dim_scores, notes, missing = _score_buffett(stock_data)

    signal     = _to_signal(score)
    confidence = _to_confidence(score, missing)

    print(f"  [Buffett] Score: {score:+.3f} -> {signal.upper()} ({confidence:.0%})")
    print(f"  [Buffett] Generating reasoning...")
    reasoning  = _generate_reasoning(ticker, stock_data, score, dim_scores, notes)

    result = {
        "agent":            "buffett",
        "ticker":           ticker,
        "signal":           signal,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "dimension_scores": dim_scores,
        "missing_fields":   missing,
        "run_at":           datetime.now().isoformat(),
    }

    out_path = TMP_DIR / f"{ticker}_buffett.json"
    with open(out_path, "w") as f_out:
        json.dump(result, f_out, indent=2)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SIGNAL_ICON = {"buy": "🟢 BUY", "watch": "🟡 WATCH", "avoid": "🔴 AVOID"}


def _wrap(text: str, width: int = 56, indent: str = "  ") -> str:
    words, lines, line = text.split(), [], []
    for w in words:
        if sum(len(x) + 1 for x in line) + len(w) > width:
            lines.append(indent + " ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(indent + " ".join(line))
    return "\n".join(lines)


def _mini_bar(score: float, width: int = 10) -> str:
    half   = width // 2
    filled = round(abs(score) * half)
    if score >= 0:
        return " " * half + "│" + "▓" * filled + "░" * (half - filled)
    return "░" * (half - filled) + "▓" * filled + "│" + " " * half


def main():
    if len(sys.argv) < 2:
        print("Usage: python buffett_agent.py <TICKER> [--no-cache]")
        sys.exit(1)

    ticker = sys.argv[1]
    cache  = "--no-cache" not in sys.argv

    cache_path = TMP_DIR / f"{ticker.upper()}.json"
    if cache and cache_path.exists():
        with open(cache_path) as f:
            stock_data = json.load(f)
    else:
        sys.path.insert(0, str(ROOT / "tools"))
        from fetch_stock_data import fetch_stock_data
        stock_data = fetch_stock_data(ticker, cache=cache)

    if "error" in stock_data:
        print(f"\nERROR: {stock_data['error']}")
        sys.exit(1)

    print(f"\nRunning Buffett quality agent for {ticker.upper()}...\n")
    result = run_buffett_agent(ticker, stock_data, cache=cache)

    bar = "=" * 62
    print(bar)
    print(f"  BUFFETT QUALITY  -  {result['ticker']}")
    print(bar)
    print(f"  Signal:     {_SIGNAL_ICON.get(result['signal'], result['signal'])}")
    print(f"  Confidence: {result['confidence']:.0%}")
    print()
    print("  Reasoning:")
    print(_wrap(result["reasoning"]))
    print()
    print("  Dimension Scores  (avoid <-----> buy)")
    for dim, score in result["dimension_scores"].items():
        print(f"  {dim:<18} {score:+.3f}  {_mini_bar(score)}")
    print()

    f = stock_data.get("fundamentals", {})
    def _v(k, suffix=""):
        v = f.get(k)
        return f"{v}{suffix}" if v is not None else "N/A"

    print("  Key Metrics:")
    print(f"  ROE: {_v('return_on_equity', '%')}  |  Net Margin: {_v('profit_margin', '%')}  |  Gross Margin: {_v('gross_margin', '%')}")
    print(f"  Op Margin: {_v('operating_margin', '%')}  |  FCF: {_v('free_cash_flow')}  |  P/E: {_v('pe_ratio')}")

    if result["missing_fields"]:
        print(f"\n  Missing ({len(result['missing_fields'])}): {', '.join(result['missing_fields'])}")

    print(f"\n  Run at: {result['run_at'][:19]}")
    print(bar)
    print(f"\n  Output -> .tmp/{result['ticker']}_buffett.json\n")


if __name__ == "__main__":
    main()
