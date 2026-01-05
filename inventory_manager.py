"""
Inventory Manager for Market Maker Bot

Tracks position size, calculates inventory skew factors,
and manages delta hedging via perpetual contracts.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any
import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class InventoryState:
    """Current inventory state."""
    spot_balance: float = 0.0      # HYPE tokens held
    spot_value_usd: float = 0.0    # Value in USD
    perp_position: float = 0.0     # Perp position (negative = short)
    perp_value_usd: float = 0.0    # Perp value in USD
    net_delta: float = 0.0         # Net exposure (spot + perp)
    last_update: float = 0.0


class InventoryManager:
    """
    Manages inventory for market making.
    
    Key responsibilities:
    1. Track spot token balance
    2. Track perpetual position
    3. Calculate inventory skew for quote adjustment
    4. Execute hedges when inventory exceeds threshold
    """
    
    def __init__(self, exchange, info):
        self.exchange = exchange
        self.info = info
        self.state = InventoryState()
        
        # Config
        self.max_inventory_usd = getattr(config, 'MM_MAX_INVENTORY_USD', 500)
        self.skew_factor = getattr(config, 'MM_SKEW_FACTOR', 0.5)
        self.hedge_threshold = getattr(config, 'MM_HEDGE_THRESHOLD_USD', 300)
        
        # Cache
        self._last_sync = 0
        self._sync_interval = 5  # Sync every 5 seconds
        
        logger.info(f"InventoryManager initialized: max=${self.max_inventory_usd}, hedge@${self.hedge_threshold}")
    
    def sync_state(self, current_price: float) -> InventoryState:
        """
        Sync inventory state with exchange.
        
        Args:
            current_price: Current HYPE price for value calculation
        """
        now = time.time()
        
        # Rate limit syncs
        if now - self._last_sync < self._sync_interval:
            return self.state
        
        try:
            # Get spot balance
            spot_state = requests.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'spotClearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5
            ).json()
            
            spot_balance = 0.0
            for balance in spot_state.get('balances', []):
                if balance.get('coin') == 'HYPE':
                    spot_balance = float(balance.get('total', 0))
                    break
            
            # Get perp position
            perp_state = requests.post(
                'https://api.hyperliquid.xyz/info',
                json={'type': 'clearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5
            ).json()
            
            perp_position = 0.0
            for pos in perp_state.get('assetPositions', []):
                if pos.get('position', {}).get('coin') == config.PERP_SYMBOL:
                    perp_position = float(pos['position'].get('szi', 0))
                    break
            
            # Update state
            self.state = InventoryState(
                spot_balance=spot_balance,
                spot_value_usd=spot_balance * current_price,
                perp_position=perp_position,
                perp_value_usd=perp_position * current_price,
                net_delta=(spot_balance + perp_position) * current_price,
                last_update=now
            )
            
            self._last_sync = now
            
            logger.debug(f"Inventory synced: spot={spot_balance:.2f} HYPE, perp={perp_position:.2f}, net_delta=${self.state.net_delta:.2f}")
            
        except Exception as e:
            logger.warning(f"Inventory sync failed: {e}")
        
        return self.state
    
    def get_skew_bps(self, current_price: float) -> float:
        """
        Calculate quote skew in basis points based on inventory.
        
        Positive skew = shift quotes up (more aggressive selling)
        Negative skew = shift quotes down (more aggressive buying)
        
        Args:
            current_price: Current HYPE price
            
        Returns:
            Skew in basis points to apply to quotes
        """
        self.sync_state(current_price)
        
        if self.max_inventory_usd == 0:
            return 0.0
        
        # Calculate inventory ratio (-1 to +1)
        inventory_ratio = self.state.net_delta / self.max_inventory_usd
        inventory_ratio = max(-1.0, min(1.0, inventory_ratio))  # Clamp
        
        # Convert to basis points with skew factor
        # At max inventory, skew is MM_SPREAD_BPS * skew_factor
        max_skew = getattr(config, 'MM_SPREAD_BPS', 8) * self.skew_factor
        skew_bps = inventory_ratio * max_skew
        
        return skew_bps
    
    def should_hedge(self, current_price: float) -> bool:
        """
        Check if inventory exceeds hedge threshold.
        
        Args:
            current_price: Current HYPE price
            
        Returns:
            True if hedging is needed
        """
        self.sync_state(current_price)
        
        # Only hedge spot inventory that exceeds threshold
        return abs(self.state.spot_value_usd) > self.hedge_threshold
    
    async def execute_hedge(self, current_price: float) -> bool:
        """
        Execute delta hedge via perpetual contract.
        
        Sells perp to offset long spot inventory, or
        buys perp to offset short spot exposure.
        
        Args:
            current_price: Current HYPE price
            
        Returns:
            True if hedge was executed successfully
        """
        self.sync_state(current_price)
        
        if not self.should_hedge(current_price):
            logger.debug("No hedge needed")
            return False
        
        # Calculate hedge size
        # We want to reduce net_delta towards 0
        hedge_size = abs(self.state.spot_balance)
        
        if hedge_size < 0.1:  # Minimum size
            return False
        
        # Determine direction
        # If we're long spot, we short perp
        is_buy = self.state.spot_balance < 0
        
        logger.info(f"🔒 HEDGING: {'Buy' if is_buy else 'Sell'} {hedge_size:.2f} HYPE perp to hedge spot")
        
        if getattr(config, 'DRY_RUN', False):
            logger.info("DRY RUN - Hedge not executed")
            return False
        
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            
            # Place hedge order with aggresive pricing
            slippage = 0.002  # 0.2% slippage
            if is_buy:
                price = round(current_price * (1 + slippage), 5)
            else:
                price = round(current_price * (1 - slippage), 5)
            
            def place_hedge():
                return self.exchange.order(
                    name=config.PERP_SYMBOL,
                    is_buy=is_buy,
                    sz=hedge_size,
                    limit_px=price,
                    order_type={"limit": {"tif": "Ioc"}}
                )
            
            result = await loop.run_in_executor(None, place_hedge)
            
            if result.get("status") == "ok":
                logger.info(f"✅ Hedge executed: {hedge_size:.2f} HYPE perp")
                self._last_sync = 0  # Force resync
                return True
            else:
                logger.warning(f"Hedge failed: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Hedge error: {e}")
            return False
    
    def get_remaining_capacity(self, current_price: float, side: str) -> float:
        """
        Get remaining capacity for a given side.
        
        Args:
            current_price: Current HYPE price
            side: 'buy' or 'sell'
            
        Returns:
            Remaining capacity in HYPE
        """
        self.sync_state(current_price)
        
        if side == 'buy':
            # How much more can we buy?
            remaining_usd = self.max_inventory_usd - self.state.spot_value_usd
        else:
            # How much can we sell?
            remaining_usd = self.max_inventory_usd + self.state.spot_value_usd
        
        remaining_hype = max(0, remaining_usd / current_price)
        return remaining_hype
    
    def is_at_limit(self, current_price: float, side: str) -> bool:
        """
        Check if we're at inventory limit for a side.
        
        Args:
            current_price: Current HYPE price
            side: 'buy' or 'sell'
            
        Returns:
            True if at limit
        """
        return self.get_remaining_capacity(current_price, side) < 1.0  # Less than 1 HYPE
