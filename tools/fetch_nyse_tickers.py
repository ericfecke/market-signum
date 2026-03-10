#!/usr/bin/env python3
"""
fetch_nyse_tickers.py — Pull the current list of NYSE-listed stocks.

Source priority:
  1. SEC EDGAR company_tickers_exchange.json (filtered to NYSE)
  2. Wikipedia S&P 500 constituent list (fallback, ~500 tickers)
  3. Stale .tmp/nyse_tickers.csv (last resort if network unreachable)

Output: list[dict] — keys: ticker, name, exchange, cik
Cache:  .tmp/nyse_tickers.csv — 24-hour TTL (ticker list changes slowly)

Notes:
  - Ticker dots are replaced with hyphens (BRK.B → BRK-B) for yfinance compat
  - CIK is empty for Wikipedia-sourced tickers; batch_runner doesn't need it
  - SEC EDGAR is the authoritative source; ~3,000+ NYSE listings

Usage:
  python tools/fetch_nyse_tickers.py             # fetch and cache
  python tools/fetch_nyse_tickers.py --list      # print all ticker symbols
  python tools/fetch_nyse_tickers.py --no-cache  # force fresh network fetch
"""

import argparse
import csv
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
CACHE_FILE = TMP_DIR / "nyse_tickers.csv"
CACHE_TTL_SECONDS = 24 * 3600  # 24 hours

# SEC EDGAR: all exchange-listed companies (includes exchange field)
SEC_EDGAR_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# Wikipedia fallback: S&P 500 table (pandas.read_html)
WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Polite user-agent required by SEC EDGAR terms of service
HEADERS = {
    "User-Agent": "MARKET SIGNUM research tool research@marketsignum.local",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_ticker(raw: str) -> str:
    """Uppercase and replace dots with hyphens for yfinance compatibility."""
    return raw.strip().upper().replace(".", "-")


def _is_cache_valid() -> bool:
    """Return True if cache file exists and is younger than CACHE_TTL_SECONDS."""
    if not CACHE_FILE.exists():
        return False
    age = time.time() - CACHE_FILE.stat().st_mtime
    return age < CACHE_TTL_SECONDS


def _load_cache() -> list[dict]:
    """Read ticker list from .tmp/nyse_tickers.csv."""
    tickers: list[dict] = []
    with open(CACHE_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tickers.append(dict(row))
    return tickers


def _save_cache(tickers: list[dict]) -> None:
    """Write ticker list to .tmp/nyse_tickers.csv."""
    if not tickers:
        return
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ticker", "name", "exchange", "cik"]
    with open(CACHE_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(tickers)


# ---------------------------------------------------------------------------
# Source 1: SEC EDGAR
# ---------------------------------------------------------------------------

def _fetch_sec_edgar() -> list[dict]:
    """
    Pull NYSE tickers from SEC EDGAR company_tickers_exchange.json.

    The JSON has the structure:
      {
        "fields": ["cik_str", "name", "ticker", "exchange"],
        "data":   [[cik, name, ticker, exchange], ...]
      }

    Filters to rows where exchange == "NYSE".
    Returns list of {ticker, name, exchange, cik}.
    """
    print("Fetching NYSE ticker list from SEC EDGAR...", flush=True)
    resp = requests.get(SEC_EDGAR_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    fields: list[str] = data.get("fields", [])
    rows: list[list] = data.get("data", [])

    if not fields or not rows:
        raise ValueError("SEC EDGAR response is empty or has unexpected structure.")

    # Build index of field positions
    fi = {name: idx for idx, name in enumerate(fields)}

    # Required field positions
    for required in ("cik_str", "name", "ticker", "exchange"):
        if required not in fi:
            raise ValueError(f"SEC EDGAR response missing expected field '{required}'.")

    tickers: list[dict] = []
    for row in rows:
        exchange = str(row[fi["exchange"]]).strip()
        if exchange != "NYSE":
            continue
        ticker_raw = str(row[fi["ticker"]]).strip()
        name = str(row[fi["name"]]).strip()
        cik = str(row[fi["cik_str"]]).strip()

        ticker = _normalize_ticker(ticker_raw)
        if not ticker:
            continue

        tickers.append({
            "ticker":   ticker,
            "name":     name,
            "exchange": "NYSE",
            "cik":      cik,
        })

    print(f"  ✓ SEC EDGAR: {len(tickers)} NYSE tickers", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# Source 2: Wikipedia S&P 500 (fallback)
# ---------------------------------------------------------------------------

def _fetch_wikipedia_fallback() -> list[dict]:
    """
    Fallback: pull S&P 500 constituents from Wikipedia via pandas.read_html().

    Returns approximately 500 tickers, not the full NYSE universe.
    Stocks listed on NASDAQ (visible when Wikipedia includes an Exchange column)
    are excluded. If no exchange column is present, all ~500 tickers are kept
    and the exchange field defaults to 'NYSE'.

    This is a degraded mode — prefer SEC EDGAR.
    """
    print("SEC EDGAR unavailable. Trying Wikipedia S&P 500 fallback...", flush=True)
    try:
        import pandas as pd  # only needed here; keep import local for clarity
    except ImportError:
        print("  ✗ pandas not installed — cannot use Wikipedia fallback.", flush=True)
        return []

    try:
        tables = pd.read_html(WIKIPEDIA_SP500_URL, header=0)
    except Exception as e:
        print(f"  ✗ Wikipedia fallback: could not read page — {e}", flush=True)
        return []

    if not tables:
        print("  ✗ Wikipedia fallback: no tables found on page.", flush=True)
        return []

    df = tables[0]  # first table is always the S&P 500 constituent list

    # Identify columns by common name patterns
    def _find_col(keywords: list[str]) -> str | None:
        for kw in keywords:
            for col in df.columns:
                if kw.lower() in str(col).lower():
                    return col
        return None

    ticker_col   = _find_col(["symbol", "ticker"])   or df.columns[0]
    name_col     = _find_col(["security", "company", "name"]) or (df.columns[1] if len(df.columns) > 1 else ticker_col)
    exchange_col = _find_col(["exchange"])

    tickers: list[dict] = []
    for _, row in df.iterrows():
        ticker_raw = str(row[ticker_col]).strip()
        name       = str(row[name_col]).strip()
        exchange   = str(row[exchange_col]).strip() if exchange_col else "NYSE"

        # Skip NASDAQ-listed stocks when exchange info is available
        if exchange_col and "NASDAQ" in exchange.upper():
            continue

        ticker = _normalize_ticker(ticker_raw)
        if not ticker or ticker == "NAN":
            continue

        tickers.append({
            "ticker":   ticker,
            "name":     name,
            "exchange": exchange if exchange_col else "NYSE",
            "cik":      "",
        })

    print(f"  ✓ Wikipedia fallback: {len(tickers)} tickers", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_nyse_tickers(use_cache: bool = True) -> list[dict]:
    """
    Return the current list of NYSE-listed stocks.

    Source priority:
      1. .tmp/nyse_tickers.csv if valid cache and use_cache=True
      2. SEC EDGAR company_tickers_exchange.json
      3. Wikipedia S&P 500 (fallback, ~500 tickers, not full NYSE)
      4. Stale .tmp/nyse_tickers.csv (last resort if all network sources fail)

    Args:
        use_cache: Skip network fetch if a valid cache file exists (default True).

    Returns:
        list of dicts: [{ticker, name, exchange, cik}, ...]

    Raises:
        RuntimeError: If all sources fail and no cache exists.
    """
    # ── 1. Valid cache ──────────────────────────────────────────────────────
    if use_cache and _is_cache_valid():
        print("Loading NYSE tickers from cache (.tmp/nyse_tickers.csv)...", flush=True)
        tickers = _load_cache()
        print(f"  ✓ {len(tickers)} tickers from cache", flush=True)
        return tickers

    # ── 2. SEC EDGAR ────────────────────────────────────────────────────────
    tickers: list[dict] = []
    try:
        tickers = _fetch_sec_edgar()
    except Exception as e:
        print(f"  ✗ SEC EDGAR failed: {e}", flush=True)

    # ── 3. Wikipedia fallback ───────────────────────────────────────────────
    if not tickers:
        tickers = _fetch_wikipedia_fallback()

    # ── 4. Stale cache (last resort) ────────────────────────────────────────
    if not tickers:
        if CACHE_FILE.exists():
            print(
                "All network sources failed. Loading stale cache as last resort...",
                flush=True,
            )
            tickers = _load_cache()
            print(f"  ✓ Stale cache: {len(tickers)} tickers", flush=True)
            return tickers  # don't overwrite the stale cache with empty data
        raise RuntimeError(
            "Could not retrieve NYSE tickers from any source and no local cache exists. "
            "Check network connectivity, or place a nyse_tickers.csv in .tmp/."
        )

    # Write fresh cache
    _save_cache(tickers)
    print(f"  Cache written → .tmp/nyse_tickers.csv", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch the current list of NYSE-listed stocks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/fetch_nyse_tickers.py             # fetch and cache
  python tools/fetch_nyse_tickers.py --list      # print ticker symbols to stdout
  python tools/fetch_nyse_tickers.py --no-cache  # force fresh network fetch
        """,
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the local cache and force a fresh fetch from the network.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print all ticker symbols (one per line) after fetching.",
    )
    args = parser.parse_args()

    try:
        tickers = fetch_nyse_tickers(use_cache=not args.no_cache)
    except RuntimeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        for t in sorted(tickers, key=lambda x: x["ticker"]):
            print(t["ticker"])
    else:
        print(f"\nDone. {len(tickers)} NYSE tickers available.")
        print(f"Cache: {CACHE_FILE}")


if __name__ == "__main__":
    main()
