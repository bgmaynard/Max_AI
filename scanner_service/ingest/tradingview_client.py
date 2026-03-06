"""
TradingView scanner — pre-market gappers + intraday small-cap movers.

Pre-market: uses premarket_change, premarket_volume columns.
Intraday:   uses change (regular session), volume columns.

Uses tradingview-screener package (v3.0.0).
Zero deps beyond pandas + requests (already installed).
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def fetch_premarket_gappers(
    min_change_pct: float = 5.0,
    min_price: float = 1.0,
    max_price: float = 20.0,
    min_premarket_volume: int = 100_000,
    limit: int = 20,
) -> list[dict]:
    """
    Fetch pre-market gappers from TradingView screener.

    Returns list of dicts with: symbol, price, change_pct, premarket_volume, gap_pct, source.
    Returns empty list on any error (fail-open).
    """
    try:
        from tradingview_screener import Query, col

        count, df = (
            Query()
            .select(
                "name", "close", "premarket_change", "premarket_volume",
                "premarket_gap", "premarket_close", "volume", "market_cap_basic",
            )
            .where(
                col("premarket_change") > min_change_pct,
                col("close").between(min_price, max_price),
                col("premarket_volume") > min_premarket_volume,
            )
            .order_by("premarket_change", ascending=False)
            .limit(limit)
            .get_scanner_data()
        )

        if df is None or df.empty:
            logger.debug("TradingView screener returned empty")
            return []

        results = []
        for _, row in df.iterrows():
            # Strip exchange prefix (NASDAQ:PHIO → PHIO)
            ticker = str(row.get("ticker", ""))
            symbol = ticker.split(":")[-1] if ":" in ticker else ticker
            if not symbol:
                continue

            price = float(row.get("close", 0) or 0)
            change_pct = float(row.get("premarket_change", 0) or 0)
            pm_volume = int(row.get("premarket_volume", 0) or 0)
            gap_pct = float(row.get("premarket_gap", 0) or 0)

            results.append({
                "symbol": symbol,
                "price": price,
                "change_pct": round(change_pct, 2),
                "premarket_volume": pm_volume,
                "gap_pct": round(gap_pct, 2),
                "source": "tradingview",
            })

        logger.info(f"TradingView screener found {len(results)} pre-market gappers")
        return results

    except Exception as e:
        logger.warning(f"TradingView fetch failed (non-fatal): {e}")
        return []


def fetch_intraday_movers(
    min_change_pct: float = 10.0,
    min_price: float = 1.0,
    max_price: float = 20.0,
    min_volume: int = 500_000,
    limit: int = 20,
) -> list[dict]:
    """
    Fetch intraday small-cap movers from TradingView screener.

    Uses regular session change% and volume (not premarket columns).
    Designed for OPEN phase (9:30 AM - 4:00 PM ET).

    Returns list of dicts with: symbol, price, change_pct, volume, source.
    Returns empty list on any error (fail-open).
    """
    try:
        from tradingview_screener import Query, col

        count, df = (
            Query()
            .select(
                "name", "close", "change", "volume",
                "relative_volume_10d_calc", "market_cap_basic",
            )
            .where(
                col("change") > min_change_pct,
                col("close").between(min_price, max_price),
                col("volume") > min_volume,
            )
            .order_by("change", ascending=False)
            .limit(limit)
            .get_scanner_data()
        )

        if df is None or df.empty:
            logger.debug("TradingView intraday screener returned empty")
            return []

        results = []
        for _, row in df.iterrows():
            ticker = str(row.get("ticker", ""))
            symbol = ticker.split(":")[-1] if ":" in ticker else ticker
            if not symbol:
                continue

            price = float(row.get("close", 0) or 0)
            change_pct = float(row.get("change", 0) or 0)
            volume = int(row.get("volume", 0) or 0)
            rvol = float(row.get("relative_volume_10d_calc", 0) or 0)

            results.append({
                "symbol": symbol,
                "price": price,
                "change_pct": round(change_pct, 2),
                "volume": volume,
                "rvol": round(rvol, 2),
                "source": "tradingview_intraday",
            })

        logger.info(f"TradingView intraday screener found {len(results)} small-cap movers")
        return results

    except Exception as e:
        logger.warning(f"TradingView intraday fetch failed (non-fatal): {e}")
        return []
