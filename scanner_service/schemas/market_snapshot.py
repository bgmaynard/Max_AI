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
        """Calculate relative volume (RVOL)."""
        if self.avg_volume == 0:
            return 0.0
        return self.volume / self.avg_volume

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
