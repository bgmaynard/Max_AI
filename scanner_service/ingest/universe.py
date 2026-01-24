"""Universe management - symbol filtering and narrowing."""

import logging
from typing import Optional
from datetime import datetime, time

from scanner_service.settings import get_settings
from scanner_service.schemas.market_snapshot import Quote

logger = logging.getLogger(__name__)


# Core NASDAQ/NYSE actively traded stocks (seed universe)
SEED_UNIVERSE = [
    # Tech mega-caps
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA", "AMD", "INTC",
    "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "QCOM", "TXN", "NOW", "AMAT", "MU",
    "LRCX", "KLAC", "SNPS", "CDNS", "MRVL", "ADI", "NXPI", "PANW", "CRWD", "FTNT",

    # Retail / Consumer
    "WMT", "COST", "HD", "TGT", "LOW", "SBUX", "MCD", "NKE", "LULU", "DECK",

    # Finance
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V", "MA", "PYPL",

    # Healthcare / Biotech
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "BMY", "AMGN", "GILD", "BIIB",
    "MRNA", "REGN", "VRTX", "ISRG", "DXCM", "ILMN",

    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "PXD", "OXY", "DVN", "HAL", "MPC",

    # Industrial
    "CAT", "DE", "HON", "UPS", "FDX", "BA", "RTX", "LMT", "GE", "MMM",

    # Popular momentum/meme stocks
    "PLTR", "SOFI", "RIVN", "LCID", "NIO", "COIN", "HOOD", "RBLX", "SNOW", "DKNG",
    "ROKU", "SQ", "SHOP", "MELI", "SE", "PINS", "SNAP", "TWLO", "ZM", "DOCU",

    # Small Cap / Momentum / Day Trading Favorites
    "SOUN", "SMCI", "IONQ", "RGTI", "QUBT", "KULR", "LUNR", "RKLB", "ASTS", "MNTS",
    "APLD", "MARA", "RIOT", "CLSK", "BITF", "HUT", "WULF", "CIFR", "CORZ", "BTBT",
    "BBAI", "BFRG", "GEVO", "PLUG", "FCEL", "BE", "BLNK", "CHPT", "EVGO", "DCFC",
    "DNA", "CRSP", "BEAM", "EDIT", "NTLA", "VERV", "RXRX", "SDGR", "TALK", "HIMS",
    "NKLA", "GOEV", "FFIE", "WKHS", "RIDE", "FSR", "PSNY", "VFS", "PTRA", "LEV",
    "SNDL", "TLRY", "CGC", "ACB", "CRON", "HEXO", "OGI", "GRWG", "CURLF", "TCNNF",
    "GME", "AMC", "BBBY", "BB", "EXPR", "KOSS", "CLOV", "WISH", "WKME", "OPEN",
    "UPST", "AFRM", "LMND", "ROOT", "SKLZ", "FUBO", "GENI", "DNUT", "BZFD", "COUR",
    "PATH", "AI", "BIGC", "FVRR", "ETSY", "CHWY", "W", "CVNA", "DASH", "ABNB",
    "GRAB", "CPNG", "GLBE", "DLO", "PAYO", "BILL", "PCOR", "GDRX", "HEPS", "LFST",

    # Biotech Small Caps (High Volatility)
    "SAVA", "SRPT", "RARE", "BLUE", "SGMO", "FATE", "KYMR", "TGTX", "IMVT", "RVMD",
    "ARQT", "BCYC", "XENE", "CRNX", "KRYS", "ALEC", "TVTX", "GTHX", "KROS", "ADVM",
    "RLAY", "PCRX", "RCKT", "FOLD", "VCEL", "ORIC", "PHAT", "AKRO", "ANNX", "DAWN",

    # Speculative / Recent IPOs / SPACs
    "HOOD", "RBAC", "CFVI", "DWAC", "PHUN", "MARK", "BKKT", "SOFI", "DNA", "JOBY",
    "EVTL", "LILM", "ACHR", "BLDE", "SPCE", "ASTR", "RDW", "VORB", "IRDM", "BKSY",

    # Chinese ADRs (High Volume)
    "BABA", "JD", "PDD", "BIDU", "NTES", "BILI", "TME", "XPEV", "LI", "ZK",
    "FUTU", "TIGR", "DIDI", "TAL", "EDU", "VNET", "KC", "YMM", "TUYA", "DOYU",

    # Penny Stocks / Sub-$5 High Volume Day Trading
    "MULN", "VXRT", "BNGO", "SENS", "CTRM", "NAKD", "ZOM", "GNUS", "SHIP", "IDEX",
    "OCGN", "ATOS", "CLOV", "PROG", "BBIG", "ATER", "SPRT", "XELA", "MMAT", "TRCH",
    "CEI", "FAMI", "KPLT", "ANY", "BTTX", "SOBR", "VISL", "SINT", "EEIQ", "BIOR",
    "IMPP", "INDO", "HCDI", "ENSV", "MEGL", "GNS", "TPST", "ATNF", "VERB", "KAVL",
    "BOXD", "BBAI", "PRTY", "NILE", "GOVX", "VERU", "ADTX", "AGRI", "TIRX", "CXAI",
    "SXTC", "JSPR", "TOP", "ATXI", "PIXY", "UTRS", "BHAT", "JZXN", "MGAM", "PETZ",
    "BTCS", "APRE", "YGTY", "EVOK", "MBOT", "RSLS", "CNET", "FBIO", "BFRG", "LQDA",
    "CRGE", "DRUG", "ALLR", "CRBP", "MYSZ", "SEEL", "OTRK", "IMRN", "ZKIN", "PCT",

    # Active Day Trading Penny Stocks (DTD movers)
    "DRCT", "KUST", "RVYL", "REVB", "YYAI", "CNEY", "GXAI", "WBUY", "NVNI", "SOPA", "CXDO",
    "LITM", "BEEM", "MGOL", "SVMH", "MDIA", "WISA", "BXRX", "SNGX", "ZCAR", "OBLG",
    "OPTT", "BKYI", "SLRX", "VNET", "CTXR", "CISS", "SASI", "NNVC", "VTGN", "PBTS",
    "WINT", "MDGS", "BDRX", "RNAZ", "HPCO", "DFFN", "SIEN", "NRXP", "GROM", "COSM",
    "SILO", "EOSE", "AIRT", "VEEE", "SATL", "EEIQ", "PXMD", "ENVB", "NCPL", "EDBL",
    "PALI", "CLVS", "AMST", "BSFC", "PRPH", "XTIA", "FGEN", "NUVB", "STSS", "COMS",
    "SFIX", "LMFA", "BSEM", "LGMK", "XFOR", "CREG", "USEA", "HOLO", "VMAR", "DPRO",
    "ONMD", "LUCY", "SYTA", "EFTR", "BWMX", "CRKN", "CDIO", "NXGL", "MNPR", "SMFL",

    # Recent IPO / SPAC Penny Stocks
    "RNXT", "SNAX", "GLUE", "SPRC", "SBFM", "BARK", "HLAH", "STRY", "AEVA", "VIEW",

    # OTC/Pink Sheet Popular (if available via Schwab)
    "EEENF", "OZSC", "DPLS", "MINE", "HCMC", "HMBL", "TSNP", "AITX", "GNUS", "BBRW",

    # ETFs for reference
    "SPY", "QQQ", "IWM", "DIA", "VTI", "ARKK", "XLF", "XLE", "XLK", "SOXL",
]


class UniverseManager:
    """
    Manages the symbol universe for scanning.

    Implements universe narrowing to focus on active candidates
    rather than brute-force scanning all symbols.
    """

    def __init__(self):
        self.settings = get_settings()
        self._universe: list[str] = SEED_UNIVERSE.copy()
        self._active_candidates: list[str] = []
        self._last_refresh: Optional[datetime] = None

    @property
    def universe(self) -> list[str]:
        """Get full universe of symbols."""
        return self._universe

    @property
    def candidates(self) -> list[str]:
        """Get narrowed active candidates."""
        return self._active_candidates or self._universe[: self.settings.max_watch_symbols]

    def add_symbols(self, symbols: list[str]) -> None:
        """Add symbols to the universe."""
        for symbol in symbols:
            symbol = symbol.upper().strip()
            if symbol and symbol not in self._universe:
                self._universe.append(symbol)
        logger.info(f"Universe now contains {len(self._universe)} symbols")

    def remove_symbols(self, symbols: list[str]) -> None:
        """Remove symbols from the universe."""
        symbols_upper = {s.upper().strip() for s in symbols}
        self._universe = [s for s in self._universe if s not in symbols_upper]

    def narrow_universe(self, quotes: dict[str, Quote]) -> list[str]:
        """
        Narrow universe to active candidates based on real-time data.

        Criteria for narrowing:
        - Volume > minimum threshold
        - Price within acceptable range
        - Has recent activity (bid/ask present)
        - Relative volume indicates interest
        """
        candidates = []

        for symbol, quote in quotes.items():
            # Skip if no meaningful data
            if quote.last_price == 0:
                continue

            # Volume filter (higher for penny stocks)
            min_volume = 100000 if quote.last_price < 1.0 else 50000
            if quote.volume < min_volume:
                continue

            # Price filter (allow penny stocks down to $0.10)
            if quote.last_price < 0.10 or quote.last_price > 1000:
                continue

            # Must have bid/ask (liquid)
            if quote.bid == 0 or quote.ask == 0:
                continue

            # Spread filter (more lenient for penny stocks)
            max_spread = 5.0 if quote.last_price < 1.0 else 2.0
            if quote.spread > max_spread:
                continue

            # Some activity criteria
            has_movement = abs(quote.change_pct) > 0.1  # At least 0.1% move
            has_volume = quote.rvol > 0.3  # At least 30% of avg volume so far

            if has_movement or has_volume:
                candidates.append(symbol)

        # Sort by activity (change % * rvol) and limit
        candidates.sort(
            key=lambda s: abs(quotes[s].change_pct) * max(quotes[s].rvol, 0.1),
            reverse=True,
        )

        max_symbols = self.settings.max_watch_symbols
        self._active_candidates = candidates[:max_symbols]
        self._last_refresh = datetime.utcnow()

        logger.info(
            f"Narrowed universe: {len(quotes)} -> {len(self._active_candidates)} candidates"
        )
        return self._active_candidates

    def is_market_hours(self) -> bool:
        """Check if current time is within market hours (EST)."""
        now = datetime.utcnow()
        # Convert to EST (UTC-5, ignoring DST for simplicity)
        est_hour = (now.hour - 5) % 24

        market_open = time(9, 30)
        market_close = time(16, 0)

        current_time = time(est_hour, now.minute)
        return market_open <= current_time <= market_close

    def get_premarket_movers(self, quotes: dict[str, Quote], limit: int = 50) -> list[str]:
        """Get top premarket movers by gap percentage."""
        movers = []
        for symbol, quote in quotes.items():
            if abs(quote.gap_pct) > 1.0 and quote.volume > 10000:
                movers.append((symbol, quote.gap_pct))

        movers.sort(key=lambda x: abs(x[1]), reverse=True)
        return [s for s, _ in movers[:limit]]
