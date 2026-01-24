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

        v0.1: Rule-based scoring (ML deferred to v0.2)
        """
        score = 0.0
        weights_sum = 0.0

        # Velocity component (0-0.25)
        velocity = features.get("velocity", 0)
        if velocity > 0:
            score += min(0.25, velocity * 0.25)
        weights_sum += 0.25

        # RVOL component (0-0.2)
        rvol = features.get("rvol", 0)
        if rvol > 1.0:
            rvol_score = min(0.2, (rvol - 1) * 0.1)
            score += rvol_score
        weights_sum += 0.2

        # HOD proximity (0-0.2)
        hod_prox = features.get("hod_proximity", 0)
        if hod_prox > 0.9:  # Near HOD
            score += 0.2
        elif hod_prox > 0.8:
            score += 0.15
        elif hod_prox > 0.7:
            score += 0.1
        weights_sum += 0.2

        # Spread penalty (negative if spread is wide)
        spread = features.get("spread", 0)
        if spread < 0.1:
            score += 0.1  # Tight spread bonus
        elif spread > 0.5:
            score -= min(0.1, spread * 0.05)  # Wide spread penalty
        weights_sum += 0.1

        # Change percentage (0-0.15)
        change_pct = features.get("change_pct", 0)
        if 2.0 < change_pct < 15.0:  # Sweet spot
            score += 0.15
        elif 0.5 < change_pct <= 2.0:
            score += 0.1
        elif change_pct > 15:  # Might be overextended
            score += 0.05
        weights_sum += 0.15

        # Momentum component (0-0.1)
        momentum = features.get("momentum", 0)
        if momentum > 0:
            score += min(0.1, momentum * 0.1)
        weights_sum += 0.1

        # Normalize to 0-1
        normalized = max(0.0, min(1.0, score / weights_sum * 2))

        return round(normalized, 3)

    def clear_state(self) -> None:
        """Clear all rolling state."""
        self.rolling.clear()
