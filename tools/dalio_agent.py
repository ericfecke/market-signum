#!/usr/bin/env python3
"""
dalio_agent.py — Ray Dalio Macro Regime Detection

Analyzes the broad macro environment across four dimensions:
  1. Rate trajectory   (yield curve, 10Y direction)
  2. Inflation         (gold, TIPS, dollar strength)
  3. Risk appetite     (VIX, credit spreads, S&P trend)
  4. Debt cycle        (long bond trend, systemic stress)

Returns a regime_flag that score_and_weight.py uses to re-scale
the weights of every other agent in the ensemble. Must run BEFORE
Graham, Buffett, Lynch, and Simons.

Output contract:
  {
    "agent":        "dalio",
    "ticker":       str,
    "signal":       "buy" | "watch" | "avoid",
    "confidence":   0.0–1.0,
    "reasoning":    str,
    "regime_flag":  "risk-on" | "neutral" | "risk-off" | "deleveraging",
    "macro_scores": { "rates": float, "inflation": float,
                      "risk_appetite": float, "debt_cycle": float },
    "macro_snapshot": { ... raw indicator values ... },
    "run_at":       ISO timestamp
  }

Regime → downstream weight adjustments (score_and_weight.py):
  risk-on:      Lynch +20%, Simons +10%, Graham -10%, Buffett neutral
  neutral:      base weights
  risk-off:     Graham +20%, Buffett +10%, Lynch -15%, Simons -5%
  deleveraging: Graham +30%, Dalio veto on buys

Usage:
  python dalio_agent.py AAPL
  python dalio_agent.py AAPL --no-cache
  ANTHROPIC_API_KEY=sk-ant-... python dalio_agent.py AAPL
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

# Model used for reasoning text. Update to the latest Claude version as needed.
CLAUDE_MODEL = "claude-sonnet-4-6"

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ---------------------------------------------------------------------------
# Macro tickers
# ---------------------------------------------------------------------------

# Each entry: (yfinance_symbol, human_label)
MACRO_TICKERS = {
    "yield_10y":   ("^TNX",  "10Y Treasury Yield"),
    "yield_short": ("^IRX",  "13W T-Bill (short rate)"),
    "vix":         ("^VIX",  "CBOE VIX"),
    "sp500":       ("^GSPC", "S&P 500"),
    "tlt":         ("TLT",   "Long-Term Treasury ETF"),
    "gld":         ("GLD",   "Gold ETF"),
    "uup":         ("UUP",   "USD Index ETF"),
    "hyg":         ("HYG",   "High-Yield Bond ETF"),
}


# ---------------------------------------------------------------------------
# Macro data fetching
# ---------------------------------------------------------------------------

def _fetch_macro_snapshot(cache: bool = True) -> dict:
    """
    Pull 6 months of daily closes for all macro tickers.

    Returns a flat dict keyed by label. Each value is either None
    (fetch failed) or a dict with: current, pct_1m, pct_3m, ma_50,
    pct_above_ma50.

    Cached to .tmp/_macro_snapshot.json for CACHE_TTL_SECONDS.
    """
    cache_path = TMP_DIR / "_macro_snapshot.json"
    if cache and cache_path.exists():
        age = datetime.now().timestamp() - cache_path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            with open(cache_path) as f:
                return json.load(f)

    snapshot = {}
    for label, (sym, _) in MACRO_TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period="6mo")
            if hist.empty:
                snapshot[label] = None
                continue

            closes = hist["Close"].dropna()
            cur = float(closes.iloc[-1])

            def _pct(n: int):
                return (
                    round((cur - float(closes.iloc[-n])) / float(closes.iloc[-n]) * 100, 2)
                    if len(closes) > n else None
                )

            ma50 = (
                round(float(closes.rolling(50).mean().iloc[-1]), 4)
                if len(closes) >= 50 else None
            )

            snapshot[label] = {
                "symbol":          sym,
                "current":         round(cur, 4),
                "pct_1m":          _pct(21),
                "pct_3m":          _pct(63),
                "ma_50":           ma50,
                "pct_above_ma50":  (
                    round((cur - ma50) / ma50 * 100, 2) if ma50 else None
                ),
            }
        except Exception as exc:
            snapshot[label] = None
            print(f"  [Dalio] Warning: could not fetch {sym} ({exc})", file=sys.stderr)

    snapshot["_fetched_at"] = datetime.now().isoformat()
    with open(cache_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    return snapshot


# ---------------------------------------------------------------------------
# Rule-based regime scoring
# ---------------------------------------------------------------------------

def _score_regime(macro: dict) -> tuple[str, float, dict, dict]:
    """
    Score the macro environment across four independent dimensions.

    Each dimension returns a value in [-1.0, +1.0]:
        +1.0 = strongly risk-on
        -1.0 = strongly risk-off

    Dimension scores are combined with fixed weights to produce a
    composite score, which maps to a regime bucket. Deleveraging is
    triggered by multi-dimension stress, not by composite score alone.

    Returns: (regime_flag, confidence, scores_dict, notes_dict)
    """

    def _val(label: str, key: str):
        d = macro.get(label)
        return d.get(key) if isinstance(d, dict) else None

    scores: dict[str, float] = {}
    notes:  dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # 1. RATE TRAJECTORY
    #    Falling yields / easing = risk-on. Rising / inverted = risk-off.
    # ------------------------------------------------------------------
    s = 0.0
    n: list[str] = []

    y10_cur   = _val("yield_10y",   "current")
    y10_1m    = _val("yield_10y",   "pct_1m")
    y10_3m    = _val("yield_10y",   "pct_3m")
    ysh_cur   = _val("yield_short", "current")

    if y10_1m is not None:
        if   y10_1m >  12: s -= 0.60; n.append(f"10Y yield surged +{y10_1m:.1f}% in 1m — sharp tightening")
        elif y10_1m >   5: s -= 0.30; n.append(f"10Y yield rising +{y10_1m:.1f}% in 1m — tightening")
        elif y10_1m < -10: s += 0.50; n.append(f"10Y yield collapsed {y10_1m:.1f}% in 1m — rapid easing")
        elif y10_1m <  -4: s += 0.25; n.append(f"10Y yield falling {y10_1m:.1f}% in 1m — easing")
        else:               n.append(f"10Y yield {y10_1m:+.1f}% in 1m — stable")

    if y10_3m is not None:
        if   y10_3m >  15: s -= 0.20; n.append(f"10Y +{y10_3m:.1f}% over 3m — persistent tightening")
        elif y10_3m < -10: s += 0.15; n.append(f"10Y {y10_3m:.1f}% over 3m — sustained easing")

    # Yield curve: 10Y minus short rate
    if y10_cur is not None and ysh_cur is not None:
        curve = round(y10_cur - ysh_cur, 3)
        if   curve < -0.75: s -= 0.50; n.append(f"Yield curve deeply inverted ({curve:+.2f}%) — recession warning")
        elif curve < -0.25: s -= 0.25; n.append(f"Yield curve inverted ({curve:+.2f}%)")
        elif curve <  0.50: n.append(f"Yield curve flat ({curve:+.2f}%)")
        elif curve >  1.50: s += 0.20; n.append(f"Yield curve steep ({curve:+.2f}%) — early/mid expansion")
        else:               n.append(f"Yield curve normal ({curve:+.2f}%)")

    scores["rates"] = max(-1.0, min(1.0, s))
    notes["rates"]  = n or ["Rate data unavailable — dimension neutral"]

    # ------------------------------------------------------------------
    # 2. INFLATION
    #    Rising gold / falling dollar = inflationary pressure = risk-off.
    #    Falling gold / rising dollar = deflationary / disinflationary.
    # ------------------------------------------------------------------
    s = 0.0
    n = []

    gld_1m = _val("gld", "pct_1m")
    gld_3m = _val("gld", "pct_3m")
    uup_1m = _val("uup", "pct_1m")
    uup_3m = _val("uup", "pct_3m")

    if gld_3m is not None:
        if   gld_3m >  12: s -= 0.45; n.append(f"Gold +{gld_3m:.1f}% in 3m — strong inflation / safe-haven demand")
        elif gld_3m >   5: s -= 0.20; n.append(f"Gold +{gld_3m:.1f}% in 3m — mild inflation signal")
        elif gld_3m <  -8: s += 0.25; n.append(f"Gold {gld_3m:.1f}% in 3m — inflation easing, risk appetite improving")
        elif gld_3m <  -3: s += 0.10; n.append(f"Gold {gld_3m:.1f}% in 3m — gold cooling")
        else:               n.append(f"Gold {gld_3m:+.1f}% in 3m — inflation signals neutral")

    if gld_1m is not None and abs(gld_1m) > 4:
        if gld_1m > 4: n.append(f"Gold accelerating +{gld_1m:.1f}% in 1m")
        else:          n.append(f"Gold dropping {gld_1m:.1f}% in 1m")

    if uup_3m is not None:
        if   uup_3m >   6: s += 0.30; n.append(f"USD +{uup_3m:.1f}% in 3m — dollar strength, disinflationary")
        elif uup_3m >   2: s += 0.10; n.append(f"USD +{uup_3m:.1f}% in 3m — mild dollar strength")
        elif uup_3m <  -6: s -= 0.30; n.append(f"USD {uup_3m:.1f}% in 3m — dollar weakness, inflationary pressure")
        elif uup_3m <  -2: s -= 0.10; n.append(f"USD {uup_3m:.1f}% in 3m — mild dollar softness")

    scores["inflation"] = max(-1.0, min(1.0, s))
    notes["inflation"]  = n or ["Inflation proxy data unavailable — dimension neutral"]

    # ------------------------------------------------------------------
    # 3. RISK APPETITE
    #    VIX, high-yield credit, and S&P vs 200MA are the best real-time
    #    reads on whether market participants are leaning in or pulling back.
    # ------------------------------------------------------------------
    s = 0.0
    n = []

    vix_cur = _val("vix", "current")
    hyg_1m  = _val("hyg", "pct_1m")
    hyg_3m  = _val("hyg", "pct_3m")
    sp_cur  = _val("sp500", "current")
    sp_ma50 = _val("sp500", "ma_50")
    sp_1m   = _val("sp500", "pct_1m")
    sp_3m   = _val("sp500", "pct_3m")

    if vix_cur is not None:
        if   vix_cur > 40: s -= 0.90; n.append(f"VIX {vix_cur:.1f} — extreme fear / systemic stress")
        elif vix_cur > 30: s -= 0.60; n.append(f"VIX {vix_cur:.1f} — high fear, risk-off conditions")
        elif vix_cur > 22: s -= 0.25; n.append(f"VIX {vix_cur:.1f} — elevated uncertainty")
        elif vix_cur > 17: n.append(f"VIX {vix_cur:.1f} — moderate, near historical average")
        elif vix_cur < 13: s += 0.40; n.append(f"VIX {vix_cur:.1f} — complacent conditions, risk-on")
        else:              s += 0.15; n.append(f"VIX {vix_cur:.1f} — calm, risk appetite healthy")

    if hyg_1m is not None:
        if   hyg_1m < -4: s -= 0.45; n.append(f"HYG {hyg_1m:.1f}% in 1m — credit spreads widening sharply, risk-off")
        elif hyg_1m < -2: s -= 0.20; n.append(f"HYG {hyg_1m:.1f}% in 1m — credit softening")
        elif hyg_1m >  2: s += 0.20; n.append(f"HYG +{hyg_1m:.1f}% in 1m — credit tight, risk appetite healthy")

    if sp_cur is not None and sp_ma50 is not None:
        pct_vs_ma = (sp_cur - sp_ma50) / sp_ma50 * 100
        if   pct_vs_ma < -12: s -= 0.40; n.append(f"S&P {pct_vs_ma:.1f}% below 50MA — bear market conditions")
        elif pct_vs_ma <  -5: s -= 0.20; n.append(f"S&P {pct_vs_ma:.1f}% below 50MA — downtrend")
        elif pct_vs_ma >   5: s += 0.25; n.append(f"S&P +{pct_vs_ma:.1f}% above 50MA — bull trend intact")
        elif pct_vs_ma >   2: s += 0.10; n.append(f"S&P +{pct_vs_ma:.1f}% above 50MA — healthy trend")
        else:                  n.append(f"S&P near 50MA ({pct_vs_ma:+.1f}%) — trend neutral")

    scores["risk_appetite"] = max(-1.0, min(1.0, s))
    notes["risk_appetite"]  = n or ["Risk appetite data unavailable — dimension neutral"]

    # ------------------------------------------------------------------
    # 4. DEBT CYCLE (proxied via long bond behavior)
    #    TLT rising = rates falling = early/mid cycle or flight to safety.
    #    TLT falling sharply = rate pressure = late cycle / tightening.
    #    We distinguish between "rates falling because growth is good"
    #    (risk-on) and "flight to bonds because panic" (risk-off). VIX
    #    context from dimension 3 resolves the ambiguity.
    # ------------------------------------------------------------------
    s = 0.0
    n = []

    tlt_1m = _val("tlt", "pct_1m")
    tlt_3m = _val("tlt", "pct_3m")
    vix_context = vix_cur or 20  # fallback to neutral

    if tlt_3m is not None:
        if   tlt_3m >  10: s += 0.30; n.append(f"TLT +{tlt_3m:.1f}% in 3m — bond rally (rate expectations falling)")
        elif tlt_3m >   4: s += 0.10; n.append(f"TLT +{tlt_3m:.1f}% in 3m — mild rate relief")
        elif tlt_3m < -10: s -= 0.45; n.append(f"TLT {tlt_3m:.1f}% in 3m — sustained bond selloff, rate pressure elevated")
        elif tlt_3m <  -4: s -= 0.20; n.append(f"TLT {tlt_3m:.1f}% in 3m — bond weakness, rate pressure building")
        else:               n.append(f"TLT {tlt_3m:+.1f}% in 3m — long bonds stable")

    if tlt_1m is not None and abs(tlt_1m) > 3:
        n.append(f"TLT {tlt_1m:+.1f}% in 1m — recent acceleration")

    # If TLT is rising but VIX is also spiking, it's a flight-to-safety,
    # not an easing cycle — temper the risk-on read.
    if tlt_3m and tlt_3m > 5 and vix_context > 28:
        s -= 0.15
        n.append("TLT rally coincides with high VIX — flight-to-safety, not easing cycle")

    scores["debt_cycle"] = max(-1.0, min(1.0, s))
    notes["debt_cycle"]  = n or ["Long bond data unavailable — dimension neutral"]

    # ------------------------------------------------------------------
    # COMPOSITE SCORE → REGIME
    # ------------------------------------------------------------------
    # Risk appetite gets the highest weight: most real-time signal.
    dim_weights = {
        "rates":         0.30,
        "inflation":     0.20,
        "risk_appetite": 0.35,
        "debt_cycle":    0.15,
    }
    composite = sum(scores[k] * dim_weights[k] for k in dim_weights)

    # Deleveraging override: triggered by VIX > 38 OR three dimensions
    # simultaneously in deeply negative territory (systemic, not just weak).
    deep_stress = sum(
        1 for k in ("rates", "risk_appetite", "debt_cycle")
        if scores[k] <= -0.45
    )
    is_deleveraging = (vix_cur is not None and vix_cur > 38) or deep_stress >= 3

    if is_deleveraging:
        regime     = "deleveraging"
        confidence = min(0.92, 0.65 + abs(composite) * 0.40)
    elif composite >= 0.18:
        regime     = "risk-on"
        confidence = min(0.90, 0.52 + composite * 0.70)
    elif composite <= -0.18:
        regime     = "risk-off"
        confidence = min(0.90, 0.52 + abs(composite) * 0.70)
    else:
        regime     = "neutral"
        confidence = max(0.38, 0.68 - abs(composite) * 0.60)

    return regime, round(confidence, 2), scores, notes


# ---------------------------------------------------------------------------
# Signal mapping
# ---------------------------------------------------------------------------

_REGIME_TO_SIGNAL = {
    "risk-on":      "buy",    # macro tailwinds broadly favor equities
    "neutral":      "watch",  # no strong macro directional bias
    "risk-off":     "watch",  # macro headwinds — let Graham/Buffett carry weight
    "deleveraging": "avoid",  # capital preservation mode
}


# ---------------------------------------------------------------------------
# LLM reasoning (optional — falls back gracefully if key absent)
# ---------------------------------------------------------------------------

def _build_prompt(ticker: str, macro: dict, regime: str, scores: dict, notes: dict) -> str:
    def _fmt(label: str) -> str:
        d = macro.get(label)
        if not isinstance(d, dict):
            return "unavailable"
        parts = [f"current={d['current']}"]
        if d.get("pct_1m") is not None:
            parts.append(f"1m={d['pct_1m']:+.1f}%")
        if d.get("pct_3m") is not None:
            parts.append(f"3m={d['pct_3m']:+.1f}%")
        return ", ".join(parts)

    dim_lines = "\n".join(
        f"  {dim} (score {scores.get(dim, 0):+.2f}): {'; '.join(notes.get(dim, []))}"
        for dim in ("rates", "inflation", "risk_appetite", "debt_cycle")
    )

    return f"""You are Ray Dalio explaining the macro regime for a stock analysis of {ticker}.

Live macro data:
  10Y Yield:  {_fmt("yield_10y")}
  Short Rate: {_fmt("yield_short")}
  VIX:        {_fmt("vix")}
  S&P 500:    {_fmt("sp500")}
  TLT:        {_fmt("tlt")}
  Gold:       {_fmt("gld")}
  USD (UUP):  {_fmt("uup")}
  HYG:        {_fmt("hyg")}

Dimension analysis:
{dim_lines}

Detected regime: {regime.upper()}

Write 2–3 sentences in Dalio's voice explaining:
1. What the economic machine is signaling right now
2. Why this specific regime was flagged, citing the most decisive indicator
3. How this macro backdrop affects the risk/reward for equity investors

Rules:
- Do not mention numeric score values
- Speak directly and confidently, as Dalio does in interviews
- Reference specific indicators from the data (e.g., "the yield curve", "credit spreads", "VIX")
- Maximum 85 words"""


def _generate_reasoning(
    ticker: str, macro: dict, regime: str, scores: dict, notes: dict
) -> str:
    """Call Claude for Dalio-voice reasoning. Falls back to rule-based text."""
    if _ANTHROPIC_AVAILABLE and _API_KEY:
        try:
            client = anthropic.Anthropic(api_key=_API_KEY)
            msg = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": _build_prompt(ticker, macro, regime, scores, notes)}],
            )
            return msg.content[0].text.strip()
        except Exception as exc:
            print(f"  [Dalio] LLM call failed ({exc}), using fallback reasoning.", file=sys.stderr)

    return _fallback_reasoning(regime, scores, notes)


def _fallback_reasoning(regime: str, scores: dict, notes: dict) -> str:
    """Rule-based reasoning when Anthropic SDK or key is unavailable."""
    intros = {
        "risk-on":      "The machine is in expansion mode — liquidity is flowing and growth assets are favored.",
        "neutral":      "The machine is balanced — no strong directional bias in the current environment.",
        "risk-off":     "The machine is contracting — capital is seeking safety and growth assets face headwinds.",
        "deleveraging": "The machine is deleveraging — systemic stress is elevated and capital preservation takes priority.",
    }
    # Pull the top note from each dimension for texture
    top_notes = [
        notes[dim][0]
        for dim in ("risk_appetite", "rates", "debt_cycle", "inflation")
        if notes.get(dim)
    ][:2]

    base = intros.get(regime, "Macro regime is ambiguous.")
    detail = " ".join(top_notes)
    return f"{base} {detail}".strip()


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def run_dalio_agent(ticker: str, stock_data: dict | None = None, cache: bool = True) -> dict:
    """
    Run the Dalio macro regime detection agent.

    Args:
        ticker:     Stock symbol being analyzed (provides context for reasoning).
        stock_data: Optional output from fetch_stock_data() — not used for scoring
                    but can enrich reasoning context in future versions.
        cache:      Use cached macro snapshot if < 6h old.

    Returns:
        Standardized agent result dict (see module docstring).
    """
    ticker = ticker.upper().strip()

    print(f"  [Dalio] Pulling macro snapshot...")
    macro = _fetch_macro_snapshot(cache=cache)

    print(f"  [Dalio] Scoring regime across 4 dimensions...")
    regime, confidence, scores, notes = _score_regime(macro)

    signal = _REGIME_TO_SIGNAL[regime]

    print(f"  [Dalio] Regime: {regime.upper()} | Signal: {signal.upper()} | Confidence: {confidence:.0%}")
    print(f"  [Dalio] Generating reasoning...")
    reasoning = _generate_reasoning(ticker, macro, regime, scores, notes)

    # Strip internal keys from snapshot before including in output
    clean_snapshot = {k: v for k, v in macro.items() if not k.startswith("_")}

    return {
        "agent":          "dalio",
        "ticker":         ticker,
        "signal":         signal,
        "confidence":     confidence,
        "reasoning":      reasoning,
        "regime_flag":    regime,
        "macro_scores":   {k: round(v, 3) for k, v in scores.items()},
        "macro_snapshot": clean_snapshot,
        "run_at":         datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

_REGIME_ICON = {
    "risk-on":      "📈 RISK-ON",
    "neutral":      "⚖️  NEUTRAL",
    "risk-off":     "🛡️  RISK-OFF",
    "deleveraging": "⚠️  DELEVERAGING",
}
_SIGNAL_ICON = {"buy": "🟢 BUY", "watch": "🟡 WATCH", "avoid": "🔴 AVOID"}


def _mini_bar(score: float, width: int = 10) -> str:
    """ASCII progress bar centered at 0. Positive = right (risk-on), negative = left."""
    half = width // 2
    filled = round(abs(score) * half)
    if score >= 0:
        return " " * half + "│" + "▓" * filled + "░" * (half - filled)
    else:
        return "░" * (half - filled) + "▓" * filled + "│" + " " * half


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


def main():
    if len(sys.argv) < 2:
        print("Usage: python dalio_agent.py <TICKER> [--no-cache]")
        sys.exit(1)

    ticker = sys.argv[1]
    cache  = "--no-cache" not in sys.argv

    print(f"\nRunning Dalio macro agent for {ticker.upper()}...\n")
    result = run_dalio_agent(ticker, cache=cache)

    bar = "=" * 62
    print(bar)
    print(f"  DALIO MACRO REGIME  -  {result['ticker']}")
    print(bar)
    print(f"  Regime:     {_REGIME_ICON.get(result['regime_flag'], result['regime_flag'])}")
    print(f"  Signal:     {_SIGNAL_ICON.get(result['signal'], result['signal'])}")
    print(f"  Confidence: {result['confidence']:.0%}")
    print()
    print("  Reasoning:")
    print(_wrap(result["reasoning"]))
    print()
    print("  Dimension Scores  (risk-off <-----> risk-on)")
    print("  " + "-" * 44)
    for dim, score in result["macro_scores"].items():
        bar_str = _mini_bar(score)
        print(f"  {dim:<16} {score:+.3f}  {bar_str}")
    print()
    print("  Key signals:")
    snap = result.get("macro_snapshot", {})
    for label, display in (
        ("vix",         "VIX"),
        ("yield_10y",   "10Y Yield"),
        ("yield_short", "Short Rate"),
        ("hyg",         "HYG (credit)"),
        ("gld",         "Gold"),
        ("uup",         "USD"),
    ):
        d = snap.get(label)
        if isinstance(d, dict):
            cur = d.get("current", "N/A")
            p1  = f"{d['pct_1m']:+.1f}%" if d.get("pct_1m") is not None else "—"
            p3  = f"{d['pct_3m']:+.1f}%" if d.get("pct_3m") is not None else "—"
            print(f"  {display:<16} {cur:>8}   1m: {p1:>7}   3m: {p3:>7}")
    print()
    print(f"  Run at: {result['run_at'][:19]}")
    print(bar)

    out_path = TMP_DIR / f"{result['ticker']}_dalio.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Output -> .tmp/{result['ticker']}_dalio.json\n")


if __name__ == "__main__":
    main()
