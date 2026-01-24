"""Scanner state management."""

import logging
from datetime import datetime
from typing import Optional
from enum import Enum

from scanner_service.schemas.market_snapshot import MarketSnapshot
from scanner_service.schemas.events import ScannerOutput

logger = logging.getLogger(__name__)


class ScannerStatus(str, Enum):
    """Scanner operational status."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


class ScannerState:
    """
    Centralized state management for the scanner service.

    Tracks operational status, metrics, and recent data.
    """

    def __init__(self):
        self._status = ScannerStatus.STOPPED
        self._started_at: Optional[datetime] = None
        self._last_scan: Optional[datetime] = None
        self._last_snapshot: Optional[MarketSnapshot] = None
        self._last_outputs: dict[str, ScannerOutput] = {}

        # Metrics
        self._scan_count = 0
        self._error_count = 0
        self._total_scan_time_ms = 0.0
        self._symbols_scanned = 0

    @property
    def status(self) -> ScannerStatus:
        """Get current scanner status."""
        return self._status

    @status.setter
    def status(self, value: ScannerStatus) -> None:
        """Set scanner status."""
        old_status = self._status
        self._status = value
        logger.info(f"Scanner status: {old_status} -> {value}")

        if value == ScannerStatus.RUNNING and self._started_at is None:
            self._started_at = datetime.utcnow()

    @property
    def is_running(self) -> bool:
        """Check if scanner is actively running."""
        return self._status == ScannerStatus.RUNNING

    def record_scan(
        self,
        snapshot: MarketSnapshot,
        outputs: dict[str, ScannerOutput],
    ) -> None:
        """Record a completed scan."""
        self._last_scan = datetime.utcnow()
        self._last_snapshot = snapshot
        self._last_outputs = outputs
        self._scan_count += 1
        self._symbols_scanned += len(snapshot)

        if snapshot.scan_duration_ms:
            self._total_scan_time_ms += snapshot.scan_duration_ms

    def record_error(self, error: Exception) -> None:
        """Record a scan error."""
        self._error_count += 1
        logger.error(f"Scan error recorded: {error}")

    def get_snapshot(self) -> Optional[MarketSnapshot]:
        """Get most recent snapshot."""
        return self._last_snapshot

    def get_output(self, profile: str) -> Optional[ScannerOutput]:
        """Get most recent output for a profile."""
        return self._last_outputs.get(profile)

    def get_all_outputs(self) -> dict[str, ScannerOutput]:
        """Get all recent outputs."""
        return self._last_outputs.copy()

    def get_metrics(self) -> dict:
        """Get scanner metrics."""
        uptime = None
        if self._started_at:
            uptime = (datetime.utcnow() - self._started_at).total_seconds()

        avg_scan_time = 0
        if self._scan_count > 0:
            avg_scan_time = self._total_scan_time_ms / self._scan_count

        return {
            "status": self._status.value,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "uptime_seconds": uptime,
            "last_scan": self._last_scan.isoformat() if self._last_scan else None,
            "scan_count": self._scan_count,
            "error_count": self._error_count,
            "error_rate": self._error_count / max(self._scan_count, 1),
            "symbols_scanned": self._symbols_scanned,
            "avg_scan_time_ms": avg_scan_time,
            "profiles_active": list(self._last_outputs.keys()),
        }

    def reset_metrics(self) -> None:
        """Reset all metrics (for testing)."""
        self._scan_count = 0
        self._error_count = 0
        self._total_scan_time_ms = 0.0
        self._symbols_scanned = 0

    def clear(self) -> None:
        """Clear all state."""
        self._last_snapshot = None
        self._last_outputs.clear()
        self.reset_metrics()
