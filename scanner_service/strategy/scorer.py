"""Scoring engine for profile-based ranking."""

import logging
from typing import Optional

from scanner_service.schemas.profile import Profile
from scanner_service.schemas.market_snapshot import Quote
from scanner_service.features.feature_engine import FeatureEngine

logger = logging.getLogger(__name__)


class Scorer:
    """
    Scores symbols based on profile criteria and features.

    Combines profile weights with computed features to produce
    a composite score for ranking.
    """

    def __init__(self, feature_engine: FeatureEngine):
        self.feature_engine = feature_engine

    def score(
        self,
        quote: Quote,
        features: dict,
        profile: Profile,
    ) -> Optional[dict]:
        """
        Score a single symbol for a profile.

        Returns None if the symbol doesn't pass profile filters.
        Returns a score dict with breakdown if it passes.
        """
        # Price filter
        if quote.last_price < profile.min_price:
            return None
        if quote.last_price > profile.max_price:
            return None

        # Volume filter
        if quote.volume < profile.min_volume:
            return None

        # Profile conditions
        if not profile.matches_filters(features):
            return None

        # Compute weighted score
        weights = profile.weights
        score_components = {}

        # Change percentage component
        change_score = self._normalize_change(features.get("change_pct", 0))
        score_components["change_pct"] = change_score * weights.change_pct

        # Velocity component
        velocity_score = (features.get("velocity", 0) + 1) / 2  # Normalize -1,1 to 0,1
        score_components["velocity"] = velocity_score * weights.velocity

        # RVOL component
        rvol = features.get("rvol", 0)
        rvol_score = min(1.0, rvol / 3)  # Cap at 3x RVOL
        score_components["rvol"] = rvol_score * weights.rvol

        # HOD proximity
        hod_score = features.get("hod_proximity", 0)
        score_components["hod_proximity"] = hod_score * weights.hod_proximity

        # Spread (inverse - lower is better)
        spread = features.get("spread", 1)
        spread_score = max(0, 1 - spread / 2)  # 0% spread = 1.0, 2% spread = 0.0
        score_components["spread"] = spread_score * weights.spread

        # Volume score
        volume = features.get("volume", 0)
        volume_score = min(1.0, volume / 5_000_000)  # Cap at 5M
        score_components["volume"] = volume_score * weights.volume

        # Total weighted score
        total_weight = sum([
            weights.change_pct,
            weights.velocity,
            weights.rvol,
            weights.hod_proximity,
            weights.spread,
            weights.volume,
        ])

        raw_score = sum(score_components.values())
        normalized_score = raw_score / total_weight if total_weight > 0 else 0

        # Get AI score
        ai_score = self.feature_engine.get_ai_score(features)

        return {
            "symbol": quote.symbol,
            "raw_score": raw_score,
            "normalized_score": normalized_score,
            "ai_score": ai_score,
            "components": score_components,
            "features": features,
        }

    def _normalize_change(self, change_pct: float) -> float:
        """Normalize change percentage to 0-1 score."""
        if change_pct <= 0:
            return 0.0
        elif change_pct >= 20:
            return 1.0
        else:
            return change_pct / 20

    def score_batch(
        self,
        quotes: dict[str, Quote],
        features: dict[str, dict],
        profile: Profile,
    ) -> list[dict]:
        """
        Score all symbols for a profile.

        Returns list of score dicts for symbols that pass filters.
        """
        scores = []

        for symbol, quote in quotes.items():
            symbol_features = features.get(symbol, {})
            score_result = self.score(quote, symbol_features, profile)
            if score_result:
                scores.append(score_result)

        return scores
