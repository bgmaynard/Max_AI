"""Feature computation engine for scanner scoring."""

import logging
from typing import Optional
from datetime import datetime

from scanner_service.schemas.market_snapshot import Quote, MarketSnapshot
from scanner_service.features.rolling import RollingState

logger = logging.getLogger(__name__)


class FeatureEngine:
    """
    Computes features for scanner scoring.

    Combines real-time quote data with rolling historical state
    to produce feature vectors for each symbol.
    """

    def __init__(self):
        self.rolling = RollingState(window_size=20)
        self._last_update: Optional[datetime] = None

    def update_from_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Update rolling state from a market snapshot."""
        for symbol, quote in snapshot.quotes.items():
            self.rolling.update(
                symbol=symbol,
                price=quote.last_price,
                volume=quote.volume,
                hod=quote.high,
            )
            # Update quote-derived acceleration features
            self.rolling.get_state(symbol).update_quote_features(
                change_pct=quote.change_pct,
                rvol=quote.rvol,
            )
        self._last_update = snapshot.timestamp

    def compute_features(self, quote: Quote) -> dict:
        """
        Compute full feature set for a single quote.

        Returns a dictionary of features used for scoring.
        """
        symbol = quote.symbol

        # Real-time features from quote
        features = {
            # Price features
            "last_price": quote.last_price,
            "change": quote.change,
            "change_pct": quote.change_pct,
            "gap_pct": quote.gap_pct,

            # Volume features
            "volume": quote.volume,
            "avg_volume": quote.avg_volume,
            "rvol": quote.rvol,

            # Spread/liquidity
            "spread": quote.spread,
            "bid": quote.bid,
            "ask": quote.ask,

            # Position features
            "hod_proximity": quote.hod_proximity,
            "high": quote.high,
            "low": quote.low,

            # Rolling features
            "velocity": self.rolling.velocity(symbol),
            "volatility": self.rolling.volatility(symbol),
            "momentum": self.rolling.momentum(symbol),
            "hod_breaks": self.rolling.hod_breaks(symbol),
            "volume_surge": self.rolling.get_state(symbol).volume_surge(),

            # Acceleration features (early breakout detection)
            "velocity_accel": self.rolling.velocity_acceleration(symbol),
            "change_accel": self.rolling.change_acceleration(symbol),
            "rvol_cross_up": self.rolling.rvol_cross_up(symbol),
            "momentum_slope": self.rolling.get_momentum_slope(symbol),
        }

        return features

    def compute_batch_features(
        self, snapshot: MarketSnapshot
    ) -> dict[str, dict]:
        """Compute features for all symbols in a snapshot."""
        # First update rolling state
        self.update_from_snapshot(snapshot)

        # Then compute features
        features = {}
        for symbol, quote in snapshot.quotes.items():
            features[symbol] = self.compute_features(quote)

        return features

    def get_ai_score(self, features: dict) -> float:
        """
        Compute AI score (0-1) based on features.

        v0.2: Acceleration-aware scoring.
        Detects momentum BUILDING (before breakout) not just momentum EXISTING.
        """
        score = 0.0
        weights_sum = 0.0

        # === ACCELERATION SIGNALS (early detection) ===

        # Velocity acceleration (0-0.25) — velocity is increasing
        vel_accel = features.get("velocity_accel", 0)
        if vel_accel > 0.15:
            score += 0.25  # Strong acceleration
        elif vel_accel > 0.05:
            score += 0.15  # Moderate acceleration
        elif vel_accel > 0:
            score += 0.05  # Slight acceleration
        weights_sum += 0.25

        # Momentum slope (0-0.20) — momentum trending up
        mom_slope = features.get("momentum_slope", 0)
        if mom_slope > 0.1:
            score += 0.20
        elif mom_slope > 0.03:
            score += 0.12
        elif mom_slope > 0:
            score += 0.05
        weights_sum += 0.20

        # RVOL cross-up bonus (0-0.15) — volume just arrived
        rvol = features.get("rvol", 0)
        rvol_cross = features.get("rvol_cross_up", False)
        if rvol_cross:
            score += 0.15  # Just crossed 2.0x — fresh volume arrival
        elif rvol > 2.0:
            score += 0.10  # Already above 2.0x
        elif rvol > 1.0:
            score += 0.05
        weights_sum += 0.15

        # Change acceleration (0-0.10) — price is accelerating
        change_accel = features.get("change_accel", 0)
        if change_accel > 0.3:
            score += 0.10
        elif change_accel > 0.1:
            score += 0.06
        weights_sum += 0.10

        # === CONFIRMATION SIGNALS (existing features, reduced weight) ===

        # Velocity (0-0.10) — current velocity
        velocity = features.get("velocity", 0)
        if velocity > 0:
            score += min(0.10, velocity * 0.10)
        weights_sum += 0.10

        # HOD proximity (0-0.10)
        hod_prox = features.get("hod_proximity", 0)
        if hod_prox > 0.9:
            score += 0.10
        elif hod_prox > 0.8:
            score += 0.07
        weights_sum += 0.10

        # Spread penalty (tight = good, wide = bad)
        spread = features.get("spread", 0)
        if spread < 0.1:
            score += 0.05
        elif spread > 0.5:
            score -= min(0.05, spread * 0.03)
        weights_sum += 0.05

        # Change percentage — reduced weight, no longer primary signal
        change_pct = features.get("change_pct", 0)
        if 1.0 < change_pct < 15.0:
            score += 0.05
        elif change_pct > 15:
            score += 0.02  # Overextended penalty
        weights_sum += 0.05

        # Normalize to 0-1
        normalized = max(0.0, min(1.0, score / weights_sum * 2))

        return round(normalized, 3)

    def clear_state(self) -> None:
        """Clear all rolling state."""
        self.rolling.clear()
