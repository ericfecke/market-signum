#!/usr/bin/env python3
"""
fetch_stock_data.py — Data layer for MARKET SIGNUM

Pulls price history, fundamentals, and technicals via yfinance.
Every persona agent (Graham, Buffett, Dalio, Lynch, Simons) consumes
the dict returned by fetch_stock_data(). Do not let agents pull their
own data — this is the single source of truth for each run.

Output contract:
  {
    "meta":          { ticker, name, sector, price, ... },
    "fundamentals":  { pe_ratio, price_to_book, roe, ... },
    "technicals":    { rsi_14, macd, bollinger_bands, ma_50, ... },
    "price_history": [ { date, open, high, low, close, volume }, ... ],
    "missing_fields": [ "fieldName", ... ]   # logged degradation
  }

Notes on yfinance field units:
  - returnOnEquity, profitMargins, revenueGrowth etc. → raw decimal (0.15 = 15%)
    We multiply by 100 so all percentage fields are stored as % values.
  - debtToEquity → returned as a raw ratio already (e.g. 0.72 means 72% D/E).
    Graham's threshold of 0.5 applies directly to this field.
  - dividendYield → raw decimal; stored as %.

Usage:
  python fetch_stock_data.py AAPL
  python fetch_stock_data.py AAPL --no-cache
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

CACHE_TTL_SECONDS = 6 * 3600  # 6 hours


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def _compute_rsi(prices: pd.Series, period: int = 14) -> float | None:
    """RSI via Wilder's smoothing. Returns None if insufficient history."""
    if len(prices) < period + 1:
        return None
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if pd.notna(val) else None


def _compute_macd(
    prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """MACD line, signal line, histogram, and crossover direction."""
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    def _r(s: pd.Series) -> float | None:
        v = s.iloc[-1]
        return round(float(v), 4) if pd.notna(v) else None

    macd_val = _r(macd_line)
    sig_val = _r(signal_line)
    return {
        "macd": macd_val,
        "signal": sig_val,
        "histogram": _r(histogram),
        "crossover": (
            "bullish" if macd_val is not None and sig_val is not None and macd_val > sig_val
            else "bearish" if macd_val is not None and sig_val is not None
            else None
        ),
    }


def _compute_bollinger(
    prices: pd.Series, period: int = 20, n_std: float = 2.0
) -> dict:
    """Bollinger Bands with normalized position (0 = lower band, 1 = upper band)."""
    if len(prices) < period:
        return {"upper": None, "middle": None, "lower": None, "position": None}
    sma = prices.rolling(period).mean()
    std = prices.rolling(period).std()
    upper = sma + n_std * std
    lower = sma - n_std * std

    up, mid, lo = float(upper.iloc[-1]), float(sma.iloc[-1]), float(lower.iloc[-1])
    band_width = up - lo
    position = (prices.iloc[-1] - lo) / band_width if band_width > 0 else 0.5

    return {
        "upper": round(up, 2) if pd.notna(up) else None,
        "middle": round(mid, 2) if pd.notna(mid) else None,
        "lower": round(lo, 2) if pd.notna(lo) else None,
        "position": round(float(position), 3),
    }


def _compute_momentum(prices: pd.Series) -> dict:
    """Percentage price change over 30, 60, and 90 day windows."""
    result = {}
    for days in [30, 60, 90]:
        if len(prices) > days:
            pct = (prices.iloc[-1] - prices.iloc[-days]) / prices.iloc[-days] * 100
            result[f"{days}d"] = round(float(pct), 2)
        else:
            result[f"{days}d"] = None
    return result


# ---------------------------------------------------------------------------
# Main fetch function
# ---------------------------------------------------------------------------

def fetch_stock_data(ticker: str, cache: bool = True) -> dict:
    """
    Fetch all data required by the MARKET SIGNUM persona agents.

    Args:
        ticker: Stock symbol (e.g. "AAPL"). Case-insensitive.
        cache:  If True, return cached .tmp/<TICKER>.json when < 6h old.

    Returns:
        Standardized data dict, or {"error": "message"} on hard failure.
    """
    ticker = ticker.upper().strip()
    cache_path = TMP_DIR / f"{ticker}.json"

    if cache and cache_path.exists():
        age = datetime.now().timestamp() - cache_path.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            with open(cache_path) as f:
                return json.load(f)

    # ---- Init yfinance ----
    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}
    except Exception as e:
        return {"error": f"yfinance failed to initialize '{ticker}': {e}"}

    if not info or info.get("trailingPegRatio") is None and info.get("symbol") is None:
        # yfinance silently returns empty dicts for invalid tickers
        pass  # continue; missing_fields will catch the gaps

    # ---- Price history ----
    try:
        hist = stock.history(period="1y")
        if hist.empty:
            return {"error": f"No price history for '{ticker}'. Verify the symbol."}
    except Exception as e:
        return {"error": f"Failed to fetch price history for '{ticker}': {e}"}

    prices = hist["Close"]
    volume = hist["Volume"]
    current_price = float(prices.iloc[-1])

    price_history = [
        {
            "date": str(idx.date()),
            "open": round(float(row["Open"]), 4),
            "high": round(float(row["High"]), 4),
            "low": round(float(row["Low"]), 4),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]),
        }
        for idx, row in hist.iterrows()
    ]

    # ---- Field extraction helper ----
    missing: list[str] = []

    def _get(key: str, transform=None, fallback=None):
        """Pull a field from yfinance info, log if absent."""
        val = info.get(key)
        if val is None or val == "None" or val == "":
            missing.append(key)
            return fallback
        try:
            return transform(val) if transform else val
        except Exception:
            missing.append(f"{key} (transform error)")
            return fallback

    pct = lambda x: round(float(x) * 100, 2)   # decimal → percentage
    f2  = lambda x: round(float(x), 2)
    f3  = lambda x: round(float(x), 3)
    f4  = lambda x: round(float(x), 4)

    # ---- Fundamentals ----
    fundamentals = {
        # --- Valuation ---
        "pe_ratio":             _get("trailingPE", f2),
        "forward_pe":           _get("forwardPE", f2),
        "price_to_book":        _get("priceToBook", f2),
        "price_to_sales":       _get("priceToSalesTrailing12Months", f2),
        "peg_ratio":            _get("pegRatio", f2),
        "enterprise_to_ebitda": _get("enterpriseToEbitda", f2),

        # --- Quality ---
        # Units: stored as % (e.g. 18.5 means 18.5% ROE)
        "return_on_equity":     _get("returnOnEquity", pct),
        "return_on_assets":     _get("returnOnAssets", pct),
        "profit_margin":        _get("profitMargins", pct),
        "operating_margin":     _get("operatingMargins", pct),
        "gross_margin":         _get("grossMargins", pct),

        # --- Balance sheet ---
        # debtToEquity from yfinance is already a ratio (not percent)
        "debt_to_equity":       _get("debtToEquity", f3),
        "current_ratio":        _get("currentRatio", f2),
        "quick_ratio":          _get("quickRatio", f2),
        "total_cash":           _get("totalCash"),
        "total_debt":           _get("totalDebt"),

        # --- Cash flow ---
        "free_cash_flow":       _get("freeCashflow"),
        "operating_cash_flow":  _get("operatingCashflow"),

        # --- Growth (%) ---
        "earnings_growth":            _get("earningsGrowth", pct),
        "revenue_growth":             _get("revenueGrowth", pct),
        "earnings_quarterly_growth":  _get("earningsQuarterlyGrowth", pct),

        # --- Size ---
        "market_cap":           _get("marketCap"),
        "enterprise_value":     _get("enterpriseValue"),
        "shares_outstanding":   _get("sharesOutstanding"),
        "float_shares":         _get("floatShares"),

        # --- Ownership ---
        # institutionPercent / insiderPercent → raw decimal, convert to %
        "institutional_ownership": _get("institutionPercent", pct),
        "insider_ownership":        _get("insiderPercent", pct),

        # --- Dividends ---
        "dividend_yield": _get("dividendYield", pct),
        "payout_ratio":   _get("payoutRatio", pct),

        # --- EPS ---
        "eps_trailing": _get("trailingEps", f4),
        "eps_forward":  _get("forwardEps", f4),

        # --- Analyst consensus ---
        "target_mean_price":        _get("targetMeanPrice", f2),
        "target_high_price":        _get("targetHighPrice", f2),
        "target_low_price":         _get("targetLowPrice", f2),
        "analyst_recommendation":   _get("recommendationKey"),
        "analyst_count":            _get("numberOfAnalystOpinions"),
    }

    # ---- Technicals ----
    ma_50  = float(prices.rolling(50).mean().iloc[-1])  if len(prices) >= 50  else None
    ma_200 = float(prices.rolling(200).mean().iloc[-1]) if len(prices) >= 200 else None

    if ma_50  is None: missing.append("ma_50 (< 50 days of history)")
    if ma_200 is None: missing.append("ma_200 (< 200 days of history)")

    avg_vol_20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else None
    volume_ratio = (
        round(float(volume.iloc[-1]) / avg_vol_20, 2)
        if avg_vol_20 and avg_vol_20 > 0 else None
    )

    high_52w = float(hist["High"].tail(252).max()) if len(hist) >= 252 else float(hist["High"].max())
    low_52w  = float(hist["Low"].tail(252).min())  if len(hist) >= 252 else float(hist["Low"].min())

    technicals = {
        "rsi_14":         _compute_rsi(prices),
        "macd":           _compute_macd(prices),
        "bollinger_bands": _compute_bollinger(prices),
        "momentum":       _compute_momentum(prices),

        "ma_50":          round(ma_50, 2)  if ma_50  else None,
        "ma_200":         round(ma_200, 2) if ma_200 else None,

        # % above/below moving average (positive = price above MA)
        "price_vs_ma50":  round((current_price - ma_50)  / ma_50  * 100, 2) if ma_50  else None,
        "price_vs_ma200": round((current_price - ma_200) / ma_200 * 100, 2) if ma_200 else None,

        # "golden" = MA50 above MA200 (bullish), "death" = MA50 below MA200 (bearish)
        "ma_cross": (
            "golden" if ma_50 and ma_200 and ma_50 > ma_200
            else "death" if ma_50 and ma_200 and ma_50 < ma_200
            else None
        ),

        "volume_ratio_20d": volume_ratio,  # current session vol / 20-day avg vol
        "52w_high":          round(high_52w, 2),
        "52w_low":           round(low_52w, 2),
        "52w_pct_from_high": round((current_price - high_52w) / high_52w * 100, 2),
        "52w_pct_from_low":  round((current_price - low_52w)  / low_52w  * 100, 2),
    }

    # ---- Assemble output ----
    prev_close = info.get("previousClose")
    result = {
        "meta": {
            "ticker":        ticker,
            "name":          info.get("longName") or info.get("shortName") or ticker,
            "sector":        info.get("sector", "Unknown"),
            "industry":      info.get("industry", "Unknown"),
            "country":       info.get("country", "Unknown"),
            "exchange":      info.get("exchange", "Unknown"),
            "currency":      info.get("currency", "USD"),
            "price":         round(current_price, 4),
            "previous_close": round(float(prev_close), 2) if prev_close else None,
            "day_change_pct": (
                round((current_price - float(prev_close)) / float(prev_close) * 100, 2)
                if prev_close else None
            ),
            "fetched_at": datetime.now().isoformat(),
        },
        "fundamentals":  fundamentals,
        "technicals":    technicals,
        "price_history": price_history,
        "missing_fields": missing,
    }

    with open(cache_path, "w") as f:
        json.dump(result, f, indent=2, default=str)

    return result


# ---------------------------------------------------------------------------
# CLI summary output
# ---------------------------------------------------------------------------

def _fmt(val, suffix="") -> str:
    return f"{val}{suffix}" if val is not None else "N/A"


def main():
    if len(sys.argv) < 2:
        print("Usage: python fetch_stock_data.py <TICKER> [--no-cache]")
        sys.exit(1)

    ticker = sys.argv[1]
    cache = "--no-cache" not in sys.argv

    print(f"\nFetching data for {ticker.upper()}...")
    data = fetch_stock_data(ticker, cache=cache)

    if "error" in data:
        print(f"\nERROR: {data['error']}")
        sys.exit(1)

    m = data["meta"]
    f = data["fundamentals"]
    t = data["technicals"]
    missing = data["missing_fields"]

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  {m['ticker']}  —  {m['name']}")
    print(f"  {m['sector']}  |  {m['industry']}")
    print(f"  ${m['price']}  ({_fmt(m['day_change_pct'], '%')})  |  {m['exchange']}  |  {m['currency']}")
    print(f"  Fetched: {m['fetched_at'][:19]}")
    print(bar)

    print(f"\n  FUNDAMENTALS")
    print(f"  P/E: {_fmt(f['pe_ratio'])}  |  P/B: {_fmt(f['price_to_book'])}  |  PEG: {_fmt(f['peg_ratio'])}")
    print(f"  ROE: {_fmt(f['return_on_equity'], '%')}  |  Net Margin: {_fmt(f['profit_margin'], '%')}")
    print(f"  D/E: {_fmt(f['debt_to_equity'])}  |  Current Ratio: {_fmt(f['current_ratio'])}")
    print(f"  Rev Growth: {_fmt(f['revenue_growth'], '%')}  |  EPS Growth: {_fmt(f['earnings_growth'], '%')}")
    print(f"  FCF: {_fmt(f['free_cash_flow'])}  |  Mkt Cap: {_fmt(f['market_cap'])}")
    print(f"  Institutional: {_fmt(f['institutional_ownership'], '%')}  |  Insider: {_fmt(f['insider_ownership'], '%')}")

    macd = t["macd"]
    bb   = t["bollinger_bands"]
    mom  = t["momentum"]
    print(f"\n  TECHNICALS")
    print(f"  RSI(14): {_fmt(t['rsi_14'])}  |  MACD: {_fmt(macd.get('crossover'))}  ({_fmt(macd.get('histogram'))})")
    print(f"  MA50: ${_fmt(t['ma_50'])}  |  MA200: ${_fmt(t['ma_200'])}  |  Cross: {_fmt(t['ma_cross'])}")
    print(f"  BB Position: {_fmt(bb.get('position'))}  (0=lower, 1=upper)")
    print(f"  Vol Ratio 20d: {_fmt(t['volume_ratio_20d'])}x")
    print(f"  52W  High: ${_fmt(t['52w_high'])}  ({_fmt(t['52w_pct_from_high'], '%')})  "
          f"Low: ${_fmt(t['52w_low'])}  (+{_fmt(t['52w_pct_from_low'], '%')})")
    print(f"  Momentum  30d: {_fmt(mom.get('30d'), '%')}  "
          f"60d: {_fmt(mom.get('60d'), '%')}  90d: {_fmt(mom.get('90d'), '%')}")

    if missing:
        print(f"\n  MISSING FIELDS ({len(missing)})")
        for field in missing:
            print(f"    - {field}")

    print(f"\n  Cached → .tmp/{m['ticker']}.json")
    print(f"{bar}\n")


if __name__ == "__main__":
    main()
