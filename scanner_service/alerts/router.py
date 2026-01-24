"""Alert routing and management."""

import logging
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from scanner_service.settings import get_settings
from scanner_service.schemas.events import AlertEvent, AlertType, ScannerRow
from scanner_service.schemas.profile import Profile
from scanner_service.alerts.audio import AudioPlayer

logger = logging.getLogger(__name__)


class AlertRouter:
    """
    Routes and manages scanner alerts.

    Handles alert triggering, cooldowns, and audio playback.
    """

    def __init__(self):
        self.settings = get_settings()
        self.audio = AudioPlayer()
        self._recent_alerts: deque[AlertEvent] = deque(maxlen=1000)
        self._cooldowns: dict[str, datetime] = {}  # symbol -> last alert time
        self._alert_counts: dict[str, int] = {}  # symbol -> count today

    def check_and_trigger(
        self,
        row: ScannerRow,
        features: dict,
        profile: Profile,
    ) -> Optional[AlertEvent]:
        """
        Check if an alert should be triggered and create it.

        Returns AlertEvent if triggered, None otherwise.
        """
        if not profile.alert_enabled:
            return None

        # Check AI score threshold
        if row.ai_score < profile.alert_threshold:
            return None

        # Check cooldown
        cooldown_key = f"{row.symbol}:{profile.name}"
        if self._is_on_cooldown(cooldown_key):
            return None

        # Determine alert type
        alert_type = self._determine_alert_type(row, features)

        # Create alert
        alert = AlertEvent(
            id=str(uuid.uuid4()),
            alert_type=alert_type,
            symbol=row.symbol,
            profile=profile.name,
            message=self._format_message(row, alert_type),
            ai_score=row.ai_score,
            price=row.last_price,
            change_pct=row.change_pct,
            volume=row.volume,
            metadata={
                "rvol": row.rvol,
                "velocity": row.velocity,
                "hod_proximity": row.hod_proximity,
            },
        )

        # Set cooldown
        self._cooldowns[cooldown_key] = datetime.utcnow()

        # Track count
        self._alert_counts[row.symbol] = self._alert_counts.get(row.symbol, 0) + 1

        # Store alert
        self._recent_alerts.append(alert)

        # Play sound (non-blocking)
        sound_file = profile.alert_sound or self._get_default_sound(alert_type)
        if sound_file:
            played = self.audio.play(sound_file)
            alert.sound_played = played

        logger.info(f"Alert triggered: {alert.alert_type} for {alert.symbol}")

        return alert

    def _is_on_cooldown(self, key: str) -> bool:
        """Check if a symbol/profile is on cooldown."""
        last_alert = self._cooldowns.get(key)
        if not last_alert:
            return False

        cooldown = timedelta(seconds=self.settings.alert_cooldown_sec)
        return datetime.utcnow() - last_alert < cooldown

    def _determine_alert_type(self, row: ScannerRow, features: dict) -> AlertType:
        """Determine the appropriate alert type based on conditions."""
        # HOD Break - near or at high of day with momentum
        if row.hod_proximity > 0.98 and features.get("hod_breaks", 0) > 0:
            return AlertType.HOD_BREAK

        # Gap alert - significant gap up/down
        gap_pct = features.get("gap_pct", 0)
        if abs(gap_pct) > 3.0:
            return AlertType.GAP_ALERT

        # Momentum surge - high velocity with volume
        if row.velocity > 0.5 and row.rvol > 2.0:
            return AlertType.MOMO_SURGE

        # Default to momo surge for high AI scores
        return AlertType.MOMO_SURGE

    def _format_message(self, row: ScannerRow, alert_type: AlertType) -> str:
        """Format alert message."""
        messages = {
            AlertType.HOD_BREAK: f"{row.symbol} breaking HOD! ${row.last_price:.2f} (+{row.change_pct:.1f}%)",
            AlertType.GAP_ALERT: f"{row.symbol} gap alert ${row.last_price:.2f} ({row.change_pct:+.1f}%)",
            AlertType.MOMO_SURGE: f"{row.symbol} momentum surge ${row.last_price:.2f} (+{row.change_pct:.1f}%) RVOL {row.rvol:.1f}x",
            AlertType.NEWS: f"{row.symbol} news catalyst ${row.last_price:.2f}",
            AlertType.RISK: f"{row.symbol} risk warning ${row.last_price:.2f}",
        }
        return messages.get(alert_type, f"{row.symbol} alert")

    def _get_default_sound(self, alert_type: AlertType) -> str:
        """Get default sound file for alert type."""
        sounds = {
            AlertType.HOD_BREAK: "hod_break.wav",
            AlertType.GAP_ALERT: "gap_alert.wav",
            AlertType.MOMO_SURGE: "momo_surge.wav",
            AlertType.NEWS: "news.wav",
            AlertType.RISK: "risk.wav",
        }
        return sounds.get(alert_type, "momo_surge.wav")

    def get_recent(self, limit: int = 50) -> list[AlertEvent]:
        """Get recent alerts."""
        alerts = list(self._recent_alerts)
        alerts.reverse()  # Most recent first
        return alerts[:limit]

    def get_for_symbol(self, symbol: str, limit: int = 10) -> list[AlertEvent]:
        """Get recent alerts for a specific symbol."""
        alerts = [a for a in self._recent_alerts if a.symbol == symbol]
        alerts.reverse()
        return alerts[:limit]

    def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an alert."""
        for alert in self._recent_alerts:
            if alert.id == alert_id:
                alert.acknowledged = True
                return True
        return False

    def test_alert(self, alert_type: AlertType, symbol: str = "TEST") -> AlertEvent:
        """Generate a test alert for testing audio/notifications."""
        alert = AlertEvent(
            id=str(uuid.uuid4()),
            alert_type=alert_type,
            symbol=symbol,
            profile="TEST",
            message=f"Test alert: {alert_type.value}",
            ai_score=0.85,
            price=100.0,
            change_pct=5.0,
            volume=1000000,
        )

        sound_file = self._get_default_sound(alert_type)
        alert.sound_played = self.audio.play(sound_file)

        self._recent_alerts.append(alert)
        return alert

    def clear_cooldowns(self) -> None:
        """Clear all cooldowns (for testing)."""
        self._cooldowns.clear()

    def get_stats(self) -> dict:
        """Get alert statistics."""
        now = datetime.utcnow()
        hour_ago = now - timedelta(hours=1)

        recent_hour = [
            a for a in self._recent_alerts
            if a.timestamp > hour_ago
        ]

        by_type = {}
        for alert in recent_hour:
            by_type[alert.alert_type.value] = by_type.get(alert.alert_type.value, 0) + 1

        return {
            "total_alerts": len(self._recent_alerts),
            "last_hour": len(recent_hour),
            "by_type": by_type,
            "active_cooldowns": len(self._cooldowns),
            "top_symbols": sorted(
                self._alert_counts.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:10],
        }
