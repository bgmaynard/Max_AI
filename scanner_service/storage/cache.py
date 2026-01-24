"""Quote and data caching."""

import logging
from datetime import datetime, timedelta
from typing import Optional, TypeVar, Generic
from collections import OrderedDict

from scanner_service.schemas.market_snapshot import Quote

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Generic TTL (time-to-live) cache."""

    def __init__(self, ttl_seconds: float = 60, max_size: int = 1000):
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[T, datetime]] = OrderedDict()

    def get(self, key: str) -> Optional[T]:
        """Get value if exists and not expired."""
        if key not in self._data:
            return None

        value, timestamp = self._data[key]
        if datetime.utcnow() - timestamp > self._ttl:
            del self._data[key]
            return None

        # Move to end (LRU)
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: T) -> None:
        """Set value with current timestamp."""
        self._data[key] = (value, datetime.utcnow())
        self._data.move_to_end(key)

        # Evict oldest if over capacity
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def delete(self, key: str) -> bool:
        """Delete a key."""
        if key in self._data:
            del self._data[key]
            return True
        return False

    def clear(self) -> None:
        """Clear all cached data."""
        self._data.clear()

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = datetime.utcnow()
        expired = [
            key for key, (_, ts) in self._data.items()
            if now - ts > self._ttl
        ]
        for key in expired:
            del self._data[key]
        return len(expired)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None


class QuoteCache:
    """
    Caching layer for quote data.

    Reduces API calls by caching recent quotes with configurable TTL.
    """

    def __init__(self, ttl_seconds: float = 5.0, max_symbols: int = 500):
        """
        Initialize quote cache.

        Args:
            ttl_seconds: How long quotes remain valid (default 5s)
            max_symbols: Maximum symbols to cache
        """
        self._cache = TTLCache[Quote](ttl_seconds=ttl_seconds, max_size=max_symbols)
        self._hit_count = 0
        self._miss_count = 0

    def get(self, symbol: str) -> Optional[Quote]:
        """Get cached quote for symbol."""
        quote = self._cache.get(symbol)
        if quote:
            self._hit_count += 1
        else:
            self._miss_count += 1
        return quote

    def get_many(self, symbols: list[str]) -> tuple[dict[str, Quote], list[str]]:
        """
        Get cached quotes for multiple symbols.

        Returns:
            Tuple of (cached quotes dict, list of symbols not in cache)
        """
        cached = {}
        missing = []

        for symbol in symbols:
            quote = self.get(symbol)
            if quote:
                cached[symbol] = quote
            else:
                missing.append(symbol)

        return cached, missing

    def set(self, symbol: str, quote: Quote) -> None:
        """Cache a quote."""
        self._cache.set(symbol, quote)

    def set_many(self, quotes: dict[str, Quote]) -> None:
        """Cache multiple quotes."""
        for symbol, quote in quotes.items():
            self._cache.set(symbol, quote)

    def invalidate(self, symbol: str) -> None:
        """Invalidate cached quote for symbol."""
        self._cache.delete(symbol)

    def clear(self) -> None:
        """Clear entire cache."""
        self._cache.clear()

    def cleanup(self) -> int:
        """Remove expired entries."""
        return self._cache.cleanup_expired()

    def get_stats(self) -> dict:
        """Get cache statistics."""
        total = self._hit_count + self._miss_count
        hit_rate = self._hit_count / total if total > 0 else 0

        return {
            "size": len(self._cache),
            "hits": self._hit_count,
            "misses": self._miss_count,
            "hit_rate": hit_rate,
        }

    def reset_stats(self) -> None:
        """Reset hit/miss statistics."""
        self._hit_count = 0
        self._miss_count = 0
