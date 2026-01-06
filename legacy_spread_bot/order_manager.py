"""
Order Manager for Market Maker Bot

Manages grid orders, tracks open orders, and handles
order placement/cancellation with POST_ONLY mode for rebates.
"""

import logging
import time
import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum

import config

logger = logging.getLogger(__name__)


class OrderSide(Enum):
    BID = "bid"
    ASK = "ask"


@dataclass
class QuoteLevel:
    """A single quote level in the grid."""
    side: OrderSide
    price: float
    size: float
    level: int  # 1 = closest to mid
    order_id: Optional[str] = None
    placed_at: float = 0.0


@dataclass
class GridState:
    """Current state of the order grid."""
    bids: List[QuoteLevel] = field(default_factory=list)
    asks: List[QuoteLevel] = field(default_factory=list)
    last_update: float = 0.0
    fair_price: float = 0.0


class OrderManager:
    """
    Manages a grid of limit orders around the fair price.
    
    Features:
    - Multi-level bid/ask grid
    - POST_ONLY orders for maker rebates
    - Automatic stale order cancellation
    - Order tracking and management
    """
    
    def __init__(self, exchange, info):
        self.exchange = exchange
        self.info = info
        self.grid = GridState()
        
        # Config
        self.num_levels = getattr(config, 'MM_NUM_LEVELS', 3)
        self.spread_bps = getattr(config, 'MM_SPREAD_BPS', 8)
        self.quote_size_usd = getattr(config, 'MM_QUOTE_SIZE_USD', 50)
        self.post_only = getattr(config, 'MM_POST_ONLY', True)
        self.stale_threshold = 10  # Cancel orders older than 10 seconds
        
        # Order tracking
        self._active_orders: Dict[str, QuoteLevel] = {}
        self._last_cancel_all = 0
        
        logger.info(f"OrderManager initialized: {self.num_levels} levels, {self.spread_bps}bps spread")
    
    def calculate_grid_prices(self, fair_price: float, skew_bps: float = 0) -> GridState:
        """
        Calculate grid prices around fair price.
        
        Args:
            fair_price: Mid-market price
            skew_bps: Inventory skew in basis points
            
        Returns:
            GridState with calculated quote levels
        """
        grid = GridState(fair_price=fair_price, last_update=time.time())
        
        base_spread_bps = self.spread_bps
        
        for level in range(1, self.num_levels + 1):
            # Spread increases with each level
            level_spread_bps = base_spread_bps * level
            
            # Apply skew
            # Positive skew = shift quotes up (we want to sell)
            bid_spread = level_spread_bps + skew_bps
            ask_spread = level_spread_bps - skew_bps
            
            # Calculate prices
            bid_price = fair_price * (1 - bid_spread / 10000)
            ask_price = fair_price * (1 + ask_spread / 10000)
            
            # Size decreases with level (more at tight spreads)
            size_multiplier = 1.0 / level
            base_size = self.quote_size_usd / fair_price
            level_size = round(base_size * size_multiplier, 2)
            
            if level_size >= 0.1:  # Min size
                grid.bids.append(QuoteLevel(
                    side=OrderSide.BID,
                    price=round(bid_price, 5),
                    size=level_size,
                    level=level
                ))
                
                grid.asks.append(QuoteLevel(
                    side=OrderSide.ASK,
                    price=round(ask_price, 5),
                    size=level_size,
                    level=level
                ))
        
        return grid
    
    async def place_grid(self, fair_price: float, skew_bps: float = 0) -> bool:
        """
        Place/update the entire order grid.
        
        Args:
            fair_price: Current fair price
            skew_bps: Inventory skew in basis points
            
        Returns:
            True if grid was placed successfully
        """
        # Cancel existing orders first
        await self.cancel_all()
        
        # Calculate new grid
        new_grid = self.calculate_grid_prices(fair_price, skew_bps)
        
        if getattr(config, 'DRY_RUN', False):
            logger.info(f"DRY RUN - Would place grid:")
            for bid in new_grid.bids:
                logger.info(f"  BID L{bid.level}: {bid.size:.2f} @ ${bid.price:.4f}")
            for ask in new_grid.asks:
                logger.info(f"  ASK L{ask.level}: {ask.size:.2f} @ ${ask.price:.4f}")
            self.grid = new_grid
            return True
        
        # Place all orders
        orders_to_place = []
        
        for bid in new_grid.bids:
            orders_to_place.append((bid, True))  # is_buy = True
        
        for ask in new_grid.asks:
            orders_to_place.append((ask, False))  # is_buy = False
        
        # Place orders concurrently
        loop = asyncio.get_event_loop()
        tasks = []
        
        for quote, is_buy in orders_to_place:
            task = loop.run_in_executor(None, self._place_order_sync, quote, is_buy)
            tasks.append((quote, task))
        
        # Wait for all orders
        success_count = 0
        for quote, task in tasks:
            try:
                result = await task
                if self._check_order_result(result, quote):
                    success_count += 1
            except Exception as e:
                logger.warning(f"Order placement failed: {e}")
        
        logger.info(f"📊 Grid placed: {success_count}/{len(orders_to_place)} orders successful")
        
        self.grid = new_grid
        return success_count > 0
    
    def _place_order_sync(self, quote: QuoteLevel, is_buy: bool) -> Dict:
        """Place a single order synchronously."""
        order_type = {"limit": {"tif": "Alo"}} if self.post_only else {"limit": {"tif": "Gtc"}}
        
        try:
            result = self.exchange.order(
                name=config.SPOT_SYMBOL,
                is_buy=is_buy,
                sz=quote.size,
                limit_px=quote.price,
                order_type=order_type
            )
            return result
        except Exception as e:
            logger.error(f"Order error: {e}")
            return {"status": "error", "error": str(e)}
    
    def _check_order_result(self, result: Dict, quote: QuoteLevel) -> bool:
        """Check if order was placed successfully."""
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if "resting" in s:
                    order_id = s["resting"].get("oid")
                    quote.order_id = str(order_id)
                    quote.placed_at = time.time()
                    self._active_orders[quote.order_id] = quote
                    logger.debug(f"Order placed: {quote.side.value} L{quote.level} @ ${quote.price:.4f}")
                    return True
                elif "filled" in s:
                    # POST_ONLY should not fill, but if it does...
                    logger.info(f"Order filled immediately: {quote.side.value} @ ${quote.price:.4f}")
                    return True
                elif "error" in s:
                    error = s.get("error", "Unknown error")
                    # POST_ONLY rejection is expected when crossing
                    if "would cross" in error.lower() or "post only" in error.lower():
                        logger.debug(f"POST_ONLY rejected (would cross): {quote.side.value} @ ${quote.price:.4f}")
                    else:
                        logger.warning(f"Order error: {error}")
                    return False
        
        logger.warning(f"Order failed: {result}")
        return False
    
    async def cancel_all(self) -> bool:
        """
        Cancel all open orders.
        
        Returns:
            True if cancellation was successful
        """
        if getattr(config, 'DRY_RUN', False):
            logger.debug("DRY RUN - Would cancel all orders")
            self._active_orders.clear()
            return True
        
        try:
            loop = asyncio.get_event_loop()
            
            # Get open orders
            def get_orders():
                return self.info.open_orders(config.ACCOUNT_ADDRESS)
            
            open_orders = await loop.run_in_executor(None, get_orders)
            
            # Filter spot orders
            spot_orders = [o for o in open_orders if o.get("coin") == config.SPOT_SYMBOL]
            
            if not spot_orders:
                logger.debug("No orders to cancel")
                return True
            
            # Cancel each order
            cancel_tasks = []
            for order in spot_orders:
                oid = order.get("oid")
                if oid:
                    def cancel_order(order_id):
                        return self.exchange.cancel(config.SPOT_SYMBOL, order_id)
                    cancel_tasks.append(loop.run_in_executor(None, cancel_order, oid))
            
            if cancel_tasks:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)
                logger.debug(f"Cancelled {len(cancel_tasks)} orders")
            
            self._active_orders.clear()
            self._last_cancel_all = time.time()
            return True
            
        except Exception as e:
            logger.error(f"Cancel all failed: {e}")
            return False
    
    async def cancel_stale_orders(self) -> int:
        """
        Cancel orders older than stale threshold.
        
        Returns:
            Number of orders cancelled
        """
        now = time.time()
        stale_orders = [
            oid for oid, quote in self._active_orders.items()
            if now - quote.placed_at > self.stale_threshold
        ]
        
        if not stale_orders:
            return 0
        
        cancelled = 0
        for oid in stale_orders:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, 
                    lambda: self.exchange.cancel(config.SPOT_SYMBOL, int(oid))
                )
                del self._active_orders[oid]
                cancelled += 1
            except Exception as e:
                logger.warning(f"Failed to cancel stale order {oid}: {e}")
        
        if cancelled:
            logger.debug(f"Cancelled {cancelled} stale orders")
        
        return cancelled
    
    def get_active_order_count(self) -> int:
        """Get number of active orders."""
        return len(self._active_orders)
    
    def get_grid_summary(self) -> str:
        """Get a summary of current grid."""
        if not self.grid.bids and not self.grid.asks:
            return "No grid active"
        
        best_bid = max(self.grid.bids, key=lambda x: x.price).price if self.grid.bids else 0
        best_ask = min(self.grid.asks, key=lambda x: x.price).price if self.grid.asks else 0
        
        return f"Grid: {len(self.grid.bids)} bids (best ${best_bid:.4f}) | {len(self.grid.asks)} asks (best ${best_ask:.4f})"
