"""Finviz screener client for top gainers with float data."""

import logging
from typing import Optional
from datetime import datetime, timedelta
import asyncio

logger = logging.getLogger(__name__)

# Cache for Finviz data (refreshes every 30 seconds for more real-time data)
_finviz_cache: dict = {}
_cache_time: Optional[datetime] = None
CACHE_TTL_SECONDS = 30


async def get_top_gainers(
    max_price: float = 100.0,
    min_change: float = 0.0,
    max_float_millions: Optional[float] = None,
    limit: int = 500,
) -> list[dict]:
    """
    Fetch top gainers from Finviz.

    Args:
        max_price: Maximum stock price filter (applied client-side)
        min_change: Minimum % change (applied client-side)
        max_float_millions: Maximum float in millions (applied client-side)
        limit: Max results to return

    Returns:
        List of dicts with ticker, price, change, volume, etc.
    """
    global _finviz_cache, _cache_time

    # Check cache - single cache for all top gainers
    cache_key = "all_gainers"
    if (
        _cache_time
        and (datetime.utcnow() - _cache_time).total_seconds() < CACHE_TTL_SECONDS
        and cache_key in _finviz_cache
    ):
        results = _finviz_cache[cache_key]
        logger.info(f"Using cached Finviz data: {len(results)} gainers")
    else:
        # Fetch fresh data (run in thread to avoid blocking)
        results = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_finviz_gainers, 100.0  # Get all price ranges
        )
        _finviz_cache[cache_key] = results
        _cache_time = datetime.utcnow()

    # Apply optional filters (but return all by default)
    filtered = []
    for row in results:
        # Price filter
        if max_price and row.get('price', 0) > max_price:
            continue

        # Change filter
        if min_change and row.get('change_pct', 0) < min_change:
            continue

        # Float filter
        if max_float_millions and row.get('float_shares', 0) > max_float_millions:
            continue

        filtered.append(row)

        if len(filtered) >= limit:
            break

    return filtered


def _fetch_finviz_gainers(max_price: float) -> list[dict]:
    """Synchronous fetch from Finviz (runs in thread)."""
    try:
        from finvizfinance.screener.overview import Overview

        # Use Overview screener first to get all top gainers
        foverview = Overview()
        foverview.set_filter(signal='Top Gainers')

        # Get up to 500 rows (default is 20)
        df = foverview.screener_view(order='Change', limit=500, ascend=False)

        if df is None or df.empty:
            logger.warning("Finviz returned empty dataframe")
            return []

        logger.info(f"Finviz Overview returned {len(df)} rows")

        import math

        results = []
        for _, row in df.iterrows():
            try:
                # Parse change (can be like "22.66%" or decimal)
                change = row.get('Change', 0)
                if isinstance(change, str):
                    change = float(change.replace('%', '').replace(',', '')) / 100
                change_pct = change * 100 if abs(change) < 1 else change

                # Parse price
                price = row.get('Price', 0)
                if isinstance(price, str):
                    price = float(price.replace(',', ''))
                price = float(price or 0)

                # Parse volume
                volume = row.get('Volume', 0)
                if isinstance(volume, str):
                    volume = _parse_number(volume)
                volume = int(volume or 0)

                # Parse market cap
                market_cap = _parse_number(row.get('Market Cap', 0)) / 1_000_000

                # Handle NaN
                if math.isnan(change_pct):
                    change_pct = 0
                if math.isnan(price):
                    price = 0

                ticker = row.get('Ticker', '')
                if not ticker:
                    continue

                results.append({
                    'symbol': ticker,
                    'price': price,
                    'change_pct': change_pct,
                    'volume': volume,
                    'float_shares': 0,  # Will be enriched later if needed
                    'shares_outstanding': 0,
                    'avg_volume': 0,
                    'market_cap': market_cap,
                    'short_float': '',
                    'insider_own': 0,
                    'inst_own': 0,
                    'source': 'finviz',
                    'company': row.get('Company', ''),
                    'sector': row.get('Sector', ''),
                })
            except Exception as e:
                logger.warning(f"Error parsing Finviz row {row.get('Ticker', '?')}: {e}")
                continue

        logger.info(f"Fetched {len(results)} top gainers from Finviz")
        return results

    except Exception as e:
        logger.error(f"Finviz fetch error: {e}")
        return []


def _parse_number(val) -> float:
    """Parse number that might have K/M/B suffix."""
    if isinstance(val, (int, float)):
        return float(val)
    if not val or val == '-':
        return 0.0

    val = str(val).strip().replace(',', '')
    multiplier = 1

    if val.endswith('K'):
        multiplier = 1_000
        val = val[:-1]
    elif val.endswith('M'):
        multiplier = 1_000_000
        val = val[:-1]
    elif val.endswith('B'):
        multiplier = 1_000_000_000
        val = val[:-1]

    try:
        return float(val) * multiplier
    except:
        return 0.0


async def get_finviz_quote(symbol: str) -> Optional[dict]:
    """Get individual stock data from Finviz."""
    try:
        from finvizfinance.quote import finvizfinance

        stock = await asyncio.get_event_loop().run_in_executor(
            None, lambda: finvizfinance(symbol)
        )
        fundament = stock.ticker_fundament()

        return {
            'symbol': symbol,
            'float_shares': _parse_number(fundament.get('Shs Float', 0)) / 1_000_000,
            'shares_outstanding': _parse_number(fundament.get('Shs Outstand', 0)) / 1_000_000,
            'market_cap': _parse_number(fundament.get('Market Cap', 0)) / 1_000_000,
            'avg_volume': _parse_number(fundament.get('Avg Volume', 0)),
            'short_float': fundament.get('Short Float', ''),
            'source': 'finviz',
        }
    except Exception as e:
        logger.warning(f"Finviz quote error for {symbol}: {e}")
        return None
