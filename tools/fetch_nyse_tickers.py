#!/usr/bin/env python3
"""
fetch_nyse_tickers.py — Pull the current list of NYSE-listed stocks.

Source priority (tried in order until one succeeds):
  1. SEC EDGAR company_tickers_exchange.json  (primary — full NYSE universe ~3 000+)
  2. Wikipedia S&P 500 constituent list       (fallback — ~350 NYSE stocks)
  3. NASDAQ Trader nasdaqtraded.txt           (fallback — all US listings, filter NYSE)
  4. Hardcoded top-500 NYSE list              (last resort — never fails, no network needed)

Cache:  .tmp/nyse_tickers.csv — 24-hour TTL written after any successful live fetch.
        Stale cache is preferred over the hardcoded list when all live sources fail.

Notes:
  - Ticker dots → hyphens (BRK.B → BRK-B) for yfinance compatibility
  - CIK is empty for non-EDGAR sources; batch_runner doesn't require it
  - SEC EDGAR company_tickers_exchange.json uses field name "cik" (not "cik_str");
    both variants are accepted defensively

Usage:
  python tools/fetch_nyse_tickers.py             # fetch and cache
  python tools/fetch_nyse_tickers.py --list      # print all ticker symbols
  python tools/fetch_nyse_tickers.py --no-cache  # force fresh network fetch
"""

import argparse
import csv
import io
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
CACHE_FILE = TMP_DIR / "nyse_tickers.csv"
CACHE_TTL_SECONDS = 24 * 3600  # 24 hours

# Source URLs
SEC_EDGAR_URL       = "https://www.sec.gov/files/company_tickers_exchange.json"
WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ_TRADER_URL   = "https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"

# SEC EDGAR requires a descriptive user-agent per their ToS
SEC_HEADERS = {
    "User-Agent": "MARKET SIGNUM research tool research@marketsignum.local",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

# Browser-like headers for Wikipedia and NASDAQ Trader (avoids 403s)
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
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
    return (time.time() - CACHE_FILE.stat().st_mtime) < CACHE_TTL_SECONDS


def _load_cache() -> list[dict]:
    """Read ticker list from .tmp/nyse_tickers.csv."""
    tickers: list[dict] = []
    with open(CACHE_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tickers.append(dict(row))
    return tickers


def _save_cache(tickers: list[dict]) -> None:
    """Write ticker list to .tmp/nyse_tickers.csv."""
    if not tickers:
        return
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["ticker", "name", "exchange", "cik"], extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(tickers)


# ---------------------------------------------------------------------------
# Source 1: SEC EDGAR
# ---------------------------------------------------------------------------

def _fetch_sec_edgar() -> list[dict]:
    """
    Pull NYSE tickers from SEC EDGAR company_tickers_exchange.json.

    Response structure:
      { "fields": ["cik", "name", "ticker", "exchange"], "data": [[...], ...] }

    Note: the CIK field may be named "cik" or "cik_str" depending on the
    API version — both are accepted. CIK is optional; we degrade gracefully
    if it's absent rather than failing the whole fetch.
    """
    print("Fetching NYSE ticker list from SEC EDGAR...", flush=True)
    resp = requests.get(SEC_EDGAR_URL, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()

    data   = resp.json()
    fields: list[str] = data.get("fields") or []
    rows:   list[list] = data.get("data")   or []

    if not fields or not rows:
        raise ValueError("SEC EDGAR: empty or unrecognised response structure.")

    fi = {name: idx for idx, name in enumerate(fields)}

    # Validate that the three required fields are present
    for required in ("name", "ticker", "exchange"):
        if required not in fi:
            raise ValueError(
                f"SEC EDGAR: required field '{required}' not found in response. "
                f"Got fields: {fields}"
            )

    # CIK field: try both known variants, fall back to None
    cik_key = "cik" if "cik" in fi else ("cik_str" if "cik_str" in fi else None)

    tickers: list[dict] = []
    for row in rows:
        if len(row) <= fi["exchange"]:
            continue
        exchange = str(row[fi["exchange"]]).strip()
        if exchange != "NYSE":
            continue

        ticker_raw = str(row[fi["ticker"]]).strip()
        name       = str(row[fi["name"]]).strip()
        cik        = str(row[fi[cik_key]]).strip() if cik_key else ""

        ticker = _normalize_ticker(ticker_raw)
        if not ticker:
            continue

        tickers.append({"ticker": ticker, "name": name, "exchange": "NYSE", "cik": cik})

    if not tickers:
        raise ValueError("SEC EDGAR: parsed successfully but found 0 NYSE tickers.")

    print(f"  ✓ SEC EDGAR: {len(tickers)} NYSE tickers", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# Source 2: Wikipedia S&P 500 (fallback)
# ---------------------------------------------------------------------------

def _fetch_wikipedia_fallback() -> list[dict]:
    """
    Fallback: pull S&P 500 constituents from Wikipedia.

    Uses requests with browser-like headers to avoid 403s (pandas.read_html()
    alone sends a bare Python user-agent that Wikipedia blocks). The HTML is
    fetched manually and passed to pandas via io.StringIO.

    Returns ~350 NYSE-listed stocks (NASDAQ constituents are filtered out when
    the Exchange column is present, otherwise all ~500 are kept).
    """
    print("Trying Wikipedia S&P 500 fallback...", flush=True)
    try:
        import pandas as pd
    except ImportError:
        print("  ✗ pandas not installed — skipping Wikipedia fallback.", flush=True)
        return []

    try:
        resp = requests.get(WIKIPEDIA_SP500_URL, headers=WEB_HEADERS, timeout=30)
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text), header=0)
    except Exception as e:
        print(f"  ✗ Wikipedia fallback failed: {e}", flush=True)
        return []

    if not tables:
        print("  ✗ Wikipedia: no tables found on page.", flush=True)
        return []

    df = tables[0]

    def _find_col(keywords: list[str]) -> str | None:
        for kw in keywords:
            for col in df.columns:
                if kw.lower() in str(col).lower():
                    return col
        return None

    ticker_col   = _find_col(["symbol", "ticker"]) or df.columns[0]
    name_col     = _find_col(["security", "company", "name"]) or (df.columns[1] if len(df.columns) > 1 else ticker_col)
    exchange_col = _find_col(["exchange"])

    tickers: list[dict] = []
    for _, row in df.iterrows():
        ticker_raw = str(row[ticker_col]).strip()
        name       = str(row[name_col]).strip()
        exchange   = str(row[exchange_col]).strip() if exchange_col else "NYSE"

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

    if not tickers:
        print("  ✗ Wikipedia: parsed table but found no usable tickers.", flush=True)
        return []

    print(f"  ✓ Wikipedia fallback: {len(tickers)} tickers", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# Source 3: NASDAQ Trader nasdaqtraded.txt (fallback)
# ---------------------------------------------------------------------------

def _fetch_nasdaq_trader() -> list[dict]:
    """
    Fallback: pull NYSE listings from NASDAQ Trader's nasdaqtraded.txt.

    This is a public flat file (no authentication required) that lists every
    US-exchange-listed security. We filter for:
      - Listing Exchange == 'N'  (NYSE)
      - ETF != 'Y'               (skip exchange-traded funds)

    File format — pipe-delimited, first line is a header, last line is a
    file-creation timestamp (not a data row):
      Nasdaq Traded | Symbol | Security Name | Listing Exchange | Market Category |
      ETF | Round Lot Size | Test Issue | Financial Status | CQS Symbol |
      NASDAQ Symbol | NextShares

    Listing Exchange values: Q/G/S=NASDAQ, N=NYSE, A=NYSE American,
                             P=NYSE Arca, Z=BATS, V=IEX
    """
    print("Trying NASDAQ Trader nasdaqtraded.txt fallback...", flush=True)
    try:
        resp = requests.get(NASDAQ_TRADER_URL, headers=WEB_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ✗ NASDAQ Trader fetch failed: {e}", flush=True)
        return []

    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        print("  ✗ NASDAQ Trader: response too short to parse.", flush=True)
        return []

    # First line is the header
    header = [h.strip() for h in lines[0].split("|")]
    fi     = {name: idx for idx, name in enumerate(header)}

    for required in ("Symbol", "Security Name", "Listing Exchange", "ETF"):
        if required not in fi:
            print(
                f"  ✗ NASDAQ Trader: expected column '{required}' not found. "
                f"Header: {header[:6]}…",
                flush=True,
            )
            return []

    sym_idx  = fi["Symbol"]
    name_idx = fi["Security Name"]
    exch_idx = fi["Listing Exchange"]
    etf_idx  = fi["ETF"]
    min_cols = max(sym_idx, name_idx, exch_idx, etf_idx) + 1

    tickers: list[dict] = []
    for line in lines[1:]:
        # Skip the file-creation timestamp footer line
        if line.startswith("File Creation"):
            continue
        fields = line.split("|")
        if len(fields) < min_cols:
            continue

        exchange = fields[exch_idx].strip()
        if exchange != "N":          # 'N' = NYSE (not NASDAQ)
            continue

        if fields[etf_idx].strip() == "Y":
            continue                 # skip ETFs

        ticker_raw = fields[sym_idx].strip()
        name       = fields[name_idx].strip()

        ticker = _normalize_ticker(ticker_raw)
        if not ticker:
            continue

        tickers.append({"ticker": ticker, "name": name, "exchange": "NYSE", "cik": ""})

    if not tickers:
        print("  ✗ NASDAQ Trader: parsed file but found no NYSE tickers.", flush=True)
        return []

    print(f"  ✓ NASDAQ Trader: {len(tickers)} NYSE tickers", flush=True)
    return tickers


# ---------------------------------------------------------------------------
# Source 4: Hardcoded top-500 NYSE list (absolute last resort)
# ---------------------------------------------------------------------------

# fmt: off
_TOP_500_NYSE: list[tuple[str, str]] = [
    # ── Financials ────────────────────────────────────────────────────────
    ("JPM",  "JPMorgan Chase & Co."),
    ("BAC",  "Bank of America Corp."),
    ("WFC",  "Wells Fargo & Co."),
    ("C",    "Citigroup Inc."),
    ("GS",   "Goldman Sachs Group Inc."),
    ("MS",   "Morgan Stanley"),
    ("BLK",  "BlackRock Inc."),
    ("AXP",  "American Express Co."),
    ("USB",  "U.S. Bancorp"),
    ("PNC",  "PNC Financial Services Group"),
    ("TFC",  "Truist Financial Corp."),
    ("COF",  "Capital One Financial Corp."),
    ("BK",   "Bank of New York Mellon Corp."),
    ("STT",  "State Street Corp."),
    ("SYF",  "Synchrony Financial"),
    ("RF",   "Regions Financial Corp."),
    ("FITB", "Fifth Third Bancorp"),
    ("HBAN", "Huntington Bancshares Inc."),
    ("KEY",  "KeyCorp"),
    ("CFG",  "Citizens Financial Group Inc."),
    ("MTB",  "M&T Bank Corp."),
    ("NTRS", "Northern Trust Corp."),
    ("CMA",  "Comerica Inc."),
    ("SCHW", "Charles Schwab Corp."),
    ("CME",  "CME Group Inc."),
    ("ICE",  "Intercontinental Exchange Inc."),
    ("MCO",  "Moody's Corp."),
    ("SPGI", "S&P Global Inc."),
    ("CB",   "Chubb Ltd."),
    ("AIG",  "American International Group Inc."),
    ("MET",  "MetLife Inc."),
    ("PRU",  "Prudential Financial Inc."),
    ("AFL",  "Aflac Inc."),
    ("ALL",  "Allstate Corp."),
    ("TRV",  "Travelers Companies Inc."),
    ("HIG",  "Hartford Financial Services Group"),
    ("LNC",  "Lincoln National Corp."),
    ("UNM",  "Unum Group"),
    ("WRB",  "W.R. Berkley Corp."),
    ("RE",   "Everest Re Group Ltd."),
    ("MKL",  "Markel Group Inc."),
    ("CINF", "Cincinnati Financial Corp."),
    ("WTW",  "Willis Towers Watson PLC"),
    ("AON",  "Aon PLC"),
    ("MMC",  "Marsh & McLennan Companies Inc."),
    ("BRO",  "Brown & Brown Inc."),
    ("AJG",  "Arthur J. Gallagher & Co."),
    ("ORI",  "Old Republic International Corp."),
    ("EG",   "Everest Group Ltd."),
    # ── Healthcare ────────────────────────────────────────────────────────
    ("JNJ",  "Johnson & Johnson"),
    ("PFE",  "Pfizer Inc."),
    ("ABBV", "AbbVie Inc."),
    ("MRK",  "Merck & Co. Inc."),
    ("BMY",  "Bristol-Myers Squibb Co."),
    ("LLY",  "Eli Lilly and Co."),
    ("CVS",  "CVS Health Corp."),
    ("ELV",  "Elevance Health Inc."),
    ("CI",   "Cigna Group"),
    ("HUM",  "Humana Inc."),
    ("CNC",  "Centene Corp."),
    ("MOH",  "Molina Healthcare Inc."),
    ("ABC",  "AmerisourceBergen Corp."),
    ("MCK",  "McKesson Corp."),
    ("CAH",  "Cardinal Health Inc."),
    ("DGX",  "Quest Diagnostics Inc."),
    ("LH",   "Laboratory Corp. of America"),
    ("BAX",  "Baxter International Inc."),
    ("BDX",  "Becton Dickinson and Co."),
    ("ZBH",  "Zimmer Biomet Holdings Inc."),
    ("SYK",  "Stryker Corp."),
    ("EW",   "Edwards Lifesciences Corp."),
    ("MTD",  "Mettler-Toledo International Inc."),
    ("WAT",  "Waters Corp."),
    ("IQV",  "IQVIA Holdings Inc."),
    ("CRL",  "Charles River Laboratories"),
    ("CTLT", "Catalent Inc."),
    ("THC",  "Tenet Healthcare Corp."),
    ("UHS",  "Universal Health Services Inc."),
    ("HCA",  "HCA Healthcare Inc."),
    ("ABT",  "Abbott Laboratories"),
    ("BSX",  "Boston Scientific Corp."),
    ("MDT",  "Medtronic PLC"),
    ("BIO",  "Bio-Rad Laboratories Inc."),
    ("PRGO", "Perrigo Company PLC"),
    # ── Consumer Staples ──────────────────────────────────────────────────
    ("KO",   "Coca-Cola Co."),
    ("PEP",  "PepsiCo Inc."),
    ("PG",   "Procter & Gamble Co."),
    ("WMT",  "Walmart Inc."),
    ("KR",   "Kroger Co."),
    ("SYY",  "Sysco Corp."),
    ("GIS",  "General Mills Inc."),
    ("K",    "Kellanova"),
    ("CPB",  "Campbell Soup Co."),
    ("HRL",  "Hormel Foods Corp."),
    ("SJM",  "J.M. Smucker Co."),
    ("CAG",  "Conagra Brands Inc."),
    ("MKC",  "McCormick & Company Inc."),
    ("HSY",  "Hershey Co."),
    ("CLX",  "Clorox Co."),
    ("CHD",  "Church & Dwight Co."),
    ("CL",   "Colgate-Palmolive Co."),
    ("KMB",  "Kimberly-Clark Corp."),
    ("EL",   "Estee Lauder Companies Inc."),
    ("NWL",  "Newell Brands Inc."),
    ("TSN",  "Tyson Foods Inc."),
    ("PM",   "Philip Morris International Inc."),
    ("MO",   "Altria Group Inc."),
    ("BTI",  "British American Tobacco PLC ADR"),
    # ── Consumer Discretionary ────────────────────────────────────────────
    ("HD",   "Home Depot Inc."),
    ("MCD",  "McDonald's Corp."),
    ("YUM",  "Yum! Brands Inc."),
    ("DRI",  "Darden Restaurants Inc."),
    ("SHW",  "Sherwin-Williams Co."),
    ("NKE",  "Nike Inc."),
    ("F",    "Ford Motor Co."),
    ("GM",   "General Motors Co."),
    ("TGT",  "Target Corp."),
    ("TJX",  "TJX Companies Inc."),
    ("ROST", "Ross Stores Inc."),
    ("KSS",  "Kohl's Corp."),
    ("M",    "Macy's Inc."),
    ("JWN",  "Nordstrom Inc."),
    ("BBWI", "Bath & Body Works Inc."),
    ("GPS",  "Gap Inc."),
    ("ANF",  "Abercrombie & Fitch Co."),
    ("PVH",  "PVH Corp."),
    ("HBI",  "Hanesbrands Inc."),
    ("VFC",  "VF Corp."),
    ("RL",   "Ralph Lauren Corp."),
    ("MHK",  "Mohawk Industries Inc."),
    ("WHR",  "Whirlpool Corp."),
    ("LEG",  "Leggett & Platt Inc."),
    ("AZO",  "AutoZone Inc."),
    ("DLTR", "Dollar Tree Inc."),
    ("DG",   "Dollar General Corp."),
    ("BURL", "Burlington Stores Inc."),
    ("FIVE", "Five Below Inc."),
    ("WSM",  "Williams-Sonoma Inc."),
    ("RH",   "RH (Restoration Hardware)"),
    ("DIS",  "Walt Disney Co."),
    ("LVS",  "Las Vegas Sands Corp."),
    ("MGM",  "MGM Resorts International"),
    ("WYNN", "Wynn Resorts Ltd."),
    ("CZR",  "Caesars Entertainment Inc."),
    ("HOG",  "Harley-Davidson Inc."),
    ("BC",   "Brunswick Corp."),
    ("KMX",  "CarMax Inc."),
    ("AN",   "AutoNation Inc."),
    ("PAG",  "Penske Automotive Group Inc."),
    ("ABG",  "Asbury Automotive Group Inc."),
    ("ALLY", "Ally Financial Inc."),
    ("LOW",  "Lowe's Companies Inc."),
    # ── Energy ────────────────────────────────────────────────────────────
    ("XOM",  "Exxon Mobil Corp."),
    ("CVX",  "Chevron Corp."),
    ("COP",  "ConocoPhillips"),
    ("OXY",  "Occidental Petroleum Corp."),
    ("PSX",  "Phillips 66"),
    ("VLO",  "Valero Energy Corp."),
    ("MPC",  "Marathon Petroleum Corp."),
    ("HES",  "Hess Corp."),
    ("DVN",  "Devon Energy Corp."),
    ("EOG",  "EOG Resources Inc."),
    ("PXD",  "Pioneer Natural Resources Co."),
    ("HAL",  "Halliburton Co."),
    ("SLB",  "SLB (Schlumberger)"),
    ("BKR",  "Baker Hughes Co."),
    ("NOV",  "NOV Inc."),
    ("RRC",  "Range Resources Corp."),
    ("AR",   "Antero Resources Corp."),
    ("CNX",  "CNX Resources Corp."),
    ("SM",   "SM Energy Co."),
    ("CIVI", "Civitas Resources Inc."),
    ("LBRT", "Liberty Energy Inc."),
    ("WMB",  "Williams Companies Inc."),
    ("ET",   "Energy Transfer LP"),
    ("KMI",  "Kinder Morgan Inc."),
    ("OKE",  "ONEOK Inc."),
    ("TRGP", "Targa Resources Corp."),
    ("EQT",  "EQT Corp."),
    ("SWN",  "Southwestern Energy Co."),
    ("MRO",  "Marathon Oil Corp."),
    ("FANG", "Diamondback Energy Inc."),
    ("PR",   "Permian Resources Corp."),
    # ── Industrials ───────────────────────────────────────────────────────
    ("CAT",  "Caterpillar Inc."),
    ("DE",   "Deere & Company"),
    ("BA",   "Boeing Co."),
    ("GE",   "GE Aerospace"),
    ("HON",  "Honeywell International Inc."),
    ("MMM",  "3M Co."),
    ("UPS",  "United Parcel Service Inc."),
    ("FDX",  "FedEx Corp."),
    ("RTX",  "RTX Corp."),
    ("LMT",  "Lockheed Martin Corp."),
    ("GD",   "General Dynamics Corp."),
    ("NOC",  "Northrop Grumman Corp."),
    ("LHX",  "L3Harris Technologies Inc."),
    ("LDOS", "Leidos Holdings Inc."),
    ("SAIC", "Science Applications International Corp."),
    ("BAH",  "Booz Allen Hamilton Holding Corp."),
    ("ITT",  "ITT Inc."),
    ("IEX",  "IDEX Corp."),
    ("PH",   "Parker-Hannifin Corp."),
    ("EMR",  "Emerson Electric Co."),
    ("ETN",  "Eaton Corp. PLC"),
    ("ROK",  "Rockwell Automation Inc."),
    ("AME",  "AMETEK Inc."),
    ("ROP",  "Roper Technologies Inc."),
    ("TDG",  "TransDigm Group Inc."),
    ("HWM",  "Howmet Aerospace Inc."),
    ("GXO",  "GXO Logistics Inc."),
    ("XPO",  "XPO Inc."),
    ("UNP",  "Union Pacific Corp."),
    ("CSX",  "CSX Corp."),
    ("NSC",  "Norfolk Southern Corp."),
    ("WAB",  "Westinghouse Air Brake Technologies"),
    ("TT",   "Trane Technologies PLC"),
    ("JCI",  "Johnson Controls International PLC"),
    ("AOS",  "A.O. Smith Corp."),
    ("IR",   "Ingersoll Rand Inc."),
    ("XYL",  "Xylem Inc."),
    ("VMC",  "Vulcan Materials Co."),
    ("MLM",  "Martin Marietta Materials Inc."),
    ("CRH",  "CRH PLC"),
    ("BLDR", "Builders FirstSource Inc."),
    ("MAS",  "Masco Corp."),
    ("FND",  "Floor & Decor Holdings Inc."),
    ("BWA",  "BorgWarner Inc."),
    ("LEA",  "Lear Corp."),
    ("ALV",  "Autoliv Inc."),
    ("DAN",  "Dana Inc."),
    ("TEN",  "Tenneco Inc."),
    ("DAL",  "Delta Air Lines Inc."),
    ("UAL",  "United Airlines Holdings Inc."),
    ("AAL",  "American Airlines Group Inc."),
    ("LUV",  "Southwest Airlines Co."),
    ("ALK",  "Alaska Air Group Inc."),
    # ── Materials ─────────────────────────────────────────────────────────
    ("NEM",  "Newmont Corp."),
    ("FCX",  "Freeport-McMoRan Inc."),
    ("BHP",  "BHP Group Ltd. ADR"),
    ("RIO",  "Rio Tinto PLC ADR"),
    ("VALE", "Vale S.A. ADR"),
    ("LIN",  "Linde PLC"),
    ("APD",  "Air Products and Chemicals Inc."),
    ("ECL",  "Ecolab Inc."),
    ("DD",   "DuPont de Nemours Inc."),
    ("DOW",  "Dow Inc."),
    ("LYB",  "LyondellBasell Industries N.V."),
    ("PPG",  "PPG Industries Inc."),
    ("RPM",  "RPM International Inc."),
    ("CF",   "CF Industries Holdings Inc."),
    ("MOS",  "Mosaic Co."),
    ("NTR",  "Nutrien Ltd."),
    ("FMC",  "FMC Corp."),
    ("ALB",  "Albemarle Corp."),
    ("SQM",  "Sociedad Quimica y Minera ADR"),
    ("BALL", "Ball Corp."),
    ("IP",   "International Paper Co."),
    ("WRK",  "WestRock Co."),
    ("PKG",  "Packaging Corp. of America"),
    ("SON",  "Sonoco Products Co."),
    ("SEE",  "Sealed Air Corp."),
    ("ATR",  "AptarGroup Inc."),
    ("CCK",  "Crown Holdings Inc."),
    ("SLGN", "Silgan Holdings Inc."),
    ("AA",   "Alcoa Corp."),
    ("STLD", "Steel Dynamics Inc."),
    ("NUE",  "Nucor Corp."),
    ("X",    "United States Steel Corp."),
    ("CLF",  "Cleveland-Cliffs Inc."),
    ("RS",   "Reliance Steel & Aluminum Co."),
    ("CMC",  "Commercial Metals Co."),
    # ── Utilities ─────────────────────────────────────────────────────────
    ("NEE",  "NextEra Energy Inc."),
    ("DUK",  "Duke Energy Corp."),
    ("SO",   "Southern Co."),
    ("AEP",  "American Electric Power Co."),
    ("D",    "Dominion Energy Inc."),
    ("EXC",  "Exelon Corp."),
    ("SRE",  "Sempra Energy"),
    ("PEG",  "Public Service Enterprise Group"),
    ("XEL",  "Xcel Energy Inc."),
    ("WEC",  "WEC Energy Group Inc."),
    ("ES",   "Eversource Energy"),
    ("AEE",  "Ameren Corp."),
    ("CMS",  "CMS Energy Corp."),
    ("CNP",  "CenterPoint Energy Inc."),
    ("NI",   "NiSource Inc."),
    ("LNT",  "Alliant Energy Corp."),
    ("EVRG", "Evergy Inc."),
    ("IDA",  "IDACORP Inc."),
    ("OGE",  "OGE Energy Corp."),
    ("POR",  "Portland General Electric Co."),
    ("MDU",  "MDU Resources Group Inc."),
    ("HE",   "Hawaiian Electric Industries Inc."),
    ("BKH",  "Black Hills Corp."),
    ("NWE",  "NorthWestern Corp."),
    ("AVA",  "Avista Corp."),
    ("SR",   "Spire Inc."),
    ("NWN",  "Northwest Natural Holding Co."),
    ("NFG",  "National Fuel Gas Co."),
    ("NJR",  "New Jersey Resources Corp."),
    # ── Real Estate (REITs) ───────────────────────────────────────────────
    ("SPG",  "Simon Property Group Inc."),
    ("AMT",  "American Tower Corp."),
    ("PLD",  "Prologis Inc."),
    ("CCI",  "Crown Castle Inc."),
    ("WELL", "Welltower Inc."),
    ("EQR",  "Equity Residential"),
    ("AVB",  "AvalonBay Communities Inc."),
    ("UDR",  "UDR Inc."),
    ("ESS",  "Essex Property Trust Inc."),
    ("MAA",  "Mid-America Apartment Communities"),
    ("NNN",  "NNN REIT Inc."),
    ("O",    "Realty Income Corp."),
    ("SRC",  "Spirit Realty Capital Inc."),
    ("ADC",  "Agree Realty Corp."),
    ("KIM",  "Kimco Realty Corp."),
    ("REG",  "Regency Centers Corp."),
    ("FRT",  "Federal Realty Investment Trust"),
    ("BXP",  "BXP Inc."),
    ("SLG",  "SL Green Realty Corp."),
    ("KRC",  "Kilroy Realty Corp."),
    ("DEI",  "Douglas Emmett Inc."),
    ("CUZ",  "Cousins Properties Inc."),
    ("MPW",  "Medical Properties Trust Inc."),
    ("OHI",  "Omega Healthcare Investors Inc."),
    ("NHI",  "National Health Investors Inc."),
    ("SBRA", "Sabra Health Care REIT Inc."),
    ("VTR",  "Ventas Inc."),
    ("ELS",  "Equity LifeStyle Properties Inc."),
    ("SUI",  "Sun Communities Inc."),
    ("INVH", "Invitation Homes Inc."),
    ("AMH",  "American Homes 4 Rent"),
    ("REXR", "Rexford Industrial Realty Inc."),
    ("PSA",  "Public Storage"),
    ("EXR",  "Extra Space Storage Inc."),
    ("CUBE", "CubeSmart"),
    ("IIPR", "Innovative Industrial Properties Inc."),
    # ── Technology (NYSE-listed) ──────────────────────────────────────────
    ("IBM",  "International Business Machines Corp."),
    ("ACN",  "Accenture PLC"),
    ("IT",   "Gartner Inc."),
    ("WU",   "Western Union Co."),
    ("FIS",  "Fidelity National Information Services Inc."),
    ("GPN",  "Global Payments Inc."),
    ("NCR",  "NCR Atleos Corp."),
    ("EFX",  "Equifax Inc."),
    ("DXC",  "DXC Technology Co."),
    ("EPAM", "EPAM Systems Inc."),
    ("GLOB", "Globant S.A."),
    ("EXLS", "ExlService Holdings Inc."),
    ("ASGN", "ASGN Inc."),
    ("BR",   "Broadridge Financial Solutions Inc."),
    ("WEX",  "WEX Inc."),
    ("WEX",  "WEX Inc."),
    ("GDDY", "GoDaddy Inc."),
    ("DLB",  "Dolby Laboratories Inc."),
    ("PAYO", "Payoneer Global Inc."),
    ("PRFT", "Perficient Inc."),
    ("VNET", "VNET Group Inc. ADR"),
    # ── Communication Services ────────────────────────────────────────────
    ("T",    "AT&T Inc."),
    ("VZ",   "Verizon Communications Inc."),
    ("DIS",  "Walt Disney Co."),
    ("IPG",  "Interpublic Group of Companies Inc."),
    ("OMC",  "Omnicom Group Inc."),
    ("NYT",  "New York Times Co."),
    ("GHC",  "Graham Holdings Co."),
    ("NWS",  "News Corp."),
    ("NWSA", "News Corp. Class A"),
    # ── Homebuilders ──────────────────────────────────────────────────────
    ("LEN",  "Lennar Corp."),
    ("DHI",  "D.R. Horton Inc."),
    ("PHM",  "PulteGroup Inc."),
    ("NVR",  "NVR Inc."),
    ("TOL",  "Toll Brothers Inc."),
    ("MDC",  "MDC Holdings Inc."),
    ("MHO",  "M/I Homes Inc."),
    ("TMHC", "Taylor Morrison Home Corp."),
    ("MTH",  "Meritage Homes Corp."),
    ("LGIH", "LGI Homes Inc."),
    ("SKY",  "Skyline Champion Corp."),
    # ── International ADRs (NYSE-listed) ─────────────────────────────────
    ("TM",   "Toyota Motor Corp. ADR"),
    ("SNY",  "Sanofi ADR"),
    ("NVO",  "Novo Nordisk A/S ADR"),
    ("AZN",  "AstraZeneca PLC ADR"),
    ("GSK",  "GSK PLC ADR"),
    ("UL",   "Unilever PLC ADR"),
    ("DEO",  "Diageo PLC ADR"),
    ("BP",   "BP PLC ADR"),
    ("SHEL", "Shell PLC ADR"),
    ("E",    "Eni S.p.A. ADR"),
    ("EQNR", "Equinor ASA ADR"),
    ("HDB",  "HDFC Bank Ltd. ADR"),
    ("IBN",  "ICICI Bank Ltd. ADR"),
    ("WIT",  "Wipro Ltd. ADR"),
    ("INFY", "Infosys Ltd. ADR"),
    ("BABA", "Alibaba Group Holding Ltd. ADR"),
    ("NIO",  "NIO Inc. ADR"),
    ("XPEV", "XPeng Inc. ADR"),
    ("LI",   "Li Auto Inc. ADR"),
    ("SE",   "Sea Ltd. ADR"),
    ("GRAB", "Grab Holdings Ltd."),
    # ── Misc large-caps ───────────────────────────────────────────────────
    ("BRK-B","Berkshire Hathaway Inc. Class B"),
    ("BRK-A","Berkshire Hathaway Inc. Class A"),
    ("WBD",  "Warner Bros. Discovery Inc."),
    ("PARA", "Paramount Global"),
    ("LYV",  "Live Nation Entertainment Inc."),
    ("MSG",  "Madison Square Garden Sports Corp."),
    ("WWE",  "World Wrestling Entertainment Inc."),
    ("IAA",  "IAA Inc."),
    ("KAR",  "OPENLANE Inc."),
    ("NCLH", "Norwegian Cruise Line Holdings Ltd."),
    ("CCL",  "Carnival Corp."),
    ("RCL",  "Royal Caribbean Cruises Ltd."),
    ("HLT",  "Hilton Worldwide Holdings Inc."),
    ("MAR",  "Marriott International Inc."),
    ("H",    "Hyatt Hotels Corp."),
    ("IHG",  "InterContinental Hotels Group ADR"),
    ("WH",   "Wyndham Hotels & Resorts Inc."),
    ("CHH",  "Choice Hotels International Inc."),
    ("VAC",  "Marriott Vacations Worldwide Corp."),
    ("HGV",  "Hilton Grand Vacations Inc."),
    ("TNL",  "Travel + Leisure Co."),
]
# fmt: on

# Deduplicate while preserving order
_SEEN: set[str] = set()
_TOP_500_NYSE_DEDUPED: list[tuple[str, str]] = []
for _t, _n in _TOP_500_NYSE:
    if _t not in _SEEN:
        _SEEN.add(_t)
        _TOP_500_NYSE_DEDUPED.append((_t, _n))


def _hardcoded_nyse_tickers() -> list[dict]:
    """
    Absolute last resort: return a curated list of well-known NYSE-listed stocks.

    This never raises, requires no network access, and ensures the pipeline
    always has a viable ticker universe. Approximately 480 stocks across all
    major sectors. Used only when all live sources AND the stale cache fail.
    """
    print(
        f"  ⚠ Using hardcoded fallback list ({len(_TOP_500_NYSE_DEDUPED)} tickers). "
        "All live sources and cache were unavailable.",
        flush=True,
    )
    return [
        {"ticker": t, "name": n, "exchange": "NYSE", "cik": ""}
        for t, n in _TOP_500_NYSE_DEDUPED
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_nyse_tickers(use_cache: bool = True) -> list[dict]:
    """
    Return the current list of NYSE-listed stocks.

    Source priority:
      1. .tmp/nyse_tickers.csv           — valid cache (< 24 h), use_cache=True
      2. SEC EDGAR                        — company_tickers_exchange.json
      3. Wikipedia S&P 500               — ~350 NYSE stocks via requests + pandas
      4. NASDAQ Trader nasdaqtraded.txt  — full US listing, filter exchange='N'
      5. Stale .tmp/nyse_tickers.csv     — any-age cache when all live sources fail
      6. Hardcoded top-480 NYSE list     — no network, no cache; never fails

    Args:
        use_cache: Return cached data if it exists and is < 24 h old (default True).

    Returns:
        list of dicts: [{ticker, name, exchange, cik}, ...]
    """
    # ── 1. Valid cache ──────────────────────────────────────────────────────
    if use_cache and _is_cache_valid():
        print("Loading NYSE tickers from cache (.tmp/nyse_tickers.csv)...", flush=True)
        tickers = _load_cache()
        print(f"  ✓ {len(tickers)} tickers from cache", flush=True)
        return tickers

    tickers: list[dict] = []

    # ── 2. SEC EDGAR ────────────────────────────────────────────────────────
    try:
        tickers = _fetch_sec_edgar()
    except Exception as e:
        print(f"  ✗ SEC EDGAR failed: {e}", flush=True)

    # ── 3. Wikipedia ────────────────────────────────────────────────────────
    if not tickers:
        try:
            tickers = _fetch_wikipedia_fallback()
        except Exception as e:
            print(f"  ✗ Wikipedia failed: {e}", flush=True)

    # ── 4. NASDAQ Trader ────────────────────────────────────────────────────
    if not tickers:
        try:
            tickers = _fetch_nasdaq_trader()
        except Exception as e:
            print(f"  ✗ NASDAQ Trader failed: {e}", flush=True)

    # ── 5. Stale cache ──────────────────────────────────────────────────────
    if not tickers and CACHE_FILE.exists():
        print(
            "All live sources failed. Loading stale cache as fallback...",
            flush=True,
        )
        tickers = _load_cache()
        print(f"  ✓ Stale cache: {len(tickers)} tickers", flush=True)
        return tickers  # don't overwrite the stale cache with empty data

    # ── 6. Hardcoded list ───────────────────────────────────────────────────
    if not tickers:
        tickers = _hardcoded_nyse_tickers()
        # Cache the hardcoded list so subsequent runs avoid network entirely
        _save_cache(tickers)
        print(f"  Cache written → .tmp/nyse_tickers.csv", flush=True)
        return tickers

    # Write fresh cache after any successful live fetch
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

    tickers = fetch_nyse_tickers(use_cache=not args.no_cache)

    if args.list:
        for t in sorted(tickers, key=lambda x: x["ticker"]):
            print(t["ticker"])
    else:
        print(f"\nDone. {len(tickers)} NYSE tickers available.")
        print(f"Cache: {CACHE_FILE}")


if __name__ == "__main__":
    main()
