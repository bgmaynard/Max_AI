"""Alert and event schemas."""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class AlertType(str, Enum):
    """Types of scanner alerts."""

    GAP_ALERT = "GAP_ALERT"
    MOMO_SURGE = "MOMO_SURGE"
    HOD_BREAK = "HOD_BREAK"
    NEWS = "NEWS"
    RISK = "RISK"


class AlertEvent(BaseModel):
    """Represents a triggered alert."""

    id: str = Field(description="Unique alert identifier")
    alert_type: AlertType
    symbol: str
    profile: str = Field(description="Profile that triggered the alert")
    message: str
    ai_score: float = Field(ge=0, le=1)
    price: float
    change_pct: float
    volume: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    sound_played: bool = Field(default=False)
    acknowledged: bool = Field(default=False)
    metadata: dict = Field(default_factory=dict)


class ScannerRow(BaseModel):
    """Single row in scanner output."""

    rank: int = Field(ge=1)
    symbol: str
    last_price: float
    change_pct: float
    volume: int
    rvol: float
    velocity: float = Field(description="Price velocity score")
    high: float = Field(description="High of day price")
    hod_proximity: float
    spread: float
    float_shares: float = Field(default=0, description="Float shares in millions")
    market_cap: float = Field(default=0, description="Market cap in millions")
    ai_score: float = Field(ge=0, le=1)
    profile: str
    alerts: list[str] = Field(default_factory=list, description="Active alert types")
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ScannerOutput(BaseModel):
    """Complete scanner response."""

    profile: str
    rows: list[ScannerRow]
    total_candidates: int
    scan_time_ms: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)
