"""
Advisory Buffer for Max Scanner
================================
Pull-based advisory system. Max emits advisories into this buffer.
Bots pull advisories when they want. Max never touches bot state.

Advisory = "symbol X looks interesting because Y" with a TTL.
Consumers call get_active() to pull non-expired advisories.

Negative Advisory = "DO NOT trade symbol X because Y".
Consumers call get_negative() to check before entering.

Max is named after a dog that retrieves the ball.
He fetches symbols. He does not trade. He does not push.
"""

import threading
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Advisory(BaseModel):
    """Single advisory emitted by scanner or news."""

    symbol: str
    source: str = Field(description="e.g. scanner_cycle, news_rss")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(description="Human-readable reason")
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    price: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    rvol: float = 0.0
    float_shares: float = 0.0
    profile: str = ""
    rediscovered: bool = False
    rediscovery_reason: list[str] = Field(default_factory=list)


class NegativeAdvisory(BaseModel):
    """DO_NOT_TRADE signal with reason."""

    symbol: str
    reason: str = Field(description="Why not to trade: extended_move, low_follow_through, spread_expansion, regime_conflict")
    detail: str = Field(default="", description="Human-readable explanation")
    emitted_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    source: str = "scanner_cycle"
    change_pct: float = 0.0
    price: float = 0.0


class AdvisoryBuffer:
    """
    Thread-safe in-memory ring buffer for advisories.

    - Max 500 active, 2000 history (including expired).
    - 120-second per-SYMBOL dedup cooldown (symbol-level, not per-source).
    - Confidence decay: advisory confidence decays linearly with age.
    - Negative advisories: DO_NOT_TRADE signals with reasons.
    - Default TTL: 300 seconds (5 minutes).
    """

    MAX_ACTIVE = 500
    MAX_HISTORY = 2000
    MAX_NEGATIVE = 200
    DEDUP_COOLDOWN_SECONDS = 120  # 2 minutes — faster re-emission for accelerating symbols

    # Confidence decay: lose 40% of original confidence by TTL expiry
    CONFIDENCE_DECAY_RATE = 0.40
    CONFIDENCE_FLOOR = 0.20  # Never decay below this

    # Re-discovery thresholds
    REDISCOVERY_CHANGE_DELTA_PP = 10.0  # +10 percentage points triggers re-discovery
    REDISCOVERY_VOLUME_MULTIPLIER = 2.0  # 2x volume triggers re-discovery

    def __init__(self, ttl_seconds: int = 300):
        self._ttl_seconds = ttl_seconds
        self._active: deque[Advisory] = deque(maxlen=self.MAX_ACTIVE)
        self._history: deque[Advisory] = deque(maxlen=self.MAX_HISTORY)
        self._negative: deque[NegativeAdvisory] = deque(maxlen=self.MAX_NEGATIVE)
        self._lock = threading.Lock()
        # Dedup: symbol -> last_emit_time (symbol-level, not per-source)
        self._dedup: dict[str, datetime] = {}
        # Re-discovery: symbol -> snapshot at first emission {change_pct, volume}
        self._first_seen_snapshot: dict[str, dict] = {}
        self._total_emitted = 0
        self._total_deduped = 0
        self._total_negative = 0
        self._total_rediscovered = 0
        self._total_suppressed_negative = 0

    def emit(
        self,
        symbol: str,
        source: str,
        confidence: float,
        reason: str,
        price: float = 0.0,
        change_pct: float = 0.0,
        volume: int = 0,
        rvol: float = 0.0,
        float_shares: float = 0.0,
        profile: str = "",
        ttl_override: Optional[int] = None,
    ) -> Optional[Advisory]:
        """
        Write an advisory into the buffer.

        Returns the Advisory if emitted, None if deduped.
        Dedup is SYMBOL-LEVEL: same symbol from any source/profile is deduped
        within the cooldown window.
        ttl_override: if set, use this TTL instead of the default.
        """
        now = datetime.utcnow()
        sym = symbol.upper()
        ttl = ttl_override if ttl_override is not None else self._ttl_seconds

        with self._lock:
            is_rediscovered = False
            rediscovery_reasons: list[str] = []

            # Dedup check — symbol-level (not per-source)
            last = self._dedup.get(sym)
            if last and (now - last).total_seconds() < self.DEDUP_COOLDOWN_SECONDS:
                # Check re-discovery gate: has the symbol changed materially?
                snapshot = self._first_seen_snapshot.get(sym)
                if snapshot:
                    orig_change = snapshot.get("change_pct", 0)
                    orig_volume = snapshot.get("volume", 0)

                    # Price extension: change% increased by >=10pp
                    if abs(change_pct) - abs(orig_change) >= self.REDISCOVERY_CHANGE_DELTA_PP:
                        rediscovery_reasons.append("price_extension")

                    # Volume expansion: current volume >= 2x original
                    if orig_volume > 0 and volume >= orig_volume * self.REDISCOVERY_VOLUME_MULTIPLIER:
                        rediscovery_reasons.append("volume_expansion")

                if rediscovery_reasons:
                    is_rediscovered = True
                    self._total_rediscovered += 1
                    logger.info(
                        f"[ADVISORY][REDISCOVERY] {sym} re-discovered: "
                        f"{', '.join(rediscovery_reasons)} "
                        f"(chg={change_pct:+.1f}% vol={volume})"
                    )
                else:
                    self._total_deduped += 1
                    return None

            # Record first-seen snapshot (or update on rediscovery)
            if sym not in self._first_seen_snapshot:
                self._first_seen_snapshot[sym] = {
                    "change_pct": change_pct,
                    "volume": volume,
                    "first_seen": now.isoformat(),
                }

            advisory = Advisory(
                symbol=sym,
                source=source,
                confidence=confidence,
                reason=reason,
                first_seen=now,
                expires_at=now + timedelta(seconds=ttl),
                price=price,
                change_pct=change_pct,
                volume=volume,
                rvol=rvol,
                float_shares=float_shares,
                profile=profile,
                rediscovered=is_rediscovered,
                rediscovery_reason=rediscovery_reasons,
            )

            self._active.append(advisory)
            self._history.append(advisory)
            self._dedup[sym] = now
            self._total_emitted += 1

            # Prune old dedup entries and snapshots
            cutoff = now - timedelta(seconds=self.DEDUP_COOLDOWN_SECONDS * 2)
            stale_keys = [k for k, v in self._dedup.items() if v < cutoff]
            for k in stale_keys:
                del self._dedup[k]
                self._first_seen_snapshot.pop(k, None)

        return advisory

    def emit_negative(
        self,
        symbol: str,
        reason: str,
        detail: str = "",
        source: str = "scanner_cycle",
        change_pct: float = 0.0,
        price: float = 0.0,
        ttl_seconds: int = 600,
    ) -> NegativeAdvisory:
        """
        Emit a DO_NOT_TRADE signal.

        Reasons: extended_move, low_follow_through, spread_expansion, regime_conflict
        TTL defaults to 10 minutes (negative signals should persist longer).
        """
        now = datetime.utcnow()
        neg = NegativeAdvisory(
            symbol=symbol.upper(),
            reason=reason,
            detail=detail,
            emitted_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            source=source,
            change_pct=change_pct,
            price=price,
        )
        with self._lock:
            self._negative.append(neg)
            self._total_negative += 1
        logger.info(f"[ADVISORY][NEGATIVE] {symbol} | {reason} | {detail}")
        return neg

    def get_negative(self, symbol: Optional[str] = None) -> list[NegativeAdvisory]:
        """Get active (non-expired) negative advisories. Optionally filter by symbol."""
        now = datetime.utcnow()
        with self._lock:
            results = []
            for neg in self._negative:
                if neg.expires_at < now:
                    continue
                if symbol and neg.symbol != symbol.upper():
                    continue
                results.append(neg)
        return results

    def is_negative(self, symbol: str) -> tuple[bool, str]:
        """Check if symbol has an active negative advisory. Returns (is_blocked, reason)."""
        negs = self.get_negative(symbol=symbol)
        if negs:
            latest = max(negs, key=lambda n: n.emitted_at)
            return True, f"{latest.reason}: {latest.detail}"
        return False, ""

    def record_negative_suppression(self):
        """Increment counter when a positive advisory is suppressed by an active negative."""
        self._total_suppressed_negative += 1

    def _decayed_confidence(self, adv: Advisory, now: datetime) -> float:
        """Calculate decayed confidence based on advisory age."""
        age_seconds = (now - adv.first_seen).total_seconds()
        ttl_seconds = (adv.expires_at - adv.first_seen).total_seconds()
        if ttl_seconds <= 0:
            return adv.confidence

        age_ratio = min(age_seconds / ttl_seconds, 1.0)
        decay = 1.0 - (self.CONFIDENCE_DECAY_RATE * age_ratio)
        decayed = adv.confidence * decay
        return max(decayed, self.CONFIDENCE_FLOOR)

    def get_active(
        self,
        min_confidence: float = 0.0,
        max_age_seconds: Optional[int] = None,
        profile: Optional[str] = None,
        apply_decay: bool = True,
    ) -> list[Advisory]:
        """
        Pull non-expired advisories with optional filters.

        When apply_decay=True (default), confidence is adjusted downward
        based on age. Older advisories have lower effective confidence.
        The returned Advisory objects have their confidence field updated
        to reflect the decayed value.
        """
        now = datetime.utcnow()

        with self._lock:
            results = []
            for adv in self._active:
                if adv.expires_at < now:
                    continue

                effective_confidence = (
                    self._decayed_confidence(adv, now) if apply_decay else adv.confidence
                )

                if effective_confidence < min_confidence:
                    continue
                if max_age_seconds and (now - adv.first_seen).total_seconds() > max_age_seconds:
                    continue
                if profile and adv.profile != profile:
                    continue

                # Return copy with decayed confidence
                result = adv.model_copy()
                if apply_decay:
                    result.confidence = round(effective_confidence, 4)
                results.append(result)

        return results

    def get_history(self, limit: int = 100) -> list[Advisory]:
        """Get recent advisories including expired."""
        with self._lock:
            items = list(self._history)
        # Most recent first
        items.reverse()
        return items[:limit]

    def clear(self):
        """Wipe the buffer (positive and negative)."""
        with self._lock:
            self._active.clear()
            self._history.clear()
            self._dedup.clear()
            self._negative.clear()
            self._first_seen_snapshot.clear()
        logger.info("[ADVISORY] Buffer cleared (positive + negative)")

    def get_stats(self) -> dict:
        """Counts and metrics with source breakdown."""
        now = datetime.utcnow()
        with self._lock:
            active = [a for a in self._active if a.expires_at >= now]
            active_count = len(active)
            expired_count = sum(1 for a in self._active if a.expires_at < now)
            unique_symbols = len(set(a.symbol for a in active))

            # Source breakdown
            by_source: dict[str, int] = {}
            by_profile: dict[str, int] = {}
            for a in active:
                by_source[a.source] = by_source.get(a.source, 0) + 1
                if a.profile:
                    by_profile[a.profile] = by_profile.get(a.profile, 0) + 1

            # Negative advisory stats
            active_negative = [n for n in self._negative if n.expires_at >= now]
            negative_count = len(active_negative)
            negative_by_reason: dict[str, int] = {}
            for n in active_negative:
                negative_by_reason[n.reason] = negative_by_reason.get(n.reason, 0) + 1

            # Average confidence of active advisories
            avg_confidence = 0.0
            if active:
                avg_confidence = round(sum(a.confidence for a in active) / len(active), 4)

            # Count rediscovered in active
            rediscovered_active = sum(1 for a in active if a.rediscovered)

        return {
            "active_advisories": active_count,
            "expired_in_buffer": expired_count,
            "history_size": len(self._history),
            "unique_symbols": unique_symbols,
            "total_emitted": self._total_emitted,
            "total_deduped": self._total_deduped,
            "total_rediscovered": self._total_rediscovered,
            "total_suppressed_negative": self._total_suppressed_negative,
            "avg_advisory_confidence": avg_confidence,
            "rediscovered_active": rediscovered_active,
            "dedup_ratio": round(self._total_deduped / max(self._total_emitted + self._total_deduped, 1) * 100, 1),
            "rediscovery_rate": round(self._total_rediscovered / max(self._total_emitted, 1) * 100, 1),
            "ttl_seconds": self._ttl_seconds,
            "dedup_cooldown_seconds": self.DEDUP_COOLDOWN_SECONDS,
            "confidence_decay_rate": self.CONFIDENCE_DECAY_RATE,
            "by_source": by_source,
            "by_profile": by_profile,
            "negative_active": negative_count,
            "negative_total": self._total_negative,
            "negative_by_reason": negative_by_reason,
        }


# Singleton
_buffer: Optional[AdvisoryBuffer] = None


def get_advisory_buffer(ttl_seconds: int = 300) -> AdvisoryBuffer:
    """Get or create the advisory buffer singleton."""
    global _buffer
    if _buffer is None:
        _buffer = AdvisoryBuffer(ttl_seconds=ttl_seconds)
    return _buffer
