"""Ranking engine for scanner output."""

import logging
from datetime import datetime
from typing import Optional

from scanner_service.schemas.profile import Profile
from scanner_service.schemas.events import ScannerRow, ScannerOutput
from scanner_service.schemas.market_snapshot import MarketSnapshot

logger = logging.getLogger(__name__)


class Ranker:
    """
    Ranks scored symbols and produces scanner output.

    Handles final ranking, limiting, and output formatting.
    """

    def __init__(self):
        self._cache: dict[str, ScannerOutput] = {}
        self._cache_time: dict[str, datetime] = {}

    def rank(
        self,
        scores: list[dict],
        profile: Profile,
        snapshot: MarketSnapshot,
        limit: int = 50,
    ) -> ScannerOutput:
        """
        Rank scored symbols and produce scanner output.

        Args:
            scores: List of score dicts from Scorer
            profile: Profile used for scoring
            snapshot: Market snapshot for additional data
            limit: Maximum rows to return

        Returns:
            ScannerOutput with ranked rows
        """
        start_time = datetime.utcnow()

        # Sort by AI score (primary) and normalized score (secondary)
        sorted_scores = sorted(
            scores,
            key=lambda x: (x["ai_score"], x["normalized_score"]),
            reverse=True,
        )

        # Limit results
        top_scores = sorted_scores[:limit]

        # Build scanner rows
        rows = []
        for rank, score_data in enumerate(top_scores, start=1):
            symbol = score_data["symbol"]
            quote = snapshot.quotes.get(symbol)

            if not quote:
                continue

            features = score_data["features"]

            row = ScannerRow(
                rank=rank,
                symbol=symbol,
                last_price=quote.last_price,
                change_pct=quote.change_pct,
                volume=quote.volume,
                rvol=features.get("rvol", 0),
                velocity=features.get("velocity", 0),
                hod_proximity=features.get("hod_proximity", 0),
                spread=features.get("spread", 0),
                ai_score=score_data["ai_score"],
                profile=profile.name,
                alerts=[],  # Populated by alert system
            )
            rows.append(row)

        scan_time = (datetime.utcnow() - start_time).total_seconds() * 1000

        output = ScannerOutput(
            profile=profile.name,
            rows=rows,
            total_candidates=len(scores),
            scan_time_ms=scan_time,
        )

        # Cache result
        self._cache[profile.name] = output
        self._cache_time[profile.name] = datetime.utcnow()

        return output

    def get_cached(self, profile_name: str) -> Optional[ScannerOutput]:
        """Get cached scanner output for a profile."""
        return self._cache.get(profile_name)

    def get_symbol_data(
        self,
        symbol: str,
        profiles: Optional[list[str]] = None,
    ) -> dict:
        """
        Get aggregated data for a symbol across profiles.

        Returns data from all cached profiles or specified ones.
        """
        result = {
            "symbol": symbol,
            "profiles": {},
            "best_rank": None,
            "best_profile": None,
            "avg_ai_score": 0,
        }

        profile_names = profiles or list(self._cache.keys())
        ai_scores = []
        best_rank = float("inf")

        for profile_name in profile_names:
            output = self._cache.get(profile_name)
            if not output:
                continue

            for row in output.rows:
                if row.symbol == symbol:
                    result["profiles"][profile_name] = {
                        "rank": row.rank,
                        "ai_score": row.ai_score,
                        "change_pct": row.change_pct,
                        "rvol": row.rvol,
                    }
                    ai_scores.append(row.ai_score)

                    if row.rank < best_rank:
                        best_rank = row.rank
                        result["best_rank"] = row.rank
                        result["best_profile"] = profile_name
                    break

        if ai_scores:
            result["avg_ai_score"] = sum(ai_scores) / len(ai_scores)

        return result

    def clear_cache(self, profile_name: Optional[str] = None) -> None:
        """Clear cached results."""
        if profile_name:
            self._cache.pop(profile_name, None)
            self._cache_time.pop(profile_name, None)
        else:
            self._cache.clear()
            self._cache_time.clear()
