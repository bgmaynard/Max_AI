"""
MAX_AI Scanner Client

This module provides a client for consuming MAX_AI_SCANNER data.
Copy this module to your bot project for integration.

Usage:
    from scanner_client import ScannerClient, ScannerRow

    async with ScannerClient() as client:
        rows = await client.get_rows("FAST_MOVERS", limit=25)
        for row in rows:
            print(f"{row.symbol}: {row.change_pct}% (score: {row.ai_score})")
"""

from .scanner_client import (
    ScannerClient,
    ScannerRow,
    SymbolContext,
    HaltInfo,
    ScannerHealthError,
)

__all__ = [
    "ScannerClient",
    "ScannerRow",
    "SymbolContext",
    "HaltInfo",
    "ScannerHealthError",
]
