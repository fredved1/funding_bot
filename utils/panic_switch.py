"""
PanicSwitch - Emergency Position Closure

Dead man's switch that market dumps ALL positions via REST API.
Called when:
- Manual trigger
- Unrecoverable error
- Watchdog timeout after reconnect fails
"""

import asyncio
import logging
from uuid import uuid4

from core.state import StateConfig

logger = logging.getLogger(__name__)


class PanicSwitch:
    """
    Emergency close all positions via REST API.
    
    Uses aggressive slippage to ensure fills.
    """
    
    def __init__(self, client):
        """
        Args:
            client: HyperliquidClient instance
        """
        self.client = client
        self.panic_slippage = 0.05  # 5% - Accept bad fills to exit fast
    
    async def emergency_close_all(self) -> bool:
        """
        Market dump ALL positions. No questions asked.
        
        Returns:
            True if all positions closed successfully
        """
        logger.critical("🚨🚨🚨 PANIC SWITCH ACTIVATED 🚨🚨🚨")
        
        state = StateConfig.get()
        
        if not state.positions:
            logger.info("No positions to close")
            return True
        
        success = True
        
        for coin, pos in list(state.positions.items()):
            logger.warning(f"💣 Emergency closing {coin}: Spot {pos.spot_size}, Perp {pos.perp_size}")
            
            try:
                # Get current prices
                prices = await self.client.get_prices(coin)
                spot_price = prices.get("spot_bid", pos.entry_price_spot)
                perp_price = prices.get("perp_ask", pos.entry_price_perp)
                
                # Aggressive limit prices for market-like execution
                spot_limit = round(spot_price * (1 - self.panic_slippage), 5)  # Sell cheap
                perp_limit = round(perp_price * (1 + self.panic_slippage), 5)  # Buy high
                
                # Close both legs simultaneously
                results = await asyncio.gather(
                    self._close_spot(coin, pos.spot_size, spot_limit),
                    self._close_perp(coin, pos.perp_size, perp_limit),
                    return_exceptions=True
                )
                
                spot_ok = not isinstance(results[0], Exception) and results[0]
                perp_ok = not isinstance(results[1], Exception) and results[1]
                
                if spot_ok and perp_ok:
                    state.remove_position(coin)
                    logger.info(f"✅ Closed {coin}")
                else:
                    logger.error(f"❌ Failed to fully close {coin}: spot={spot_ok} perp={perp_ok}")
                    success = False
                    
            except Exception as e:
                logger.error(f"Panic close error for {coin}: {e}")
                success = False
        
        return success
    
    async def _close_spot(self, coin: str, size: float, price: float) -> bool:
        """Sell spot position."""
        try:
            result = await asyncio.wait_for(
                self.client.place_order(coin, "spot", False, size, price, uuid4().hex),
                timeout=10.0
            )
            return result.get("status") == "filled"
        except Exception as e:
            logger.error(f"Spot close failed: {e}")
            return False
    
    async def _close_perp(self, coin: str, size: float, price: float) -> bool:
        """Close perp short (buy back)."""
        try:
            result = await asyncio.wait_for(
                self.client.place_order(coin, "perp", True, size, price, uuid4().hex),
                timeout=10.0
            )
            return result.get("status") == "filled"
        except Exception as e:
            logger.error(f"Perp close failed: {e}")
            return False
    
    async def close_single(self, coin: str) -> bool:
        """Emergency close a single position."""
        state = StateConfig.get()
        pos = state.get_position(coin)
        
        if not pos:
            return True
        
        logger.warning(f"💣 Emergency closing {coin}")
        
        try:
            prices = await self.client.get_prices(coin)
            
            results = await asyncio.gather(
                self._close_spot(coin, pos.spot_size, prices["spot_bid"] * 0.95),
                self._close_perp(coin, pos.perp_size, prices["perp_ask"] * 1.05),
                return_exceptions=True
            )
            
            if all(not isinstance(r, Exception) and r for r in results):
                state.remove_position(coin)
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Close single failed: {e}")
            return False
