#!/usr/bin/env python3
"""
simons_agent.py — Jim Simons Quant Signals & Pattern Recognition

Simons has no opinion on the business. He only sees patterns in
price, volume, and momentum data. He acts as a pure technical
counterweight to the narrative-heavy agents. When Simons and Lynch
both say buy, momentum is confirmed. When they diverge, flag for
review.

Scoring dimensions (all in [-1, +1]):
  momentum         (30%) — 30d / 60d / 90d price momentum
  oscillators      (30%) — RSI(14), MACD crossover + histogram
  trend_structure  (25%) — MA cross, price vs MA50/MA200
  volume_bb        (15%) — volume ratio, Bollinger band position

Signal thresholds (symmetric — patterns don't have opinions):
  score >= 0.25  → buy
  score >= -0.25 → watch
  score <  -0.25 → avoid

Output contract:
  {
    "agent":            "simons",
    "ticker":           str,
    "signal":           "buy" | "watch" | "avoid",
    "confidence":       0.0–1.0,
    "reasoning":        str,
    "dimension_scores": { momentum, oscillators, trend_structure, volume_bb },
    "missing_fields":   [ str, ... ],
    "run_at":           ISO timestamp
  }

Usage:
  python simons_agent.py AAPL
  python simons_agent.py AAPL --no-cache
  ANTHROPIC_API_KEY=sk-ant-... python simons_agent.py AAPL
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

def _score_momentum_30d(m30) -> tuple[float, str]:
    if m30 is None: return  0.00, "30d momentum unavailable"
    if m30 > 15:    return +0.70, f"30d momentum +{m30:.1f}% — strong positive trend"
    if m30 > 8:     return +0.40, f"30d momentum +{m30:.1f}% — solid near-term uptrend"
    if m30 > 3:     return +0.20, f"30d momentum +{m30:.1f}% — mild positive drift"
    if m30 > 0:     return  0.00, f"30d momentum +{m30:.1f}% — flat to marginally positive"
    if m30 > -3:    return -0.10, f"30d momentum {m30:.1f}% — marginal weakness"
    if m30 > -8:    return -0.30, f"30d momentum {m30:.1f}% — negative short-term trend"
    return                 -0.70, f"30d momentum {m30:.1f}% — sharp near-term decline"


def _score_momentum_60d(m60) -> tuple[float, str]:
    if m60 is None: return  0.00, "60d momentum unavailable"
    if m60 > 20:    return +0.40, f"60d momentum +{m60:.1f}% — sustained intermediate uptrend"
    if m60 > 8:     return +0.20, f"60d momentum +{m60:.1f}% — positive intermediate trend"
    if m60 > 0:     return  0.00, f"60d momentum +{m60:.1f}% — flat over 60 days"
    if m60 > -10:   return -0.20, f"60d momentum {m60:.1f}% — intermediate downtrend"
    return                 -0.40, f"60d momentum {m60:.1f}% — sustained intermediate decline"


def _score_momentum_90d(m90) -> tuple[float, str]:
    if m90 is None: return  0.00, "90d momentum unavailable"
    if m90 > 25:    return +0.30, f"90d momentum +{m90:.1f}% — strong 3-month trend persistence"
    if m90 > 10:    return +0.20, f"90d momentum +{m90:.1f}% — positive 3-month trend"
    if m90 > 0:     return  0.00, f"90d momentum +{m90:.1f}% — flat over 90 days"
    if m90 > -10:   return -0.20, f"90d momentum {m90:.1f}% — 3-month downward drift"
    return                 -0.30, f"90d momentum {m90:.1f}% — sustained 3-month decline"


def _score_rsi(rsi) -> tuple[float, str]:
    """
    RSI(14) interpretation for a quant system.
    Simons treats 55–70 as the signal zone: momentum confirmed without
    extreme overbought risk. > 75 or < 25 flag mean-reversion risk.
    """
    if rsi is None:     return  0.00, "RSI unavailable"
    if rsi > 80:        return -0.20, f"RSI {rsi:.1f} — extreme overbought, mean-reversion risk elevated"
    if rsi > 75:        return +0.10, f"RSI {rsi:.1f} — overbought but momentum still intact"
    if rsi > 68:        return +0.30, f"RSI {rsi:.1f} — strong momentum, approaching overbought"
    if rsi > 55:        return +0.60, f"RSI {rsi:.1f} — bullish zone, momentum confirmed without excess"
    if rsi > 45:        return  0.00, f"RSI {rsi:.1f} — neutral zone"
    if rsi > 35:        return -0.30, f"RSI {rsi:.1f} — weakening momentum"
    if rsi > 25:        return -0.50, f"RSI {rsi:.1f} — oversold, downtrend extended"
    return                     -0.70, f"RSI {rsi:.1f} — extreme oversold, high volatility / trend break"


def _score_macd(macd_data) -> tuple[float, str]:
    """
    MACD crossover + histogram magnitude.
    Bullish crossover with growing histogram = accelerating momentum.
    """
    if not isinstance(macd_data, dict):
        return 0.00, "MACD data unavailable"

    crossover = macd_data.get("crossover")
    histogram = macd_data.get("histogram")
    macd_val  = macd_data.get("macd")
    sig_val   = macd_data.get("signal")

    if crossover is None:
        return 0.00, "MACD crossover unavailable"

    # Base score from crossover direction
    if crossover == "bullish":
        base = +0.50
        direction_note = "MACD bullish crossover"
    else:
        base = -0.50
        direction_note = "MACD bearish crossover"

    # Histogram gives conviction — larger divergence = stronger signal
    hist_modifier = 0.0
    hist_note = ""
    if histogram is not None:
        if abs(histogram) > 0.5:
            hist_modifier = 0.20 if crossover == "bullish" else -0.20
            hist_note = f" with strong histogram ({histogram:+.3f})"
        elif abs(histogram) > 0.1:
            hist_modifier = 0.10 if crossover == "bullish" else -0.10
            hist_note = f" (histogram {histogram:+.3f})"
        else:
            hist_note = " (histogram near zero — signal just formed)"

    score = base + hist_modifier
    note  = f"{direction_note}{hist_note}"
    return round(max(-1.0, min(1.0, score)), 3), note


def _score_ma_cross(ma_cross) -> tuple[float, str]:
    if ma_cross is None:    return  0.00, "MA cross unavailable"
    if ma_cross == "golden": return +0.50, "Golden cross (MA50 > MA200) — long-term uptrend confirmed"
    if ma_cross == "death":  return -0.50, "Death cross (MA50 < MA200) — long-term downtrend in force"
    return 0.00, "MA cross status unknown"


def _score_price_vs_ma50(pct) -> tuple[float, str]:
    if pct is None:  return  0.00, "Price vs MA50 unavailable"
    if pct > 8:      return +0.40, f"Price +{pct:.1f}% above MA50 — strong trend continuation"
    if pct > 3:      return +0.20, f"Price +{pct:.1f}% above MA50 — healthy uptrend"
    if pct > 0:      return +0.10, f"Price +{pct:.1f}% above MA50 — marginally above trend"
    if pct > -3:     return -0.10, f"Price {pct:.1f}% below MA50 — slightly below trend"
    if pct > -8:     return -0.30, f"Price {pct:.1f}% below MA50 — below trend"
    return                  -0.50, f"Price {pct:.1f}% below MA50 — extended below trend line"


def _score_price_vs_ma200(pct) -> tuple[float, str]:
    if pct is None:  return  0.00, "Price vs MA200 unavailable"
    if pct > 10:     return +0.30, f"Price +{pct:.1f}% above MA200 — long-term bull trend"
    if pct > 5:      return +0.20, f"Price +{pct:.1f}% above MA200 — above long-term average"
    if pct > 0:      return +0.10, f"Price +{pct:.1f}% above MA200 — marginally above long-term trend"
    if pct > -5:     return -0.10, f"Price {pct:.1f}% below MA200 — below long-term average"
    return                  -0.40, f"Price {pct:.1f}% below MA200 — extended below long-term trend"


def _score_volume_ratio(vr) -> tuple[float, str]:
    """Volume ratio = current session vol / 20-day average. > 1 = above-average interest."""
    if vr is None: return  0.00, "Volume ratio unavailable"
    if vr > 2.5:   return +0.30, f"Volume ratio {vr:.2f}x — very high volume, strong conviction"
    if vr > 1.5:   return +0.20, f"Volume ratio {vr:.2f}x — above-average volume confirming move"
    if vr > 0.8:   return  0.00, f"Volume ratio {vr:.2f}x — normal volume"
    return                -0.10, f"Volume ratio {vr:.2f}x — below-average volume, weak conviction"


def _score_bollinger_position(bb_data) -> tuple[float, str]:
    """
    Bollinger Band position: 0 = at lower band, 1 = at upper band, 0.5 = middle.
    Simons sees 0.6-0.85 as the "momentum zone" — trending up without extreme extension.
    """
    if not isinstance(bb_data, dict):
        return 0.00, "Bollinger Band data unavailable"

    pos = bb_data.get("position")
    if pos is None: return 0.00, "Bollinger position unavailable"

    if pos > 0.90:   return -0.10, f"BB position {pos:.2f} — at upper band extremity, pullback risk"
    if pos > 0.65:   return +0.30, f"BB position {pos:.2f} — in upper momentum zone"
    if pos > 0.50:   return +0.10, f"BB position {pos:.2f} — in upper half, mild bullish"
    if pos > 0.35:   return  0.00, f"BB position {pos:.2f} — mid-band, no directional signal"
    if pos > 0.10:   return -0.20, f"BB position {pos:.2f} — in lower half, bearish tendency"
    return                  -0.30, f"BB position {pos:.2f} — near lower band"


def _score_simons(data: dict) -> tuple[float, dict, dict, list]:
    """
    Score stock on Simons' quant/technical criteria.
    Returns: (composite [-1,+1], dim_scores, notes, missing_fields)
    """
    t  = data.get("technicals", {})
    missing: list[str] = []

    def _get(key, src=t):
        v = src.get(key)
        if v is None:
            missing.append(f"technicals.{key}")
        return v

    mom       = t.get("momentum", {}) or {}
    macd_data = t.get("macd")
    bb_data   = t.get("bollinger_bands")

    m30    = mom.get("30d")
    m60    = mom.get("60d")
    m90    = mom.get("90d")
    rsi    = _get("rsi_14")
    ma_x   = t.get("ma_cross")
    p_ma50 = t.get("price_vs_ma50")
    p_ma2  = t.get("price_vs_ma200")
    vr     = t.get("volume_ratio_20d")

    if m30  is None: missing.append("technicals.momentum.30d")
    if m60  is None: missing.append("technicals.momentum.60d")
    if m90  is None: missing.append("technicals.momentum.90d")
    if ma_x is None: missing.append("technicals.ma_cross")

    m30_s, m30_n = _score_momentum_30d(m30)
    m60_s, m60_n = _score_momentum_60d(m60)
    m90_s, m90_n = _score_momentum_90d(m90)
    rsi_s, rsi_n = _score_rsi(rsi)
    mcd_s, mcd_n = _score_macd(macd_data)
    mac_s, mac_n = _score_ma_cross(ma_x)
    p50_s, p50_n = _score_price_vs_ma50(p_ma50)
    p2_s,  p2_n  = _score_price_vs_ma200(p_ma2)
    vol_s, vol_n = _score_volume_ratio(vr)
    bb_s,  bb_n  = _score_bollinger_position(bb_data)

    momentum        = m30_s * 0.50 + m60_s * 0.30 + m90_s * 0.20
    oscillators     = rsi_s * 0.45 + mcd_s * 0.55
    trend_structure = mac_s * 0.40 + p50_s * 0.35 + p2_s  * 0.25
    volume_bb       = vol_s * 0.50 + bb_s  * 0.50

    composite = (
        momentum        * 0.30 +
        oscillators     * 0.30 +
        trend_structure * 0.25 +
        volume_bb       * 0.15
    )

    scores = {
        "momentum":        round(momentum,        3),
        "oscillators":     round(oscillators,     3),
        "trend_structure": round(trend_structure, 3),
        "volume_bb":       round(volume_bb,       3),
    }
    notes = {
        "momentum":        [m30_n, m60_n, m90_n],
        "oscillators":     [rsi_n, mcd_n],
        "trend_structure": [mac_n, p50_n, p2_n],
        "volume_bb":       [vol_n, bb_n],
    }

    return round(max(-1.0, min(1.0, composite)), 3), scores, notes, missing


# ---------------------------------------------------------------------------
# Signal + confidence
# ---------------------------------------------------------------------------

def _to_signal(score: float) -> str:
    if score >= 0.25:  return "buy"
    if score >= -0.25: return "watch"
    return "avoid"


def _to_confidence(score: float, missing: list) -> float:
    # Technical data is computed from price history — fewer missing fields expected.
    base    = 0.50 + abs(score) * 0.42
    penalty = min(0.20, len(missing) * 0.03)
    return round(max(0.25, min(0.92, base - penalty)), 2)


# ---------------------------------------------------------------------------
# LLM reasoning
# ---------------------------------------------------------------------------

def _build_prompt(ticker: str, data: dict, score: float,
                  dim_scores: dict, notes: dict) -> str:
    t   = data.get("technicals", {})
    mom = t.get("momentum", {}) or {}
    bb  = t.get("bollinger_bands", {}) or {}
    macd = t.get("macd", {}) or {}

    def _v(d, k, suffix=""):
        v = d.get(k)
        return f"{v}{suffix}" if v is not None else "N/A"

    all_notes = [n for ns in notes.values() for n in ns]
    note_text  = "\n".join(f"  - {n}" for n in all_notes[:8])

    return f"""You are Jim Simons analyzing {ticker} using quantitative pattern recognition.

Key signals:
  RSI(14):          {_v(t, "rsi_14")}
  MACD crossover:   {_v(macd, "crossover")}   histogram: {_v(macd, "histogram")}
  MA cross:         {_v(t, "ma_cross")}
  Price vs MA50:    {_v(t, "price_vs_ma50", "%")}
  Price vs MA200:   {_v(t, "price_vs_ma200", "%")}
  Bollinger pos:    {_v(bb, "position")} (0=lower, 1=upper)
  Volume ratio 20d: {_v(t, "volume_ratio_20d", "x")}
  Momentum 30d:     {mom.get("30d", "N/A")}%
  Momentum 60d:     {mom.get("60d", "N/A")}%
  Momentum 90d:     {mom.get("90d", "N/A")}%

Pattern analysis identified:
{note_text}

Composite quant score: {score:+.3f} (scale: -1 = avoid, +1 = strong buy)
Signal: {_to_signal(score).upper()}

Write 2–3 sentences as Simons explaining the statistical signals for {ticker}:
- Speak in purely quantitative terms — no opinions on the business
- Reference specific indicators (RSI, MACD, momentum windows, MA cross)
- Note whether signals are confirming each other or diverging
- Cold, precise, mathematical tone — Simons doesn't speculate

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
            print(f"  [Simons] LLM call failed ({exc}), using fallback.", file=sys.stderr)

    return _fallback_reasoning(score, dim_scores, notes, data)


def _fallback_reasoning(score: float, dim_scores: dict,
                        notes: dict, data: dict) -> str:
    t    = data.get("technicals", {})
    rsi  = t.get("rsi_14")
    macd = t.get("macd", {}) or {}
    ma_x = t.get("ma_cross")
    mom  = t.get("momentum", {}) or {}

    if score >= 0.25:
        verdict = "The quantitative pattern stack is aligned. Multiple independent signals confirm upside momentum."
    elif score >= -0.25:
        verdict = "Signals are mixed — insufficient statistical consensus for a directional trade."
    else:
        verdict = "The pattern data is negative across multiple windows. Statistical evidence favors downside."

    details = []
    if rsi is not None:
        details.append(f"RSI at {rsi:.1f} {'is in the bullish momentum zone' if 55 < rsi < 70 else 'signals caution'}")
    if macd.get("crossover"):
        details.append(f"MACD shows a {macd['crossover']} crossover")
    if ma_x:
        details.append(f"{'golden' if ma_x == 'golden' else 'death'} cross on the 50/200MA")
    if mom.get("30d") is not None:
        details.append(f"30d momentum at {mom['30d']:+.1f}%")

    detail_str = "; ".join(details[:3])
    return f"{verdict} {detail_str}.".strip()


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def run_simons_agent(ticker: str, stock_data: dict, cache: bool = True) -> dict:
    """
    Run Simons quant signal analysis on pre-fetched stock data.

    Args:
        ticker:     Stock symbol.
        stock_data: Output dict from fetch_stock_data(). Must not be None.
        cache:      Unused (scoring is deterministic). Kept for API consistency.

    Returns: Standardized agent result dict.
    """
    ticker = ticker.upper().strip()

    if "error" in stock_data:
        return {
            "agent": "simons", "ticker": ticker,
            "signal": "avoid", "confidence": 0.25,
            "reasoning": f"Data fetch error — cannot evaluate: {stock_data['error']}",
            "dimension_scores": {}, "missing_fields": [], "run_at": datetime.now().isoformat(),
        }

    print(f"  [Simons] Scoring quant signals and patterns...")
    score, dim_scores, notes, missing = _score_simons(stock_data)

    signal     = _to_signal(score)
    confidence = _to_confidence(score, missing)

    print(f"  [Simons] Score: {score:+.3f} → {signal.upper()} ({confidence:.0%})")
    print(f"  [Simons] Generating reasoning...")
    reasoning  = _generate_reasoning(ticker, stock_data, score, dim_scores, notes)

    result = {
        "agent":            "simons",
        "ticker":           ticker,
        "signal":           signal,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "dimension_scores": dim_scores,
        "missing_fields":   missing,
        "run_at":           datetime.now().isoformat(),
    }

    out_path = TMP_DIR / f"{ticker}_simons.json"
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
        print("Usage: python simons_agent.py <TICKER> [--no-cache]")
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

    print(f"\nRunning Simons quant agent for {ticker.upper()}...\n")
    result = run_simons_agent(ticker, stock_data, cache=cache)

    bar = "=" * 62
    print(bar)
    print(f"  SIMONS QUANT  —  {result['ticker']}")
    print(bar)
    print(f"  Signal:     {_SIGNAL_ICON.get(result['signal'], result['signal'])}")
    print(f"  Confidence: {result['confidence']:.0%}")
    print()
    print("  Reasoning:")
    print(_wrap(result["reasoning"]))
    print()
    print("  Dimension Scores  (avoid ◀─────▶ buy)")
    for dim, score in result["dimension_scores"].items():
        print(f"  {dim:<18} {score:+.3f}  {_mini_bar(score)}")
    print()

    t    = stock_data.get("technicals", {})
    mom  = t.get("momentum", {}) or {}
    bb   = t.get("bollinger_bands", {}) or {}
    macd = t.get("macd", {}) or {}

    def _v(d, k, suffix=""):
        v = d.get(k)
        return f"{v}{suffix}" if v is not None else "N/A"

    print("  Key Signals:")
    print(f"  RSI(14): {_v(t, 'rsi_14')}  |  MACD: {_v(macd, 'crossover')}  |  MA Cross: {_v(t, 'ma_cross')}")
    print(f"  vs MA50: {_v(t, 'price_vs_ma50', '%')}  |  vs MA200: {_v(t, 'price_vs_ma200', '%')}  |  BB pos: {_v(bb, 'position')}")
    print(f"  Momentum  30d: {mom.get('30d', 'N/A')}%   60d: {mom.get('60d', 'N/A')}%   90d: {mom.get('90d', 'N/A')}%")
    print(f"  Volume ratio: {_v(t, 'volume_ratio_20d', 'x')}")

    if result["missing_fields"]:
        print(f"\n  Missing ({len(result['missing_fields'])}): {', '.join(result['missing_fields'])}")

    print(f"\n  Run at: {result['run_at'][:19]}")
    print(bar)
    print(f"\n  Output → .tmp/{result['ticker']}_simons.json\n")


if __name__ == "__main__":
    main()
