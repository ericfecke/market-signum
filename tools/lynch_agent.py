#!/usr/bin/env python3
"""
lynch_agent.py — Peter Lynch Growth & Momentum Screening

Lynch finds growth before the institutions do. He is the most
optimistic agent in the ensemble — most likely to flag a rising
stock early. His key insight: a stock growing earnings at 20%
trading at a PEG below 1.0 is almost always a buy.

Scoring dimensions (all in [-1, +1]):
  growth     (35%) — earnings growth, revenue growth
  peg_value  (30%) — PEG ratio (price/earnings to growth)
  discovery  (20%) — institutional & insider ownership
  momentum   (15%) — 30d price momentum confirming the story

Signal thresholds (optimistic — Lynch leans in early):
  score >= 0.20 → buy
  score >= -0.10 → watch
  score <  -0.10 → avoid

Output contract:
  {
    "agent":            "lynch",
    "ticker":           str,
    "signal":           "buy" | "watch" | "avoid",
    "confidence":       0.0–1.0,
    "reasoning":        str,
    "dimension_scores": { growth, peg_value, discovery, momentum },
    "missing_fields":   [ str, ... ],
    "run_at":           ISO timestamp
  }

Usage:
  python lynch_agent.py AAPL
  python lynch_agent.py AAPL --no-cache
  ANTHROPIC_API_KEY=sk-ant-... python lynch_agent.py AAPL
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

def _score_earnings_growth(eg) -> tuple[float, str]:
    """Lynch's ideal: 15–30% annual earnings growth. Above 30% = bonus."""
    if eg is None:  return  0.00, "Earnings growth unavailable"
    if eg > 30:     return +0.80, f"Earnings growth {eg:.1f}% — exceptional, Lynch's preferred territory"
    if eg > 20:     return +0.60, f"Earnings growth {eg:.1f}% — strong growth, Lynch would notice"
    if eg > 15:     return +0.40, f"Earnings growth {eg:.1f}% — in Lynch's 15-30% sweet spot"
    if eg > 10:     return +0.10, f"Earnings growth {eg:.1f}% — solid but below Lynch's ideal"
    if eg > 5:      return -0.10, f"Earnings growth {eg:.1f}% — modest, limited upside"
    if eg > 0:      return -0.30, f"Earnings growth {eg:.1f}% — near-flat growth"
    return                 -0.70, f"Earnings growth {eg:.1f}% — declining earnings"


def _score_revenue_growth(rg) -> tuple[float, str]:
    if rg is None:  return  0.00, "Revenue growth unavailable"
    if rg > 20:     return +0.50, f"Revenue growth {rg:.1f}% — expanding market share"
    if rg > 10:     return +0.30, f"Revenue growth {rg:.1f}% — healthy top-line momentum"
    if rg > 5:      return +0.10, f"Revenue growth {rg:.1f}% — modest revenue expansion"
    if rg >= 0:     return -0.10, f"Revenue growth {rg:.1f}% — flat revenues"
    return                 -0.50, f"Revenue growth {rg:.1f}% — shrinking revenues"


def _score_peg(peg) -> tuple[float, str]:
    """
    PEG ratio = P/E ÷ earnings growth rate.
    Lynch's rule: PEG < 1.0 is a buy, PEG > 2.0 is overvalued.
    """
    if peg is None or peg <= 0:
        return 0.00, "PEG ratio unavailable or non-calculable"
    if peg < 0.5:   return +0.90, f"PEG {peg:.2f} — exceptional value for growth (Lynch's ideal)"
    if peg < 1.0:   return +0.50, f"PEG {peg:.2f} — below 1.0, classic Lynch buy signal"
    if peg < 1.5:   return +0.10, f"PEG {peg:.2f} — reasonable but growth premium emerging"
    if peg < 2.0:   return -0.30, f"PEG {peg:.2f} — growth priced in, limited upside"
    return                 -0.60, f"PEG {peg:.2f} — overvalued relative to growth rate"


def _score_institutional_ownership(inst) -> tuple[float, str]:
    """
    Lynch prefers stocks under-owned by institutions.
    Low institutional ownership = early-stage discovery opportunity.
    """
    if inst is None:  return  0.00, "Institutional ownership unavailable"
    if inst < 15:     return +0.50, f"Institutional ownership {inst:.1f}% — undiscovered, significant upside if story plays out"
    if inst < 30:     return +0.30, f"Institutional ownership {inst:.1f}% — early institutional interest"
    if inst < 50:     return  0.00, f"Institutional ownership {inst:.1f}% — moderately followed"
    if inst < 75:     return -0.10, f"Institutional ownership {inst:.1f}% — widely owned, limited discovery premium"
    return                   -0.30, f"Institutional ownership {inst:.1f}% — crowded, over-owned"


def _score_insider_ownership(insider) -> tuple[float, str]:
    """High insider ownership signals management alignment with shareholders."""
    if insider is None: return  0.00, "Insider ownership unavailable"
    if insider > 25:    return +0.50, f"Insider ownership {insider:.1f}% — management has strong skin in the game"
    if insider > 10:    return +0.30, f"Insider ownership {insider:.1f}% — meaningful insider alignment"
    if insider > 5:     return +0.10, f"Insider ownership {insider:.1f}% — some insider interest"
    return                     0.00, f"Insider ownership {insider:.1f}% — low insider commitment"


def _score_price_momentum_30d(mom30) -> tuple[float, str]:
    """
    Lynch uses price momentum as confirmation: a rising stock with
    strong fundamentals confirms the story is playing out.
    """
    if mom30 is None:   return  0.00, "30d momentum unavailable"
    if mom30 > 15:      return +0.50, f"30d momentum +{mom30:.1f}% — price action strongly confirming the thesis"
    if mom30 > 5:       return +0.30, f"30d momentum +{mom30:.1f}% — price moving in the right direction"
    if mom30 > 0:       return +0.10, f"30d momentum +{mom30:.1f}% — modest positive confirmation"
    if mom30 > -5:      return -0.10, f"30d momentum {mom30:.1f}% — mild price weakness"
    if mom30 > -15:     return -0.30, f"30d momentum {mom30:.1f}% — price trend working against the thesis"
    return                     -0.50, f"30d momentum {mom30:.1f}% — significant price deterioration"


def _score_lynch(data: dict) -> tuple[float, dict, dict, list]:
    """
    Score stock on Lynch's growth + discovery criteria.
    Returns: (composite [-1,+1], dim_scores, notes, missing_fields)
    """
    f  = data.get("fundamentals", {})
    t  = data.get("technicals",   {})
    missing: list[str] = []

    def _get_f(key):
        v = f.get(key)
        if v is None:
            missing.append(key)
        return v

    def _get_t(key):
        v = t.get(key)
        if v is None:
            missing.append(f"technicals.{key}")
        return v

    eg     = _get_f("earnings_growth")
    rg     = _get_f("revenue_growth")
    peg    = _get_f("peg_ratio")
    inst   = _get_f("institutional_ownership")
    insider= _get_f("insider_ownership")
    mom    = t.get("momentum", {})
    mom30  = mom.get("30d") if isinstance(mom, dict) else None
    if mom30 is None:
        missing.append("technicals.momentum.30d")

    eg_s,   eg_n   = _score_earnings_growth(eg)
    rg_s,   rg_n   = _score_revenue_growth(rg)
    peg_s,  peg_n  = _score_peg(peg)
    inst_s, inst_n = _score_institutional_ownership(inst)
    ins_s,  ins_n  = _score_insider_ownership(insider)
    mom_s,  mom_n  = _score_price_momentum_30d(mom30)

    growth    = eg_s * 0.60 + rg_s * 0.40
    peg_value = peg_s
    discovery = inst_s * 0.55 + ins_s * 0.45
    momentum  = mom_s

    composite = (
        growth    * 0.35 +
        peg_value * 0.30 +
        discovery * 0.20 +
        momentum  * 0.15
    )

    scores = {
        "growth":    round(growth,    3),
        "peg_value": round(peg_value, 3),
        "discovery": round(discovery, 3),
        "momentum":  round(momentum,  3),
    }
    notes = {
        "growth":    [eg_n, rg_n],
        "peg_value": [peg_n],
        "discovery": [inst_n, ins_n],
        "momentum":  [mom_n],
    }

    return round(max(-1.0, min(1.0, composite)), 3), scores, notes, missing


# ---------------------------------------------------------------------------
# Signal + confidence
# ---------------------------------------------------------------------------

def _to_signal(score: float) -> str:
    if score >= 0.20:  return "buy"   # Lynch leans in early
    if score >= -0.10: return "watch"
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
    f   = data.get("fundamentals", {})
    t   = data.get("technicals",   {})
    mom = t.get("momentum", {})

    def _fmt(key, src=f, suffix=""):
        v = src.get(key)
        return f"{v}{suffix}" if v is not None else "N/A"

    all_notes = [n for ns in notes.values() for n in ns]
    note_text  = "\n".join(f"  - {n}" for n in all_notes)

    return f"""You are Peter Lynch evaluating {ticker} as a potential growth investment.

Key data:
  Earnings growth:         {_fmt("earnings_growth", suffix="%")}
  Revenue growth:          {_fmt("revenue_growth", suffix="%")}
  PEG ratio:               {_fmt("peg_ratio")}
  Institutional ownership: {_fmt("institutional_ownership", suffix="%")}
  Insider ownership:       {_fmt("insider_ownership", suffix="%")}
  30-day price momentum:   {mom.get("30d", "N/A")}%

Your analysis identified:
{note_text}

Composite growth score: {score:+.3f} (scale: -1 = avoid, +1 = strong buy)
Signal: {_to_signal(score).upper()}

Write 2–3 sentences as Lynch explaining your verdict on {ticker}:
- Speak enthusiastically if growth is strong, cautiously if it isn't
- Emphasize PEG ratio, earnings growth, or institutional ownership as Lynch would
- Reference whether the stock is "undiscovered" or already well-known
- Speak in Lynch's plain-spoken, direct voice — no financial jargon

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
            print(f"  [Lynch] LLM call failed ({exc}), using fallback.", file=sys.stderr)

    return _fallback_reasoning(score, dim_scores, notes, data)


def _fallback_reasoning(score: float, dim_scores: dict,
                        notes: dict, data: dict) -> str:
    f      = data.get("fundamentals", {})
    eg     = f.get("earnings_growth")
    peg    = f.get("peg_ratio")
    inst   = f.get("institutional_ownership")

    if score >= 0.20:
        verdict = "This looks like the kind of growth story I'd want to own before Wall Street catches on."
    elif score >= -0.10:
        verdict = "Some interesting growth characteristics here but not enough to pull the trigger yet."
    else:
        verdict = "The growth profile doesn't meet my criteria — I'd pass on this one."

    details = []
    if eg   is not None: details.append(f"earnings growing at {eg:.1f}% {'fits my 15-30% profile' if 15 <= eg <= 30 else 'is outside my sweet spot'}")
    if peg  is not None and peg > 0: details.append(f"PEG of {peg:.2f} {'is a gift' if peg < 1 else 'tells me the growth is already priced in'}")
    if inst is not None: details.append(f"{'only' if inst < 30 else ''} {inst:.0f}% institutional ownership {'means I might be early' if inst < 30 else 'means the street already knows about this one'}")

    detail_str = "; ".join(details[:2])
    return f"{verdict} {detail_str}.".strip()


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def run_lynch_agent(ticker: str, stock_data: dict, cache: bool = True) -> dict:
    """
    Run Lynch growth screening on pre-fetched stock data.

    Args:
        ticker:     Stock symbol.
        stock_data: Output dict from fetch_stock_data(). Must not be None.
        cache:      Unused (scoring is deterministic). Kept for API consistency.

    Returns: Standardized agent result dict.
    """
    ticker = ticker.upper().strip()

    if "error" in stock_data:
        return {
            "agent": "lynch", "ticker": ticker,
            "signal": "avoid", "confidence": 0.25,
            "reasoning": f"Data fetch error — cannot evaluate: {stock_data['error']}",
            "dimension_scores": {}, "missing_fields": [], "run_at": datetime.now().isoformat(),
        }

    print(f"  [Lynch] Scoring growth and discovery metrics...")
    score, dim_scores, notes, missing = _score_lynch(stock_data)

    signal     = _to_signal(score)
    confidence = _to_confidence(score, missing)

    print(f"  [Lynch] Score: {score:+.3f} → {signal.upper()} ({confidence:.0%})")
    print(f"  [Lynch] Generating reasoning...")
    reasoning  = _generate_reasoning(ticker, stock_data, score, dim_scores, notes)

    result = {
        "agent":            "lynch",
        "ticker":           ticker,
        "signal":           signal,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "dimension_scores": dim_scores,
        "missing_fields":   missing,
        "run_at":           datetime.now().isoformat(),
    }

    out_path = TMP_DIR / f"{ticker}_lynch.json"
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
        print("Usage: python lynch_agent.py <TICKER> [--no-cache]")
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

    print(f"\nRunning Lynch growth agent for {ticker.upper()}...\n")
    result = run_lynch_agent(ticker, stock_data, cache=cache)

    bar = "=" * 62
    print(bar)
    print(f"  LYNCH GROWTH  —  {result['ticker']}")
    print(bar)
    print(f"  Signal:     {_SIGNAL_ICON.get(result['signal'], result['signal'])}")
    print(f"  Confidence: {result['confidence']:.0%}")
    print()
    print("  Reasoning:")
    print(_wrap(result["reasoning"]))
    print()
    print("  Dimension Scores  (avoid ◀─────▶ buy)")
    for dim, score in result["dimension_scores"].items():
        print(f"  {dim:<12} {score:+.3f}  {_mini_bar(score)}")
    print()

    f   = stock_data.get("fundamentals", {})
    t   = stock_data.get("technicals",   {})
    mom = t.get("momentum", {})

    def _v(k, src=f, suffix=""):
        v = src.get(k)
        return f"{v}{suffix}" if v is not None else "N/A"

    print("  Key Metrics:")
    print(f"  EPS Growth: {_v('earnings_growth', suffix='%')}  |  Rev Growth: {_v('revenue_growth', suffix='%')}  |  PEG: {_v('peg_ratio')}")
    print(f"  Institutional: {_v('institutional_ownership', suffix='%')}  |  Insider: {_v('insider_ownership', suffix='%')}  |  30d Mom: {mom.get('30d', 'N/A')}%")

    if result["missing_fields"]:
        print(f"\n  Missing ({len(result['missing_fields'])}): {', '.join(result['missing_fields'])}")

    print(f"\n  Run at: {result['run_at'][:19]}")
    print(bar)
    print(f"\n  Output → .tmp/{result['ticker']}_lynch.json\n")


if __name__ == "__main__":
    main()
