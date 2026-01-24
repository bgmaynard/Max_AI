"""Trading halt tracker - monitors NASDAQ halt feed."""

import logging
from typing import Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import asyncio
import re

logger = logging.getLogger(__name__)

# Store active and recent halts
_halts: dict[str, 'HaltInfo'] = {}
_halt_history: list['HaltInfo'] = []
_last_fetch: Optional[datetime] = None
FETCH_INTERVAL_SECONDS = 15  # Check for halts every 15 seconds


@dataclass
class HaltInfo:
    """Information about a trading halt."""
    symbol: str
    halt_time: datetime
    halt_price: float = 0.0
    halt_reason: str = ""
    resume_time: Optional[datetime] = None
    resume_price: float = 0.0
    status: str = "HALTED"  # HALTED, RESUMED, PENDING
    exchange: str = ""

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'halt_time': self.halt_time.isoformat() if self.halt_time else None,
            'halt_price': self.halt_price,
            'halt_reason': self.halt_reason,
            'resume_time': self.resume_time.isoformat() if self.resume_time else None,
            'resume_price': self.resume_price,
            'status': self.status,
            'exchange': self.exchange,
            'halt_duration_minutes': self._get_duration_minutes(),
        }

    def _get_duration_minutes(self) -> float:
        if not self.halt_time:
            return 0
        end_time = self.resume_time or datetime.now()
        return (end_time - self.halt_time).total_seconds() / 60


async def fetch_halts() -> list[dict]:
    """Fetch current halts from NASDAQ."""
    global _last_fetch, _halts

    now = datetime.now()

    # Rate limit fetching
    if _last_fetch and (now - _last_fetch).total_seconds() < FETCH_INTERVAL_SECONDS:
        return get_all_halts()

    _last_fetch = now

    try:
        # Fetch from NASDAQ trade halts page
        halts_data = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_nasdaq_halts
        )

        # Process the halt data
        for halt in halts_data:
            symbol = halt.get('symbol', '').upper()
            if not symbol:
                continue

            halt_time = _parse_halt_time(halt.get('halt_time', ''))
            resume_time = _parse_halt_time(halt.get('resume_time', ''))

            if symbol in _halts:
                # Update existing halt
                existing = _halts[symbol]
                if resume_time and not existing.resume_time:
                    existing.resume_time = resume_time
                    existing.resume_price = halt.get('resume_price', 0)
                    existing.status = "RESUMED"
                    _halt_history.append(existing)
                    logger.info(f"Halt resumed: {symbol} at {resume_time}")
            else:
                # New halt
                halt_info = HaltInfo(
                    symbol=symbol,
                    halt_time=halt_time or now,
                    halt_price=halt.get('halt_price', 0),
                    halt_reason=halt.get('reason', ''),
                    resume_time=resume_time,
                    resume_price=halt.get('resume_price', 0),
                    status="RESUMED" if resume_time else "HALTED",
                    exchange=halt.get('exchange', ''),
                )
                _halts[symbol] = halt_info

                if resume_time:
                    _halt_history.append(halt_info)

                logger.info(f"New halt detected: {symbol} - {halt_info.halt_reason}")

        # Clean up old halts (older than 24 hours)
        cutoff = now - timedelta(hours=24)
        _halts_to_remove = [
            sym for sym, h in _halts.items()
            if h.halt_time and h.halt_time < cutoff
        ]
        for sym in _halts_to_remove:
            del _halts[sym]

        return get_all_halts()

    except Exception as e:
        logger.error(f"Error fetching halts: {e}")
        return get_all_halts()


def _fetch_nasdaq_halts() -> list[dict]:
    """Synchronous fetch from NASDAQ trade halts."""
    import urllib.request
    import json

    halts = []

    try:
        # NASDAQ Trade Halts RSS/JSON feed
        url = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8')

        # Parse RSS feed for halt information
        # The NASDAQ RSS feed contains items like:
        # <item><title>Trading Halt - SYMBOL</title><description>...</description></item>

        import xml.etree.ElementTree as ET
        root = ET.fromstring(content)

        for item in root.findall('.//item'):
            title = item.find('title')
            desc = item.find('description')
            pub_date = item.find('pubDate')

            if title is not None and title.text:
                # Parse the title for symbol and action
                title_text = title.text.strip()

                # Extract symbol from title (e.g., "Trading Halt - AAPL" or "Trading Resumption - AAPL")
                symbol_match = re.search(r'(?:Halt|Resumption)\s*[-:]\s*(\w+)', title_text, re.I)
                if symbol_match:
                    symbol = symbol_match.group(1).upper()
                    is_resumption = 'resumption' in title_text.lower()

                    halt_info = {
                        'symbol': symbol,
                        'halt_time': pub_date.text if pub_date is not None else '',
                        'reason': desc.text if desc is not None else '',
                        'exchange': 'NASDAQ',
                    }

                    if is_resumption:
                        halt_info['resume_time'] = pub_date.text if pub_date is not None else ''

                    halts.append(halt_info)

        logger.info(f"Fetched {len(halts)} halt entries from NASDAQ")

    except Exception as e:
        logger.warning(f"Error fetching NASDAQ halts: {e}")
        # Try alternative source - SEC halt feed
        halts = _fetch_sec_halts()

    return halts


def _fetch_sec_halts() -> list[dict]:
    """Alternative: Fetch from SEC/FINRA halt data."""
    import urllib.request

    halts = []

    try:
        # Try the NYSE halt page
        url = "https://www.nyse.com/trade-halt-current"

        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        # Note: This may require JavaScript rendering, so we'll use a simpler approach
        # For now, return empty and rely on NASDAQ feed

    except Exception as e:
        logger.warning(f"Error fetching SEC halts: {e}")

    return halts


def _parse_halt_time(time_str: str) -> Optional[datetime]:
    """Parse halt time string to datetime."""
    if not time_str:
        return None

    try:
        # Try various formats
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",  # RSS format
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
            "%H:%M:%S",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(time_str.strip(), fmt)
                # If only time, add today's date
                if dt.year == 1900:
                    today = datetime.now()
                    dt = dt.replace(year=today.year, month=today.month, day=today.day)
                return dt
            except ValueError:
                continue

        return None
    except Exception:
        return None


def get_all_halts() -> list[dict]:
    """Get all current and recent halts."""
    all_halts = []

    # Current halts (still halted)
    for symbol, halt in _halts.items():
        if halt.status == "HALTED":
            all_halts.append(halt.to_dict())

    # Recent resumptions (last 2 hours)
    cutoff = datetime.now() - timedelta(hours=2)
    for halt in _halt_history[-50:]:  # Last 50 resumptions
        if halt.resume_time and halt.resume_time > cutoff:
            all_halts.append(halt.to_dict())

    # Sort by halt time descending (most recent first)
    all_halts.sort(key=lambda x: x.get('halt_time', ''), reverse=True)

    return all_halts


def get_active_halts() -> list[dict]:
    """Get only currently halted stocks."""
    return [h.to_dict() for h in _halts.values() if h.status == "HALTED"]


def get_resumed_halts(hours: int = 2) -> list[dict]:
    """Get recently resumed halts."""
    cutoff = datetime.now() - timedelta(hours=hours)
    resumed = []

    for halt in _halt_history:
        if halt.resume_time and halt.resume_time > cutoff:
            resumed.append(halt.to_dict())

    # Also check current halts that have resumed
    for halt in _halts.values():
        if halt.status == "RESUMED" and halt.resume_time and halt.resume_time > cutoff:
            resumed.append(halt.to_dict())

    resumed.sort(key=lambda x: x.get('resume_time', ''), reverse=True)
    return resumed


async def add_manual_halt(
    symbol: str,
    halt_price: float,
    halt_reason: str = "Manual Entry"
) -> dict:
    """Manually add a halt (for testing or manual tracking)."""
    global _halts

    symbol = symbol.upper()
    halt_info = HaltInfo(
        symbol=symbol,
        halt_time=datetime.now(),
        halt_price=halt_price,
        halt_reason=halt_reason,
        status="HALTED",
    )
    _halts[symbol] = halt_info
    logger.info(f"Manual halt added: {symbol} at ${halt_price}")
    return halt_info.to_dict()


async def update_halt_resume(
    symbol: str,
    resume_price: float
) -> Optional[dict]:
    """Update a halt with resume information."""
    global _halts, _halt_history

    symbol = symbol.upper()
    if symbol not in _halts:
        return None

    halt = _halts[symbol]
    halt.resume_time = datetime.now()
    halt.resume_price = resume_price
    halt.status = "RESUMED"
    _halt_history.append(halt)

    logger.info(f"Halt resumed: {symbol} at ${resume_price}")
    return halt.to_dict()
