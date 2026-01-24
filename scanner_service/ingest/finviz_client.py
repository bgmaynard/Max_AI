"""Finviz screener client for top gainers with float data."""

import logging
from typing import Optional
from datetime import datetime, timedelta
import asyncio

logger = logging.getLogger(__name__)

# Cache for Finviz data (refreshes every 2 minutes)
_finviz_cache: dict = {}
_cache_time: Optional[datetime] = None
CACHE_TTL_SECONDS = 120


async def get_top_gainers(
    max_price: float = 20.0,
    min_change: float = 0.0,
    max_float_millions: Optional[float] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Fetch top gainers from Finviz with float data.

    Args:
        max_price: Maximum stock price (default $20)
        min_change: Minimum % change (default 0)
        max_float_millions: Maximum float in millions (optional)
        limit: Max results to return

    Returns:
        List of dicts with ticker, price, change, volume, float, etc.
    """
    global _finviz_cache, _cache_time

    # Check cache
    cache_key = f"gainers_{max_price}"
    if (
        _cache_time
        and (datetime.utcnow() - _cache_time).total_seconds() < CACHE_TTL_SECONDS
        and cache_key in _finviz_cache
    ):
        results = _finviz_cache[cache_key]
    else:
        # Fetch fresh data (run in thread to avoid blocking)
        results = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_finviz_gainers, max_price
        )
        _finviz_cache[cache_key] = results
        _cache_time = datetime.utcnow()

    # Apply additional filters
    filtered = []
    for row in results:
        # Change filter
        if row.get('change_pct', 0) < min_change:
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
        from finvizfinance.screener.ownership import Ownership

        # Map price to Finviz filter format
        if max_price <= 5:
            price_filter = 'Under $5'
        elif max_price <= 10:
            price_filter = 'Under $10'
        elif max_price <= 20:
            price_filter = 'Under $20'
        elif max_price <= 50:
            price_filter = 'Under $50'
        else:
            price_filter = None

        fown = Ownership()
        filters = {}
        if price_filter:
            filters['Price'] = price_filter

        fown.set_filter(signal='Top Gainers', filters_dict=filters)
        df = fown.screener_view()

        results = []
        for _, row in df.iterrows():
            try:
                # Parse float value (can be like "1.4M" or raw number)
                float_val = row.get('Float', 0)
                if isinstance(float_val, str):
                    float_val = _parse_number(float_val)
                float_millions = float_val / 1_000_000 if float_val else 0

                # Parse change (can be like "22.66%" or decimal)
                change = row.get('Change', 0)
                if isinstance(change, str):
                    change = float(change.replace('%', '')) / 100
                change_pct = change * 100 if abs(change) < 1 else change

                # Handle NaN values for JSON compliance
                import math
                insider_own = row.get('Insider Own', 0)
                inst_own = row.get('Inst Own', 0)
                if isinstance(insider_own, float) and math.isnan(insider_own):
                    insider_own = 0
                if isinstance(inst_own, float) and math.isnan(inst_own):
                    inst_own = 0

                results.append({
                    'symbol': row.get('Ticker', ''),
                    'price': float(row.get('Price', 0) or 0),
                    'change_pct': change_pct if not math.isnan(change_pct) else 0,
                    'volume': int(row.get('Volume', 0) or 0),
                    'float_shares': float_millions if not math.isnan(float_millions) else 0,
                    'shares_outstanding': _parse_number(row.get('Outstanding', 0)) / 1_000_000,
                    'avg_volume': int(row.get('Avg Volume', 0) or 0),
                    'market_cap': _parse_number(row.get('Market Cap', 0)) / 1_000_000,
                    'short_float': row.get('Short Float', '') or '',
                    'insider_own': insider_own or 0,
                    'inst_own': inst_own or 0,
                    'source': 'finviz',
                })
            except Exception as e:
                logger.warning(f"Error parsing Finviz row: {e}")
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
