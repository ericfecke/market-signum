#!/usr/bin/env python3
"""
batch_runner.py — Orchestrate a full NYSE batch scan for MARKET SIGNUM.

Pipeline per run:
  1. fetch_nyse_tickers()         → NYSE universe (~3000 tickers)
  2. run_dalio_agent()            → macro regime_flag (once, shared for all tickers)
  3. For each ticker in universe:
       a. fetch_stock_data()      → stock data (cached 6h)
       b. pre_filter()            → market cap ≥ $500M AND avg vol 20d ≥ 500k
       c. [if pass] run graham, buffett, lynch, simons
       d. score_and_weight()      → final score + recommendation
       e. cache → .tmp/<TICKER>_score.json
  4. render_html(all_results)     → output/report.html (master dashboard)

Pre-filter thresholds:
  - Market cap:    fundamentals.market_cap ≥ 500_000_000
  - Avg vol (20d): computed from last 20 bars of price_history ≥ 500_000 shares

CLI:
  python tools/batch_runner.py                        # full NYSE scan
  python tools/batch_runner.py --limit 50             # test with first 50 tickers
  python tools/batch_runner.py --tickers AAPL,MSFT    # specific tickers only
  python tools/batch_runner.py --delay 1.0            # throttle yfinance requests
  python tools/batch_runner.py --no-cache             # bypass all caches
  python tools/batch_runner.py --skip-filter          # skip market cap / volume filter
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
OUTPUT_DIR = ROOT / "output"
TMP_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Add project root to path so we can import tools as modules
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.fetch_nyse_tickers import fetch_nyse_tickers
from tools.fetch_stock_data import fetch_stock_data
from tools.dalio_agent import run_dalio_agent
from tools.graham_agent import run_graham_agent
from tools.buffett_agent import run_buffett_agent
from tools.lynch_agent import run_lynch_agent
from tools.simons_agent import run_simons_agent
from tools.score_and_weight import score_and_weight
from tools.render_html import render_html

# ── Constants ─────────────────────────────────────────────────────────────────
MIN_MARKET_CAP = 500_000_000       # $500 million
MIN_AVG_VOLUME = 500_000           # 500k shares/day (20-day average)
DEFAULT_DELAY_SECONDS = 0.5        # pause between tickers (yfinance throttle)


# ---------------------------------------------------------------------------
# Pre-filter
# ---------------------------------------------------------------------------

def _compute_avg_volume(stock_data: dict, lookback: int = 20) -> float:
    """Compute average daily volume from the last `lookback` price history bars."""
    history = stock_data.get("price_history", [])
    if not history:
        return 0.0
    recent = history[-lookback:]
    vols = [bar.get("volume", 0) for bar in recent if bar.get("volume") is not None]
    return sum(vols) / len(vols) if vols else 0.0


def pre_filter(stock_data: dict) -> tuple[bool, str]:
    """
    Check whether a stock passes the minimum liquidity and size thresholds.

    Returns:
        (True, "") if the stock passes.
        (False, reason) if it fails, where reason describes why.
    """
    fundamentals = stock_data.get("fundamentals", {})

    market_cap = fundamentals.get("market_cap")
    if market_cap is None or market_cap < MIN_MARKET_CAP:
        cap_str = f"${market_cap/1e6:.0f}M" if market_cap else "N/A"
        return False, f"market cap {cap_str} < $500M"

    avg_vol = _compute_avg_volume(stock_data)
    if avg_vol < MIN_AVG_VOLUME:
        return False, f"avg vol {avg_vol:,.0f} < 500k"

    return True, ""


# ---------------------------------------------------------------------------
# Single-ticker pipeline
# ---------------------------------------------------------------------------

def _run_ticker(
    ticker: str,
    dalio_result: dict,
    use_cache: bool = True,
    skip_filter: bool = False,
) -> dict | None:
    """
    Execute the full per-ticker pipeline: fetch → filter → agents → score.

    Args:
        ticker:       Stock symbol.
        dalio_result: Pre-computed Dalio macro result (shared across all tickers).
        use_cache:    Whether to use cached stock data and agent results.
        skip_filter:  If True, bypass the market cap / volume pre-filter.

    Returns:
        Result dict on success, or None if the ticker was filtered/failed.
        Result dict structure: {
            "ticker":        str,
            "stock_data":    dict,
            "agent_results": dict,    # {dalio, graham, buffett, lynch, simons}
            "score_result":  dict,    # score_and_weight() output
            "filtered":      bool,
            "error":         str | None,
        }
    """
    # ── Fetch stock data ─────────────────────────────────────────────────────
    stock_data = fetch_stock_data(ticker, cache=use_cache)
    if "error" in stock_data:
        return {
            "ticker": ticker, "filtered": False, "error": stock_data["error"],
            "stock_data": None, "agent_results": None, "score_result": None,
        }

    # ── Pre-filter ────────────────────────────────────────────────────────────
    if not skip_filter:
        passed, reason = pre_filter(stock_data)
        if not passed:
            return {
                "ticker": ticker, "filtered": True, "error": reason,
                "stock_data": stock_data, "agent_results": None, "score_result": None,
            }

    # ── Persona agents ────────────────────────────────────────────────────────
    graham  = run_graham_agent(ticker,  stock_data, cache=use_cache)
    buffett = run_buffett_agent(ticker, stock_data, cache=use_cache)
    lynch   = run_lynch_agent(ticker,   stock_data, cache=use_cache)
    simons  = run_simons_agent(ticker,  stock_data, cache=use_cache)

    agent_results = {
        "dalio":   dalio_result,
        "graham":  graham,
        "buffett": buffett,
        "lynch":   lynch,
        "simons":  simons,
    }

    # ── Score + weight ────────────────────────────────────────────────────────
    score_result = score_and_weight(agent_results)

    return {
        "ticker":        ticker,
        "filtered":      False,
        "error":         None,
        "stock_data":    stock_data,
        "agent_results": agent_results,
        "score_result":  score_result,
    }


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def run_batch(
    limit: int | None = None,
    tickers_override: list[str] | None = None,
    delay: float = DEFAULT_DELAY_SECONDS,
    use_cache: bool = True,
    skip_filter: bool = False,
) -> dict:
    """
    Orchestrate a full NYSE batch scan.

    Args:
        limit:             If set, only process the first N tickers in the universe.
        tickers_override:  If set, use this ticker list instead of fetching NYSE universe.
        delay:             Seconds to pause between tickers (reduces yfinance throttling).
        use_cache:         Whether to reuse cached stock data and agent results.
        skip_filter:       If True, skip the pre-filter for all tickers.

    Returns:
        Summary dict: {
            "run_at":          str (ISO timestamp),
            "regime_flag":     str,
            "tickers_fetched": int,
            "passed_filter":   int,
            "scored":          int,
            "skipped":         int,
            "errors":          int,
            "results":         list[dict],   # one per scored ticker
            "report_path":     str | None,
        }
    """
    run_start = datetime.now()
    print(f"\n{'='*62}")
    print(f"  MARKET SIGNUM — Batch Scan")
    print(f"  Started: {run_start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*62}\n")

    # ── Step 1: Get ticker universe ───────────────────────────────────────────
    if tickers_override:
        universe = [{"ticker": t.upper().strip(), "name": "", "exchange": "NYSE", "cik": ""}
                    for t in tickers_override]
        print(f"Using override ticker list: {len(universe)} tickers")
    else:
        print("Step 1/4: Fetching NYSE ticker universe...")
        universe = fetch_nyse_tickers(use_cache=use_cache)
        print(f"  {len(universe)} tickers in NYSE universe\n")

    if limit:
        universe = universe[:limit]
        print(f"  [--limit {limit}] Processing first {len(universe)} tickers only\n")

    total = len(universe)

    # ── Step 2: Dalio macro (once per batch) ──────────────────────────────────
    print("Step 2/4: Running Dalio macro regime analysis (once for entire batch)...")
    try:
        dalio_result = run_dalio_agent("_MACRO", stock_data=None, cache=use_cache)
        regime_flag = dalio_result.get("regime_flag", "neutral")
        print(f"  ✓ Regime: {regime_flag.upper()} — {dalio_result.get('signal', 'N/A')}")
        print(f"    {dalio_result.get('reasoning', '')[:100]}...\n")
    except Exception as e:
        print(f"  ✗ Dalio failed ({e}) — falling back to neutral regime\n")
        dalio_result = {
            "ticker": "_MACRO", "signal": "watch", "confidence": 0.5,
            "regime_flag": "neutral", "reasoning": "Dalio agent unavailable; defaulting to neutral.",
        }
        regime_flag = "neutral"

    # ── Step 3: Per-ticker pipeline ───────────────────────────────────────────
    print(f"Step 3/4: Processing {total} tickers (delay={delay}s, filter={'off' if skip_filter else 'on'})...\n")

    scored_results: list[dict] = []
    filtered_count = 0
    error_count = 0
    skipped_count = 0

    for i, entry in enumerate(universe, 1):
        ticker = entry["ticker"]
        pct = i / total * 100
        print(f"  [{i:4d}/{total}  {pct:5.1f}%]  {ticker:<8}", end="", flush=True)

        try:
            result = _run_ticker(
                ticker,
                dalio_result=dalio_result,
                use_cache=use_cache,
                skip_filter=skip_filter,
            )
        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            error_count += 1
            if delay > 0:
                time.sleep(delay)
            continue

        if result is None:
            print(f"  (no result)", flush=True)
            skipped_count += 1
        elif result.get("filtered"):
            reason = result.get("error", "filtered")
            print(f"  SKIP  {reason}", flush=True)
            filtered_count += 1
        elif result.get("error"):
            print(f"  ERROR {result['error']}", flush=True)
            error_count += 1
        else:
            score  = result["score_result"].get("final_score", 0)
            rec    = result["score_result"].get("recommendation", "?")
            rec_sym = {"BUY": "🟢", "WATCH": "🟡", "AVOID": "🔴"}.get(rec, "⚪")
            print(f"  {rec_sym} {rec:<5}  score={score:.3f}", flush=True)
            scored_results.append(result)

        if delay > 0:
            time.sleep(delay)

    # ── Step 4: Render master dashboard ──────────────────────────────────────
    report_path = None
    if scored_results:
        print(f"\nStep 4/4: Rendering master dashboard ({len(scored_results)} stocks)...")
        try:
            report_path = render_html(scored_results, dalio_result=dalio_result)
            print(f"  ✓ Report written → {report_path}")
        except Exception as e:
            print(f"  ✗ render_html failed: {e}")
    else:
        print("\nStep 4/4: No scored results to render — skipping HTML output.")

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - run_start).total_seconds()
    buy_count   = sum(1 for r in scored_results if r["score_result"].get("recommendation") == "BUY")
    watch_count = sum(1 for r in scored_results if r["score_result"].get("recommendation") == "WATCH")
    avoid_count = sum(1 for r in scored_results if r["score_result"].get("recommendation") == "AVOID")

    print(f"\n{'='*62}")
    print(f"  BATCH SCAN COMPLETE  ({elapsed:.0f}s)")
    print(f"{'='*62}")
    print(f"  Tickers in universe : {total:>6,}")
    print(f"  Passed filter       : {len(scored_results) + 0:>6,}")
    print(f"  Filtered out        : {filtered_count:>6,}")
    print(f"  Errors / skipped    : {error_count + skipped_count:>6,}")
    print(f"  Scored              : {len(scored_results):>6,}")
    print(f"  ─────────────────────────────")
    print(f"  🟢 BUY              : {buy_count:>6,}")
    print(f"  🟡 WATCH            : {watch_count:>6,}")
    print(f"  🔴 AVOID            : {avoid_count:>6,}")
    print(f"  Macro regime        : {regime_flag.upper()}")
    if report_path:
        print(f"  Report              : {report_path}")
    print(f"{'='*62}\n")

    return {
        "run_at":          run_start.isoformat(),
        "regime_flag":     regime_flag,
        "tickers_fetched": total,
        "passed_filter":   len(scored_results),
        "scored":          len(scored_results),
        "filtered":        filtered_count,
        "errors":          error_count,
        "skipped":         skipped_count,
        "results":         scored_results,
        "report_path":     str(report_path) if report_path else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a full MARKET SIGNUM batch scan of NYSE-listed stocks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/batch_runner.py
  python tools/batch_runner.py --limit 50
  python tools/batch_runner.py --tickers AAPL,MSFT,NVDA,JNJ
  python tools/batch_runner.py --delay 1.0 --no-cache
  python tools/batch_runner.py --tickers AAPL --skip-filter
        """,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N tickers (useful for test runs).",
    )
    parser.add_argument(
        "--tickers",
        type=str,
        default=None,
        metavar="SYM[,SYM,...]",
        help="Comma-separated list of specific tickers to analyze instead of the full NYSE universe.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        metavar="SECS",
        help=f"Seconds to pause between tickers (default: {DEFAULT_DELAY_SECONDS}). "
             "Increase to avoid yfinance rate limiting on large runs.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass all caches (stock data, agent results, ticker list).",
    )
    parser.add_argument(
        "--skip-filter",
        action="store_true",
        help="Skip the market cap / volume pre-filter (analyze all tickers).",
    )
    args = parser.parse_args()

    tickers_override: list[str] | None = None
    if args.tickers:
        tickers_override = [t.strip() for t in args.tickers.split(",") if t.strip()]

    run_batch(
        limit=args.limit,
        tickers_override=tickers_override,
        delay=args.delay,
        use_cache=not args.no_cache,
        skip_filter=args.skip_filter,
    )


if __name__ == "__main__":
    main()
