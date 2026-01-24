"""Market data schemas."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Real-time quote data for a single symbol."""

    symbol: str
    last_price: float = Field(ge=0)
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)
    volume: int = Field(ge=0)
    avg_volume: int = Field(ge=0, description="Average daily volume")
    high: float = Field(ge=0, description="Day high")
    low: float = Field(ge=0, description="Day low")
    open_price: float = Field(ge=0, description="Opening price")
    prev_close: float = Field(ge=0, description="Previous close")
    change: float = Field(description="Price change from prev close")
    change_pct: float = Field(description="Percent change from prev close")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def spread(self) -> float:
        """Calculate bid-ask spread."""
        if self.bid == 0:
            return 0.0
        return (self.ask - self.bid) / self.bid * 100

    @property
    def rvol(self) -> float:
        """
        Calculate relative volume (RVOL).

        If avg_volume is not available, estimate based on typical
        intraday volume distribution (more volume at open/close).
        """
        if self.avg_volume > 0:
            return self.volume / self.avg_volume

        # Estimate expected volume based on time of day
        # Typical stock trades ~65M shares/day (avg)
        # Volume distribution: ~25% in first hour, ~15% in last hour
        from datetime import datetime

        now = datetime.utcnow()
        # Convert to EST (UTC-5)
        est_hour = (now.hour - 5) % 24
        est_minute = now.minute

        # Market hours 9:30 AM - 4:00 PM EST
        if est_hour < 9 or (est_hour == 9 and est_minute < 30) or est_hour >= 16:
            # Pre/post market - use lower baseline
            return 0.0

        # Minutes since market open
        minutes_open = (est_hour - 9) * 60 + est_minute - 30
        total_minutes = 390  # 6.5 hours

        # Expected cumulative % based on typical U-shaped volume curve
        # Approximation: faster accumulation at open and close
        if minutes_open <= 60:  # First hour
            expected_pct = 0.25 * (minutes_open / 60)
        elif minutes_open >= 330:  # Last hour
            expected_pct = 0.75 + 0.25 * ((minutes_open - 330) / 60)
        else:  # Middle of day
            expected_pct = 0.25 + 0.50 * ((minutes_open - 60) / 270)

        # Use the stock's own volume pattern - compare to what we'd expect
        # Assume typical stock trades about 5M shares/day for reference
        # RVOL = actual volume / expected volume at this time
        # Since we don't have avg, use 5M as baseline and scale by price
        baseline_volume = 5_000_000
        if self.last_price > 100:
            baseline_volume = 2_000_000  # Lower volume for expensive stocks
        elif self.last_price < 10:
            baseline_volume = 20_000_000  # Higher volume for cheap stocks

        expected_volume = baseline_volume * expected_pct
        if expected_volume == 0:
            return 0.0

        return self.volume / expected_volume

    @property
    def hod_proximity(self) -> float:
        """Calculate proximity to high of day (0-1, 1 = at HOD)."""
        if self.high == self.low:
            return 1.0
        return (self.last_price - self.low) / (self.high - self.low)

    @property
    def gap_pct(self) -> float:
        """Calculate gap percentage from previous close to open."""
        if self.prev_close == 0:
            return 0.0
        return (self.open_price - self.prev_close) / self.prev_close * 100


class MarketSnapshot(BaseModel):
    """Collection of quotes at a point in time."""

    quotes: dict[str, Quote] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    scan_duration_ms: Optional[float] = None

    def get_quote(self, symbol: str) -> Optional[Quote]:
        """Get quote for a specific symbol."""
        return self.quotes.get(symbol)

    def symbols(self) -> list[str]:
        """Get list of all symbols in snapshot."""
        return list(self.quotes.keys())

    def __len__(self) -> int:
        return len(self.quotes)
