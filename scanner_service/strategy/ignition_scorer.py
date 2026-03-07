"""
Ignition Scorer — Composite Ranking for Highest Probability of Ignition
=========================================================================
Replaces simple filtering with a multi-factor composite score.

Score = relative_volume * float_score * catalyst_score * price_range_expansion * liquidity_score
      * sector_multiplier(heat) * chain_multiplier

Each factor is normalized to [0, 1] and the product creates a multiplicative
gate — a zero in any factor kills the score entirely.

Sector heat and chain membership are applied as post-multipliers.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Set

from scanner_service.strategy.momentum_chain_detector import (
    MomentumChainDetector, sector_multiplier,
)

logger = logging.getLogger(__name__)


def heat_label(heat_score: float) -> str:
    """Convert numeric heat score to display label."""
    if heat_score >= 0.70:
        return "HOT"
    if heat_score >= 0.50:
        return "WARM"
    if heat_score >= 0.30:
        return "COOL"
    return "COLD"


class IgnitionScorer:
    """
    Scores symbols by probability of ignition (explosive move).

    Factors:
      1. relative_volume  — volume vs average (higher = more interest)
      2. float_score      — lower float = higher score (easier to move)
      3. catalyst_score   — news/catalyst presence boosts score
      4. price_range_expansion — intraday range vs avg range
      5. liquidity_score  — bid/ask spread health (too wide = bad)
      6. sector_heat      — hot sector multiplier (from research server)
      7. chain_membership — momentum chain multiplier (leader/sympathy boost)
    """

    def __init__(self):
        # Symbols with known catalysts (set by news pipeline)
        self._catalyst_symbols: Dict[str, float] = {}  # symbol -> catalyst confidence
        self._catalyst_types: Dict[str, str] = {}  # symbol -> catalyst type

    def update_catalysts(self, catalyst_map: Dict[str, float], type_map: Optional[Dict[str, str]] = None):
        """Update known catalyst symbols from news pipeline."""
        self._catalyst_symbols.update(catalyst_map)
        if type_map:
            self._catalyst_types.update(type_map)

    def update_catalysts_from_news(self, news_alerts: list):
        """Update catalysts from NewsAlert objects."""
        for alert in news_alerts:
            for sym in alert.symbols:
                self._catalyst_symbols[sym] = max(
                    self._catalyst_symbols.get(sym, 0),
                    alert.confidence,
                )
                self._catalyst_types[sym] = alert.catalyst_type

    def score_symbol(
        self,
        symbol: str,
        rvol: float,
        float_millions: float,
        spread_pct: float,
        change_pct: float,
        high: float,
        low: float,
        prev_close: float,
        volume: int,
        avg_volume: int,
        sector_heat: float = 0.30,
        chain_mult: float = 1.0,
        sector: str = "unknown",
        cluster_role: str = "none",
    ) -> dict:
        """
        Compute ignition score for a single symbol.

        Returns dict with total score and component breakdown.
        """
        # 1. Relative Volume Score (0-1)
        rv_score = min(1.0, rvol / 5.0) if rvol > 0 else 0.0

        # 2. Float Score (0-1) — lower float = higher score
        if float_millions <= 0:
            fl_score = 0.5  # Unknown float — neutral
        elif float_millions < 10:
            fl_score = 1.0
        elif float_millions < 20:
            fl_score = 0.8
        elif float_millions < 50:
            fl_score = 0.6
        elif float_millions < 100:
            fl_score = 0.3
        else:
            fl_score = 0.1

        # 3. Catalyst Score (0-1)
        cat_confidence = self._catalyst_symbols.get(symbol, 0)
        cat_type = self._catalyst_types.get(symbol, "none")
        if cat_confidence > 0:
            cat_score = max(0.5, cat_confidence)
        else:
            cat_score = 0.3

        # 4. Price Range Expansion (0-1)
        if prev_close > 0 and high > low:
            intraday_range_pct = ((high - low) / prev_close) * 100
            pre_score = min(1.0, intraday_range_pct / 10.0)
        else:
            pre_score = 0.3

        # 5. Liquidity Score (0-1)
        if spread_pct <= 0.1:
            liq_score = 1.0
        elif spread_pct <= 0.3:
            liq_score = 0.85
        elif spread_pct <= 0.5:
            liq_score = 0.7
        elif spread_pct <= 1.0:
            liq_score = 0.4
        elif spread_pct <= 2.0:
            liq_score = 0.2
        else:
            liq_score = 0.1

        # Base composite (multiplicative)
        base_score = rv_score * fl_score * cat_score * pre_score * liq_score

        # 6. Sector heat multiplier
        sec_mult = sector_multiplier(sector_heat)

        # 7. Chain multiplier (already computed externally)
        # chain_mult passed in

        # Final score
        total = base_score * sec_mult * chain_mult

        return {
            "symbol": symbol,
            "ignition_score": round(total, 4),
            "components": {
                "relative_volume": round(rv_score, 3),
                "float_score": round(fl_score, 3),
                "catalyst_score": round(cat_score, 3),
                "price_range_expansion": round(pre_score, 3),
                "liquidity_score": round(liq_score, 3),
                "sector_multiplier": round(sec_mult, 2),
                "chain_multiplier": round(chain_mult, 2),
            },
            "meta": {
                "rvol": round(rvol, 2),
                "float_m": round(float_millions, 1),
                "spread_pct": round(spread_pct, 3),
                "change_pct": round(change_pct, 2),
                "catalyst_type": cat_type,
                "volume": volume,
                "sector": sector,
                "heat_score": round(sector_heat, 2),
                "heat": heat_label(sector_heat),
                "cluster_role": cluster_role,
            },
        }

    def rank_symbols(
        self,
        rows: list,
        features: Dict[str, dict],
        quotes: dict,
        limit: int = 20,
        sector_map: Optional[Dict[str, str]] = None,
        heat_scores: Optional[Dict[str, float]] = None,
        chain_detector: Optional[MomentumChainDetector] = None,
    ) -> List[dict]:
        """
        Score and rank all symbols by ignition probability.

        Args:
            rows: ScannerRow objects (from ranker output)
            features: Feature dict keyed by symbol
            quotes: Quote objects keyed by symbol
            limit: Max symbols to return
            sector_map: {symbol: sector_name} from research server
            heat_scores: {sector: heat_score} from research heatmap
            chain_detector: MomentumChainDetector with detected chains

        Returns:
            List of scored symbols sorted by ignition_score descending
        """
        sector_map = sector_map or {}
        heat_scores = heat_scores or {}

        scored = []
        for row in rows:
            sym = row.symbol
            quote = quotes.get(sym)
            feat = features.get(sym, {})
            if not quote:
                continue

            # Sector intelligence
            sector = sector_map.get(sym, "unknown")
            s_heat = heat_scores.get(sector, 0.30)
            chain_mult = chain_detector.get_multiplier(sym) if chain_detector else 1.0
            cluster_role = chain_detector.get_role(sym) if chain_detector else "none"

            result = self.score_symbol(
                symbol=sym,
                rvol=feat.get("rvol", row.rvol if hasattr(row, "rvol") else 0),
                float_millions=quote.float_shares if quote.float_shares else 0,
                spread_pct=feat.get("spread", 0),
                change_pct=row.change_pct,
                high=quote.high,
                low=quote.low,
                prev_close=quote.prev_close if hasattr(quote, "prev_close") else 0,
                volume=row.volume,
                avg_volume=quote.avg_volume if quote.avg_volume else 0,
                sector_heat=s_heat,
                chain_mult=chain_mult,
                sector=sector,
                cluster_role=cluster_role,
            )
            scored.append(result)

        # Sort by ignition_score descending
        scored.sort(key=lambda x: x["ignition_score"], reverse=True)
        return scored[:limit]

    def get_catalyst_symbols(self) -> Dict[str, float]:
        """Return current catalyst map."""
        return dict(self._catalyst_symbols)

    def clear_catalysts(self):
        """Clear catalyst tracking (call at session start)."""
        self._catalyst_symbols.clear()
        self._catalyst_types.clear()
