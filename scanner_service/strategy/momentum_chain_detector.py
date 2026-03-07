"""
Momentum Chain Detector
=========================
Detects sector clusters where multiple symbols move simultaneously.

A MOMENTUM_CHAIN requires:
  - 1+ leader:   gain_pct >= 12%, rvol >= 5x
  - 1+ sympathy: gain_pct >= 6%,  rvol >= 2x
  in the same sector.

Chain membership boosts scanner scores:
  - chain_multiplier = 1.35
  - leader_multiplier = 1.10 (additional)
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Thresholds
LEADER_CHANGE_MIN = 12.0    # % gain
LEADER_RVOL_MIN = 5.0       # relative volume
SYMPATHY_CHANGE_MIN = 6.0   # % gain
SYMPATHY_RVOL_MIN = 2.0     # relative volume

# Score multipliers
CHAIN_MULTIPLIER = 1.35
LEADER_BONUS_MULTIPLIER = 1.10


def sector_multiplier(heat_score: float) -> float:
    """Convert sector heat score to a score multiplier."""
    if heat_score >= 0.70:
        return 1.30
    if heat_score >= 0.50:
        return 1.15
    if heat_score >= 0.30:
        return 1.05
    if heat_score >= 0.20:
        return 1.00
    return 0.85


@dataclass
class ChainMember:
    """A symbol that belongs to a momentum chain."""
    symbol: str
    sector: str
    role: str  # "leader" or "sympathy"
    change_pct: float
    rvol: float
    price: float
    volume: int


@dataclass
class MomentumChain:
    """A detected sector momentum chain."""
    sector: str
    leaders: List[ChainMember] = field(default_factory=list)
    sympathy: List[ChainMember] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.leaders) + len(self.sympathy)

    @property
    def is_valid(self) -> bool:
        return len(self.leaders) >= 1 and len(self.sympathy) >= 1

    def all_symbols(self) -> List[str]:
        return [m.symbol for m in self.leaders] + [m.symbol for m in self.sympathy]

    def to_dict(self) -> dict:
        return {
            "sector": self.sector,
            "size": self.size,
            "leaders": [
                {
                    "symbol": m.symbol,
                    "change_pct": round(m.change_pct, 1),
                    "rvol": round(m.rvol, 1),
                    "price": m.price,
                }
                for m in self.leaders
            ],
            "sympathy": [
                {
                    "symbol": m.symbol,
                    "change_pct": round(m.change_pct, 1),
                    "rvol": round(m.rvol, 1),
                    "price": m.price,
                }
                for m in self.sympathy
            ],
        }


class MomentumChainDetector:
    """
    Detects sector momentum chains from watchlist candidates.

    Usage:
      1. Call detect() with candidate data + sector classifications
      2. Query get_role(symbol) to check chain membership
      3. Apply score multipliers via get_multiplier(symbol)
    """

    def __init__(self):
        self._chains: Dict[str, MomentumChain] = {}  # sector -> chain
        self._symbol_roles: Dict[str, str] = {}  # symbol -> "leader" | "sympathy" | "none"
        self._symbol_sectors: Dict[str, str] = {}  # symbol -> sector

    def detect(
        self,
        candidates: List[dict],
        sector_map: Dict[str, str],
    ) -> List[MomentumChain]:
        """
        Detect momentum chains from candidate data.

        Args:
            candidates: List of dicts with keys:
                symbol, change_pct, rvol, price, volume
            sector_map: {symbol: sector_name}

        Returns:
            List of valid MomentumChain objects detected
        """
        self._chains.clear()
        self._symbol_roles.clear()
        self._symbol_sectors = dict(sector_map)

        # Group by sector
        sector_groups: Dict[str, List[dict]] = defaultdict(list)
        for c in candidates:
            sym = c["symbol"]
            sector = sector_map.get(sym, "unknown")
            if sector != "unknown":
                sector_groups[sector].append(c)

        # Detect chains per sector
        detected = []
        for sector, members in sector_groups.items():
            chain = MomentumChain(sector=sector)

            for m in members:
                sym = m["symbol"]
                change = abs(m.get("change_pct", 0))
                rvol = m.get("rvol", 0)

                if change >= LEADER_CHANGE_MIN and rvol >= LEADER_RVOL_MIN:
                    chain.leaders.append(ChainMember(
                        symbol=sym, sector=sector, role="leader",
                        change_pct=m.get("change_pct", 0), rvol=rvol,
                        price=m.get("price", 0), volume=m.get("volume", 0),
                    ))
                elif change >= SYMPATHY_CHANGE_MIN and rvol >= SYMPATHY_RVOL_MIN:
                    chain.sympathy.append(ChainMember(
                        symbol=sym, sector=sector, role="sympathy",
                        change_pct=m.get("change_pct", 0), rvol=rvol,
                        price=m.get("price", 0), volume=m.get("volume", 0),
                    ))

            if chain.is_valid:
                self._chains[sector] = chain
                detected.append(chain)

                # Record roles
                for m in chain.leaders:
                    self._symbol_roles[m.symbol] = "leader"
                for m in chain.sympathy:
                    self._symbol_roles[m.symbol] = "sympathy"

                # Log detection
                leader_strs = [f"{m.symbol} +{m.change_pct:.0f}%" for m in chain.leaders]
                sympathy_strs = [f"{m.symbol} +{m.change_pct:.0f}%" for m in chain.sympathy]
                logger.info(
                    f"[CHAIN] Momentum chain detected: {sector.upper()}\n"
                    f"  leader={','.join(leader_strs)}\n"
                    f"  sympathy=[{','.join(sympathy_strs)}]"
                )

        return detected

    def get_role(self, symbol: str) -> str:
        """Get chain role for symbol: 'leader', 'sympathy', or 'none'."""
        return self._symbol_roles.get(symbol.upper(), "none")

    def get_sector(self, symbol: str) -> str:
        """Get sector for symbol."""
        return self._symbol_sectors.get(symbol.upper(), "unknown")

    def get_multiplier(self, symbol: str) -> float:
        """
        Get total chain multiplier for a symbol.

        leader:   CHAIN_MULTIPLIER * LEADER_BONUS_MULTIPLIER = 1.485
        sympathy: CHAIN_MULTIPLIER = 1.35
        none:     1.0
        """
        role = self.get_role(symbol)
        if role == "leader":
            return CHAIN_MULTIPLIER * LEADER_BONUS_MULTIPLIER
        elif role == "sympathy":
            return CHAIN_MULTIPLIER
        return 1.0

    def get_chains(self) -> List[dict]:
        """Get all detected chains as dicts."""
        return [c.to_dict() for c in self._chains.values() if c.is_valid]

    def get_chain_symbols(self) -> Dict[str, str]:
        """Get all symbols in chains with their roles."""
        return dict(self._symbol_roles)

    def clear(self):
        """Clear all chain state."""
        self._chains.clear()
        self._symbol_roles.clear()
        self._symbol_sectors.clear()
