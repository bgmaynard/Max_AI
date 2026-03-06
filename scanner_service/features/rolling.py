"""Rolling state management for time-series features."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import statistics


@dataclass
class PricePoint:
    """Single price observation."""
    price: float
    volume: int
    timestamp: datetime


@dataclass
class SymbolRollingState:
    """Rolling state for a single symbol."""

    symbol: str
    window_size: int = 20  # Number of observations to keep
    prices: deque = field(default_factory=lambda: deque(maxlen=20))
    volumes: deque = field(default_factory=lambda: deque(maxlen=20))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=20))

    # Tracked values
    prev_price: Optional[float] = None
    prev_hod: Optional[float] = None
    hod_break_count: int = 0
    last_update: Optional[datetime] = None

    # Acceleration tracking
    prev_velocity: float = 0.0
    velocity_accel: float = 0.0  # velocity delta between scans
    prev_change_pct: float = 0.0
    change_accel: float = 0.0  # change_pct delta between scans
    prev_rvol: float = 0.0
    rvol_crossed_up: bool = False  # True when rvol crosses 2.0 from below
    momentum_history: deque = field(default_factory=lambda: deque(maxlen=5))
    momentum_slope: float = 0.0  # linear slope of last 5 momentum scores

    def update(self, price: float, volume: int, hod: float) -> None:
        """Update rolling state with new observation."""
        now = datetime.utcnow()

        self.prices.append(price)
        self.volumes.append(volume)
        self.timestamps.append(now)

        # Track HOD breaks
        if self.prev_hod is not None and hod > self.prev_hod:
            self.hod_break_count += 1

        self.prev_price = price
        self.prev_hod = hod
        self.last_update = now

        # Update acceleration features after prices are appended
        current_vel = self.velocity()
        self.velocity_accel = current_vel - self.prev_velocity
        self.prev_velocity = current_vel

        current_mom = self.momentum_score()
        self.momentum_history.append(current_mom)
        if len(self.momentum_history) >= 3:
            vals = list(self.momentum_history)
            n = len(vals)
            x_mean = (n - 1) / 2
            y_mean = sum(vals) / n
            num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
            den = sum((i - x_mean) ** 2 for i in range(n))
            self.momentum_slope = num / den if den > 0 else 0.0
        else:
            self.momentum_slope = 0.0

    def update_quote_features(self, change_pct: float, rvol: float) -> None:
        """Update acceleration features from quote data (called after update)."""
        self.change_accel = change_pct - self.prev_change_pct
        self.prev_change_pct = change_pct

        self.rvol_crossed_up = (self.prev_rvol < 2.0 and rvol >= 2.0)
        self.prev_rvol = rvol

    def velocity(self) -> float:
        """
        Calculate price velocity (rate of change).

        Returns normalized velocity score between -1 and 1.
        """
        if len(self.prices) < 3:
            return 0.0

        prices = list(self.prices)

        # Short-term velocity (last 3 observations)
        short_change = (prices[-1] - prices[-3]) / prices[-3] if prices[-3] != 0 else 0

        # Medium-term velocity (all observations)
        if len(prices) >= 5:
            long_change = (prices[-1] - prices[0]) / prices[0] if prices[0] != 0 else 0
        else:
            long_change = short_change

        # Combine and normalize
        velocity = (short_change * 2 + long_change) / 3

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, velocity * 10))  # Scale factor

    def volatility(self) -> float:
        """Calculate rolling volatility (standard deviation of returns)."""
        if len(self.prices) < 3:
            return 0.0

        prices = list(self.prices)
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] != 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])

        if len(returns) < 2:
            return 0.0

        return statistics.stdev(returns)

    def volume_surge(self) -> float:
        """Calculate volume surge ratio (recent vs older)."""
        if len(self.volumes) < 5:
            return 1.0

        volumes = list(self.volumes)
        recent = sum(volumes[-3:]) / 3
        older = sum(volumes[:-3]) / max(len(volumes) - 3, 1)

        if older == 0:
            return 1.0

        return recent / older

    def momentum_score(self) -> float:
        """Combined momentum score based on velocity and volume."""
        vel = self.velocity()
        vol_surge = self.volume_surge()

        # Positive velocity with volume confirmation
        if vel > 0 and vol_surge > 1.2:
            return min(1.0, vel * vol_surge)
        elif vel > 0:
            return vel * 0.5
        else:
            return vel


class RollingState:
    """
    Manages rolling state for all tracked symbols.

    Provides time-series features like velocity, volatility,
    and momentum that require historical context.
    """

    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self._states: dict[str, SymbolRollingState] = {}

    def get_state(self, symbol: str) -> SymbolRollingState:
        """Get or create rolling state for a symbol."""
        if symbol not in self._states:
            self._states[symbol] = SymbolRollingState(
                symbol=symbol,
                window_size=self.window_size,
            )
        return self._states[symbol]

    def update(self, symbol: str, price: float, volume: int, hod: float) -> None:
        """Update rolling state for a symbol."""
        state = self.get_state(symbol)
        state.update(price, volume, hod)

    def velocity(self, symbol: str) -> float:
        """Get velocity for a symbol."""
        return self.get_state(symbol).velocity()

    def volatility(self, symbol: str) -> float:
        """Get volatility for a symbol."""
        return self.get_state(symbol).volatility()

    def momentum(self, symbol: str) -> float:
        """Get momentum score for a symbol."""
        return self.get_state(symbol).momentum_score()

    def hod_breaks(self, symbol: str) -> int:
        """Get HOD break count for a symbol."""
        return self.get_state(symbol).hod_break_count

    def velocity_acceleration(self, symbol: str) -> float:
        """Get velocity acceleration (velocity delta between scans)."""
        return self.get_state(symbol).velocity_accel

    def change_acceleration(self, symbol: str) -> float:
        """Get change_pct acceleration between scans."""
        return self.get_state(symbol).change_accel

    def rvol_cross_up(self, symbol: str) -> bool:
        """True if rvol just crossed 2.0 from below."""
        return self.get_state(symbol).rvol_crossed_up

    def get_momentum_slope(self, symbol: str) -> float:
        """Get linear slope of last 5 momentum scores."""
        return self.get_state(symbol).momentum_slope

    def clear(self, symbol: Optional[str] = None) -> None:
        """Clear state for a symbol or all symbols."""
        if symbol:
            self._states.pop(symbol, None)
        else:
            self._states.clear()

    def symbols(self) -> list[str]:
        """Get list of all tracked symbols."""
        return list(self._states.keys())
