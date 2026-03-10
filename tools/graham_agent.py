#!/usr/bin/env python3
"""
graham_agent.py — Benjamin Graham Valuation Analysis

Graham is the valuation gatekeeper. He rarely says buy. When he does,
it means the stock is trading at a genuine discount to intrinsic value
with a meaningful margin of safety. When he passes, the other agents
carry more weight.

Scoring dimensions (all in [-1, +1]):
  valuation      (40%) — P/E, Price-to-Book
  balance_sheet  (35%) — Debt/Equity, Current Ratio
  earnings_power (25%) — EPS, Profit Margin

Signal thresholds (strict — Graham is skeptical by default):
  score >= 0.30 → buy
  score >= 0.05 → watch
  score <  0.05 → avoid

Output contract:
  {
    "agent":            "graham",
    "ticker":           str,
    "signal":           "buy" | "watch" | "avoid",
    "confidence":       0.0–1.0,
    "reasoning":        str,
    "dimension_scores": { valuation, balance_sheet, earnings_power },
    "missing_fields":   [ str, ... ],
    "run_at":           ISO timestamp
  }

Usage:
  python graham_agent.py AAPL
  python graham_agent.py AAPL --no-cache
  ANTHROPIC_API_KEY=sk-ant-... python graham_agent.py AAPL
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
# Scoring
# ---------------------------------------------------------------------------

def _score_pe(pe) -> tuple[float, str]:
    if pe is None:            return  0.00, "P/E unavailable"
    if pe <= 0:               return -0.90, f"P/E negative ({pe}) — earnings deficit"
    if pe < 10:               return +0.90, f"P/E {pe} — deeply undervalued by Graham standards"
    if pe < 15:               return +0.50, f"P/E {pe} — within Graham's preferred range (<15)"
    if pe < 20:               return  0.00, f"P/E {pe} — fair value, no margin of safety"
    if pe < 25:               return -0.40, f"P/E {pe} — overvalued for Graham"
    if pe < 35:               return -0.70, f"P/E {pe} — well above Graham threshold"
    return                           -1.00, f"P/E {pe} — speculative valuation, Graham avoids"


def _score_pb(pb) -> tuple[float, str]:
    if pb is None:            return  0.00, "P/B unavailable"
    if pb <= 0:               return -0.50, f"P/B {pb} — negative book value"
    if pb < 1.0:              return +0.80, f"P/B {pb} — trading below book value (Graham's ideal)"
    if pb < 1.5:              return +0.50, f"P/B {pb} — within Graham's P/B threshold (<1.5)"
    if pb < 2.0:              return +0.10, f"P/B {pb} — acceptable but not a bargain"
    if pb < 3.0:              return -0.30, f"P/B {pb} — premium to book"
    return                           -0.80, f"P/B {pb} — far above book, no margin of safety"


def _score_de(de) -> tuple[float, str]:
    if de is None:            return  0.00, "D/E unavailable"
    if de < 0:                return -0.30, f"D/E {de} — negative equity (liabilities exceed assets)"
    if de < 0.20:             return +0.60, f"D/E {de} — very conservative balance sheet"
    if de < 0.50:             return +0.20, f"D/E {de} — below Graham's 0.5 threshold"
    if de < 1.00:             return -0.40, f"D/E {de} — exceeds Graham's threshold (flags at >0.5)"
    return                           -0.80, f"D/E {de} — high leverage, Graham avoids"


def _score_current_ratio(cr) -> tuple[float, str]:
    if cr is None:            return  0.00, "Current ratio unavailable"
    if cr > 3.0:              return +0.50, f"Current ratio {cr} — very strong liquidity"
    if cr > 2.0:              return +0.30, f"Current ratio {cr} — meets Graham's >2.0 standard"
    if cr > 1.5:              return  0.00, f"Current ratio {cr} — adequate but below Graham's ideal"
    if cr > 1.0:              return -0.30, f"Current ratio {cr} — below Graham's safety threshold"
    return                           -0.70, f"Current ratio {cr} — financial stress risk"


def _score_eps(eps) -> tuple[float, str]:
    if eps is None:           return -0.20, "EPS unavailable"
    if eps > 0:               return +0.30, f"EPS ${eps:.2f} — company is profitable"
    return                           -0.70, f"EPS ${eps:.2f} — company is losing money"


def _score_margin(margin) -> tuple[float, str]:
    if margin is None:        return  0.00, "Profit margin unavailable"
    if margin > 15:           return +0.40, f"Net margin {margin:.1f}% — healthy earnings quality"
    if margin > 5:            return +0.20, f"Net margin {margin:.1f}% — adequate profitability"
    if margin >= 0:           return  0.00, f"Net margin {margin:.1f}% — thin margins"
    return                           -0.60, f"Net margin {margin:.1f}% — unprofitable"


def _score_graham(data: dict) -> tuple[float, dict, dict, list]:
    """
    Score stock on Graham's value criteria.
    Returns: (composite [-1,+1], dim_scores dict, notes dict, missing list)
    """
    f = data.get("fundamentals", {})
    missing: list[str] = []

    def _track(key, score_fn, source=f):
        val = source.get(key)
        if val is None:
            missing.append(key)
        s, note = score_fn(val)
        return s, note

    # --- Valuation dimension ---
    pe_s,  pe_n  = _track("pe_ratio",       _score_pe)
    pb_s,  pb_n  = _track("price_to_book",  _score_pb)
    valuation = pe_s * 0.60 + pb_s * 0.40

    # --- Balance sheet dimension ---
    de_s,  de_n  = _track("debt_to_equity", _score_de)
    cr_s,  cr_n  = _track("current_ratio",  _score_current_ratio)
    balance_sheet = de_s * 0.50 + cr_s * 0.50

    # --- Earnings power dimension ---
    eps_s, eps_n = _track("eps_trailing",   _score_eps)
    mgn_s, mgn_n = _track("profit_margin",  _score_margin)
    earnings_power = eps_s * 0.50 + mgn_s * 0.50

    composite = (
        valuation      * 0.40 +
        balance_sheet  * 0.35 +
        earnings_power * 0.25
    )

    scores = {
        "valuation":      round(valuation, 3),
        "balance_sheet":  round(balance_sheet, 3),
        "earnings_power": round(earnings_power, 3),
    }
    notes = {
        "valuation":      [pe_n, pb_n],
        "balance_sheet":  [de_n, cr_n],
        "earnings_power": [eps_n, mgn_n],
    }

    return round(max(-1.0, min(1.0, composite)), 3), scores, notes, missing


# ---------------------------------------------------------------------------
# Signal + confidence
# ---------------------------------------------------------------------------

def _to_signal(score: float) -> str:
    # Graham's strict thresholds — he rarely says buy
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

    return f"""You are Benjamin Graham evaluating {ticker} for potential investment.

Key data:
  P/E ratio:       {_fmt("pe_ratio")}
  Price-to-Book:   {_fmt("price_to_book")}
  Debt/Equity:     {_fmt("debt_to_equity")}
  Current ratio:   {_fmt("current_ratio")}
  EPS (trailing):  {_fmt("eps_trailing")}
  Net margin:      {_fmt("profit_margin", "%")}

Your analysis identified:
{note_text}

Composite valuation score: {score:+.3f} (scale: -1 = avoid, +1 = strong buy)
Signal: {_to_signal(score).upper()}

Write 2–3 sentences as Graham explaining your verdict on {ticker}:
- Reference specific numbers (P/E, P/B, D/E, or current ratio) that most influenced your decision
- Be conservative and skeptical — Graham rarely endorses a stock
- Speak in Graham's voice: disciplined, value-focused, wary of speculation

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
            print(f"  [Graham] LLM call failed ({exc}), using fallback.", file=sys.stderr)

    return _fallback_reasoning(score, dim_scores, notes, data)


def _fallback_reasoning(score: float, dim_scores: dict,
                        notes: dict, data: dict) -> str:
    f = data.get("fundamentals", {})
    pe   = f.get("pe_ratio")
    pb   = f.get("price_to_book")
    de   = f.get("debt_to_equity")
    cr   = f.get("current_ratio")

    if score >= 0.30:
        verdict = "This stock meets Graham's criteria for a value purchase."
    elif score >= 0.05:
        verdict = "This stock shows some value characteristics but does not clear Graham's full margin-of-safety threshold."
    else:
        verdict = "This stock fails Graham's valuation screen. The price provides insufficient margin of safety."

    details = []
    if pe  is not None: details.append(f"P/E of {pe} {'is acceptable' if pe < 15 else 'is elevated for a value investor'}")
    if pb  is not None: details.append(f"P/B of {pb} {'offers book-value support' if pb < 1.5 else 'trades at a significant premium to book'}")
    if de  is not None: details.append(f"D/E of {de} {'is conservatively financed' if de < 0.5 else 'carries more leverage than Graham prefers'}")
    if cr  is not None: details.append(f"current ratio of {cr} {'satisfies' if cr >= 2 else 'falls below'} the 2.0 standard")

    detail_str = "; ".join(details[:3])
    return f"{verdict} {detail_str}.".strip()


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def run_graham_agent(ticker: str, stock_data: dict, cache: bool = True) -> dict:
    """
    Run Graham valuation analysis on pre-fetched stock data.

    Args:
        ticker:     Stock symbol (for labeling and reasoning context).
        stock_data: Output dict from fetch_stock_data(). Must not be None.
        cache:      Unused here (scoring is deterministic). Kept for API consistency.

    Returns: Standardized agent result dict (see module docstring).
    """
    ticker = ticker.upper().strip()

    if "error" in stock_data:
        return {
            "agent": "graham", "ticker": ticker,
            "signal": "avoid", "confidence": 0.25,
            "reasoning": f"Data fetch error — cannot evaluate: {stock_data['error']}",
            "dimension_scores": {}, "missing_fields": [], "run_at": datetime.now().isoformat(),
        }

    print(f"  [Graham] Scoring valuation fundamentals...")
    score, dim_scores, notes, missing = _score_graham(stock_data)

    signal     = _to_signal(score)
    confidence = _to_confidence(score, missing)

    print(f"  [Graham] Score: {score:+.3f} → {signal.upper()} ({confidence:.0%})")
    print(f"  [Graham] Generating reasoning...")
    reasoning  = _generate_reasoning(ticker, stock_data, score, dim_scores, notes)

    result = {
        "agent":            "graham",
        "ticker":           ticker,
        "signal":           signal,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "dimension_scores": dim_scores,
        "missing_fields":   missing,
        "run_at":           datetime.now().isoformat(),
    }

    out_path = TMP_DIR / f"{ticker}_graham.json"
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
        print("Usage: python graham_agent.py <TICKER> [--no-cache]")
        sys.exit(1)

    ticker = sys.argv[1]
    cache  = "--no-cache" not in sys.argv

    # Load or fetch stock data
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

    print(f"\nRunning Graham valuation agent for {ticker.upper()}...\n")
    result = run_graham_agent(ticker, stock_data, cache=cache)

    bar = "=" * 62
    print(bar)
    print(f"  GRAHAM VALUATION  —  {result['ticker']}")
    print(bar)
    print(f"  Signal:     {_SIGNAL_ICON.get(result['signal'], result['signal'])}")
    print(f"  Confidence: {result['confidence']:.0%}")
    print()
    print("  Reasoning:")
    print(_wrap(result["reasoning"]))
    print()
    print("  Dimension Scores  (avoid ◀─────▶ buy)")
    for dim, score in result["dimension_scores"].items():
        print(f"  {dim:<16} {score:+.3f}  {_mini_bar(score)}")
    print()

    f = stock_data.get("fundamentals", {})
    def _v(k, suffix=""):
        v = f.get(k)
        return f"{v}{suffix}" if v is not None else "N/A"

    print("  Key Metrics:")
    print(f"  P/E: {_v('pe_ratio')}  |  P/B: {_v('price_to_book')}  |  D/E: {_v('debt_to_equity')}")
    print(f"  Current Ratio: {_v('current_ratio')}  |  EPS: {_v('eps_trailing')}  |  Net Margin: {_v('profit_margin', '%')}")

    if result["missing_fields"]:
        print(f"\n  Missing ({len(result['missing_fields'])}): {', '.join(result['missing_fields'])}")

    print(f"\n  Run at: {result['run_at'][:19]}")
    print(bar)
    print(f"\n  Output → .tmp/{result['ticker']}_graham.json\n")


if __name__ == "__main__":
    main()
