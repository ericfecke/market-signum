#!/usr/bin/env python3
"""
score_and_weight.py — Ensemble Aggregator

Takes the five agent results (Dalio, Graham, Buffett, Lynch, Simons),
applies Dalio's regime_flag to shift the base weights, then computes
a confidence-weighted final score.

Weighting approach:
  effective_weight_i = regime_weight_i × confidence_i
  final_score = Σ(signal_score_i × effective_weight_i) / Σ(effective_weight_i)

This means a high-confidence agent has their full nominal weight, while
an agent with low confidence (sparse data, many missing fields) counts
proportionally less. Weights are then renormalized to sum to 1.

Deleveraging veto:
  When regime_flag == "deleveraging", any non-Dalio agent signaling
  "buy" is capped to "watch" before scoring. This prevents the final
  score from reaching the BUY threshold regardless of other agents.

Signal → numeric:
  buy   → 1.0
  watch → 0.5
  avoid → 0.0

Final score thresholds:
  0.70 – 1.00 → BUY
  0.50 – 0.69 → WATCH
  0.00 – 0.49 → AVOID

Output contract:
  {
    "ticker":              str,
    "regime_flag":         str,
    "final_score":         float,
    "recommendation":      "BUY" | "WATCH" | "AVOID",
    "applied_weights":     { agent: float (normalized) },
    "agent_contributions": {
        agent: { signal, vetoed_signal, confidence,
                 raw_score, effective_weight, contribution }
    },
    "consensus": {
        "buy":   [ agent, ... ],
        "watch": [ agent, ... ],
        "avoid": [ agent, ... ]
    },
    "deleveraging_veto_applied": bool,
    "run_at":              ISO timestamp
  }

Usage:
  python score_and_weight.py AAPL          # loads .tmp/AAPL_*.json
  python score_and_weight.py AAPL --no-cache
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT    = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Weight tables
# Raw values from spec. Totals may exceed 100 — normalized at runtime.
# ---------------------------------------------------------------------------

_RAW_WEIGHTS: dict[str, dict[str, float]] = {
    "neutral":     {"graham": 15, "buffett": 25, "dalio": 20, "lynch": 20, "simons": 20},
    "risk-on":     {"graham":  5, "buffett": 25, "dalio": 20, "lynch": 40, "simons": 30},
    "risk-off":    {"graham": 35, "buffett": 35, "dalio": 20, "lynch":  5, "simons": 15},
    "deleveraging":{"graham": 45, "buffett": 25, "dalio": 20, "lynch": 20, "simons": 20},
}

_SIGNAL_SCORE: dict[str, float] = {"buy": 1.0, "watch": 0.5, "avoid": 0.0}

_AGENTS = ("dalio", "graham", "buffett", "lynch", "simons")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total == 0:
        return {k: 1 / len(weights) for k in weights}
    return {k: v / total for k, v in weights.items()}


def _recommendation(score: float) -> str:
    if score >= 0.70: return "BUY"
    if score >= 0.50: return "WATCH"
    return "AVOID"


# ---------------------------------------------------------------------------
# Core scoring function
# ---------------------------------------------------------------------------

def score_and_weight(agent_results: dict[str, dict]) -> dict:
    """
    Aggregate five agent results into a final weighted score.

    Args:
        agent_results: Dict keyed by agent name ("dalio", "graham",
                       "buffett", "lynch", "simons"). Missing agents
                       are skipped — the remaining weights are renormalized.

    Returns: Full scoring result dict (see module docstring).
    """
    # Pull regime_flag from Dalio; default to neutral if Dalio missing
    dalio_result  = agent_results.get("dalio", {})
    regime_flag   = dalio_result.get("regime_flag", "neutral")
    if regime_flag not in _RAW_WEIGHTS:
        regime_flag = "neutral"

    raw_weights   = _RAW_WEIGHTS[regime_flag]

    # Deleveraging veto: active when Dalio signals avoid in deleveraging
    veto_active = (
        regime_flag == "deleveraging" and
        dalio_result.get("signal") == "avoid"
    )

    # ---- Per-agent scoring ----
    agent_contributions: dict[str, dict] = {}
    weighted_score_sum    = 0.0
    effective_weight_sum  = 0.0

    for agent in _AGENTS:
        result = agent_results.get(agent)
        if result is None or "error" in result:
            continue

        signal     = result.get("signal", "watch")
        confidence = float(result.get("confidence", 0.5))
        base_w     = raw_weights.get(agent, 0.0)

        # Apply veto: cap non-Dalio "buy" → "watch" in deleveraging
        vetoed_signal = signal
        veto_applied_here = False
        if veto_active and agent != "dalio" and signal == "buy":
            vetoed_signal = "watch"
            veto_applied_here = True

        raw_score        = _SIGNAL_SCORE.get(vetoed_signal, 0.5)
        effective_weight = base_w * confidence

        weighted_score_sum   += raw_score * effective_weight
        effective_weight_sum += effective_weight

        agent_contributions[agent] = {
            "signal":          signal,
            "vetoed_signal":   vetoed_signal,
            "veto_applied":    veto_applied_here,
            "confidence":      confidence,
            "raw_score":       raw_score,
            "base_weight":     round(base_w, 4),
            "effective_weight":round(effective_weight, 4),
            # contribution filled in after normalization below
        }

    # ---- Final score ----
    if effective_weight_sum == 0:
        final_score = 0.5  # no data → neutral
    else:
        final_score = weighted_score_sum / effective_weight_sum

    final_score = round(min(1.0, max(0.0, final_score)), 4)

    # ---- Normalize effective weights for display (sum to 1) ----
    if effective_weight_sum > 0:
        for agent, c in agent_contributions.items():
            c["effective_weight"] = round(c["effective_weight"] / effective_weight_sum, 4)
            c["contribution"]     = round(c["raw_score"] * c["effective_weight"], 4)

    # ---- Applied (base) weights normalized ----
    present_raw = {a: raw_weights[a] for a in agent_contributions}
    applied_weights = _normalize(present_raw)
    applied_weights = {k: round(v, 4) for k, v in applied_weights.items()}

    # ---- Consensus ----
    consensus: dict[str, list[str]] = {"buy": [], "watch": [], "avoid": []}
    for agent, c in agent_contributions.items():
        consensus[c["vetoed_signal"]].append(agent)

    ticker = dalio_result.get("ticker") or next(
        (v.get("ticker") for v in agent_results.values() if isinstance(v, dict)),
        "UNKNOWN"
    )

    result_out = {
        "ticker":                    ticker.upper(),
        "regime_flag":               regime_flag,
        "final_score":               final_score,
        "recommendation":            _recommendation(final_score),
        "applied_weights":           applied_weights,
        "agent_contributions":       agent_contributions,
        "consensus":                 consensus,
        "deleveraging_veto_applied": veto_active,
        "run_at":                    datetime.now().isoformat(),
    }

    out_path = TMP_DIR / f"{ticker.upper()}_score.json"
    with open(out_path, "w") as f:
        json.dump(result_out, f, indent=2)

    return result_out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_REC_ICON  = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}
_REG_LABEL = {
    "risk-on":      "📈 RISK-ON",
    "neutral":      "⚖️  NEUTRAL",
    "risk-off":     "🛡️  RISK-OFF",
    "deleveraging": "⚠️  DELEVERAGING",
}
_SIG_ICON  = {"buy": "🟢", "watch": "🟡", "avoid": "🔴"}


def _load_agent(ticker: str, agent: str) -> dict | None:
    path = TMP_DIR / f"{ticker.upper()}_{agent}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _score_bar(score: float, width: int = 40) -> str:
    """Visual score bar with AVOID/WATCH/BUY zones marked."""
    avoid_end  = int(0.50 * width)
    watch_end  = int(0.70 * width)
    needle_pos = int(score * width)

    bar = []
    for i in range(width):
        if i == needle_pos:
            bar.append("▼")
        elif i < avoid_end:
            bar.append("░")
        elif i < watch_end:
            bar.append("▒")
        else:
            bar.append("▓")
    return "".join(bar)


def main():
    if len(sys.argv) < 2:
        print("Usage: python score_and_weight.py <TICKER> [--no-cache]")
        sys.exit(1)

    ticker = sys.argv[1].upper()

    print(f"\nLoading agent results for {ticker}...")
    agent_results: dict[str, dict] = {}
    for agent in _AGENTS:
        data = _load_agent(ticker, agent)
        if data:
            agent_results[agent] = data
            print(f"  [OK] {agent}")
        else:
            print(f"  [FAIL] {agent} - .tmp/{ticker}_{agent}.json not found (skipped)")

    if not agent_results:
        print(f"\nNo agent results found for {ticker}. Run the agents first.")
        sys.exit(1)

    print(f"\nComputing weighted score...")
    result = score_and_weight(agent_results)

    # ---- Output ----
    bar_str = "=" * 62
    rec     = result["recommendation"]
    regime  = result["regime_flag"]
    score   = result["final_score"]

    print(f"\n{bar_str}")
    print(f"  MARKET SIGNUM VERDICT  -  {result['ticker']}")
    print(bar_str)
    print(f"  {_REC_ICON.get(rec, '')} {rec}   (score {score:.3f})")
    print(f"  Regime: {_REG_LABEL.get(regime, regime)}")
    if result["deleveraging_veto_applied"]:
        print(f"  [WARN]  Deleveraging veto applied - buy signals capped to watch")
    print()

    # Score bar
    sv = _score_bar(score)
    print(f"  Score: {sv}")
    print(f"         {'AVOID':^20}{'WATCH':^8}{'BUY':^12}")
    print()

    # Agent breakdown
    print(f"  {'Agent':<10} {'Signal':<8} {'Conf':>5}  {'W(base)':>7}  {'W(eff)':>6}  {'Contrib':>7}")
    print(f"  {'-'*9} {'-'*7} {'-'*5}  {'-'*7}  {'-'*6}  {'-'*7}")
    for agent in _AGENTS:
        c = result["agent_contributions"].get(agent)
        if c is None:
            print(f"  {agent:<10} {'-':<8} {'-':>5}  {'-':>7}  {'-':>6}  {'-':>7}  (missing)")
            continue
        sig   = c["vetoed_signal"]
        veto  = " [veto]" if c.get("veto_applied") else ""
        print(
            f"  {agent:<10} {_SIG_ICON.get(sig,'')} {sig:<6}{veto}"
            f"  {c['confidence']:>4.0%}  "
            f"{c['base_weight']:>6.1%}  "
            f"{c['effective_weight']:>5.1%}  "
            f"{c['contribution']:>6.3f}"
        )
    print()

    # Consensus
    buy_agents   = result["consensus"]["buy"]
    watch_agents = result["consensus"]["watch"]
    avoid_agents = result["consensus"]["avoid"]
    if buy_agents:   print(f"  Buy:   {', '.join(buy_agents)}")
    if watch_agents: print(f"  Watch: {', '.join(watch_agents)}")
    if avoid_agents: print(f"  Avoid: {', '.join(avoid_agents)}")

    print(f"\n  Run at: {result['run_at'][:19]}")
    print(bar_str)
    print(f"\n  Output -> .tmp/{result['ticker']}_score.json\n")


if __name__ == "__main__":
    main()
