"""
StateConfig - In-Memory State Singleton

Single source of truth for real-time position and margin state.
NEVER persists to disk. Always reconcile from Exchange API on startup.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, ClassVar

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open delta-neutral position."""
    coin: str
    spot_size: float
    perp_size: float
    entry_price_spot: float
    entry_price_perp: float
    entry_time: float = field(default_factory=time.time)
    
    @property
    def size_usd(self) -> float:
        return self.spot_size * self.entry_price_spot


@dataclass
class PendingOrder:
    """Tracks an in-flight order."""
    cloid: str
    coin: str
    side: str  # "spot" or "perp"
    is_buy: bool
    size: float
    price: float
    created_at: float = field(default_factory=time.time)


@dataclass
class StateConfig:
    """
    In-memory singleton for all real-time state.
    
    Rules:
    - NEVER touch disk in hot path
    - Modified only by ExecutionGuard and MarginMonitor
    - Read by Strategy and Dashboard
    """
    
    # Position tracking
    positions: Dict[str, Position] = field(default_factory=dict)
    pending_orders: Dict[str, PendingOrder] = field(default_factory=dict)
    
    # Safety metrics (updated every tick)
    margin_ratio: float = 1.0
    last_price_update: float = 0.0
    
    # Capital tracking
    available_buffer_usd: float = 0.0
    total_exposure_usd: float = 0.0
    spot_balance_usdc: float = 0.0
    perp_margin_usdc: float = 0.0
    
    # Funding tracking
    last_funding_check: float = 0.0
    current_funding_rate: float = 0.0
    
    # Singleton instance
    _instance: ClassVar[Optional['StateConfig']] = None
    
    @classmethod
    def get(cls) -> 'StateConfig':
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset(cls) -> 'StateConfig':
        """Reset state for testing or reconciliation."""
        cls._instance = cls()
        return cls._instance
    
    def has_position(self, coin: str) -> bool:
        """Check if we have an open position for a coin."""
        return coin in self.positions
    
    def get_position(self, coin: str) -> Optional[Position]:
        """Get position for a coin."""
        return self.positions.get(coin)
    
    def add_position(self, position: Position):
        """Add a new position."""
        self.positions[position.coin] = position
        self._update_exposure()
        logger.info(f"📥 Added position: {position.coin} {position.spot_size}")
    
    def remove_position(self, coin: str):
        """Remove a closed position."""
        if coin in self.positions:
            del self.positions[coin]
            self._update_exposure()
            logger.info(f"📤 Removed position: {coin}")
    
    def update_position_size(self, coin: str, new_spot_size: float, new_perp_size: float):
        """Update position sizes after partial close."""
        if coin in self.positions:
            self.positions[coin].spot_size = new_spot_size
            self.positions[coin].perp_size = new_perp_size
            self._update_exposure()
    
    def _update_exposure(self):
        """Recalculate total exposure."""
        self.total_exposure_usd = sum(
            pos.size_usd for pos in self.positions.values()
        )
    
    def add_pending_order(self, order: PendingOrder):
        """Track an in-flight order."""
        self.pending_orders[order.cloid] = order
    
    def remove_pending_order(self, cloid: str):
        """Remove a completed/cancelled order."""
        if cloid in self.pending_orders:
            del self.pending_orders[cloid]
    
    def get_summary(self) -> dict:
        """Get state summary for logging/dashboard."""
        return {
            "positions": len(self.positions),
            "total_exposure_usd": round(self.total_exposure_usd, 2),
            "margin_ratio": round(self.margin_ratio, 4),
            "buffer_usd": round(self.available_buffer_usd, 2),
            "pending_orders": len(self.pending_orders),
            "funding_rate": self.current_funding_rate
        }
