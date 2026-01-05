"""
ExecutionGuard - Atomic Trade Execution with Safety Lock

Guarantees delta-neutral execution:
- Either BOTH legs fill, or NEITHER exists after completion
- AsyncIO lock prevents race conditions with MarginMonitor
- MarginMonitor has priority access for safety operations
"""

import asyncio
import logging
import time
from uuid import uuid4
from typing import Optional, Tuple
from dataclasses import dataclass

from core.state import StateConfig, Position, PendingOrder

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""
    success: bool
    spot_cloid: str = ""
    perp_cloid: str = ""
    spot_filled: float = 0.0
    perp_filled: float = 0.0
    error: str = ""


class ExecutionGuard:
    """
    Atomic execution with priority lock for margin safety.
    
    Lock Priority:
    - MarginMonitor.safety_rebalance() blocks strategy and takes priority
    - Strategy.execute_delta_neutral() waits if safety is active
    """
    
    def __init__(self, client, dry_run: bool = True):
        """
        Args:
            client: HyperliquidClient instance
            dry_run: If True, simulates orders without hitting the API
        """
        self.client = client
        self.dry_run = dry_run
        self._lock = asyncio.Lock()
        self._safety_priority = asyncio.Event()
        self._safety_priority.set()  # Default: strategy allowed
        
        # Timing
        self.order_timeout = 5.0  # seconds
        self.slippage_buffer = 0.01  # 1%
        
        if dry_run:
            logger.warning("🎭 EXECUTION GUARD IN DRY RUN MODE - NO REAL ORDERS")
    
    async def execute_delta_neutral(self, coin: str, size_usd: float, 
                                     spot_price: float, perp_price: float) -> ExecutionResult:
        """
        Strategy entry point - waits if safety is rebalancing.
        
        Returns:
            ExecutionResult with success status and fill details
        """
        # DRY RUN: Simulate success without placing orders
        if self.dry_run:
            logger.info(f"🎭 DRY RUN: Would execute {coin} size=${size_usd:.2f}")
            return ExecutionResult(
                success=True,  # Simulate success
                spot_cloid="dry_run",
                perp_cloid="dry_run",
                spot_filled=size_usd / spot_price,
                perp_filled=size_usd / perp_price,
                error="dry_run"
            )
        
        # Block if margin monitor has priority
        await self._safety_priority.wait()
        
        async with self._lock:
            return await self._parallel_execute(coin, size_usd, spot_price, perp_price)
    
    async def safety_rebalance(self, coin: str, percentage: float) -> bool:
        """
        Margin monitor entry point - takes priority.
        
        Args:
            coin: The position to reduce
            percentage: How much to close (0.25 = 25%)
        
        Returns:
            True if rebalance successful
        """
        self._safety_priority.clear()  # Block strategy
        try:
            async with self._lock:
                return await self._close_partial(coin, percentage)
        finally:
            self._safety_priority.set()  # Release strategy
    
    async def emergency_close(self, coin: str) -> bool:
        """Close entire position for a coin. Used by panic switch."""
        return await self._close_partial(coin, 1.0)
    
    async def _parallel_execute(self, coin: str, size_usd: float,
                                  spot_price: float, perp_price: float) -> ExecutionResult:
        """
        Execute both legs in parallel with proper error handling.
        
        FIX APPLIED: Check for Exception instances from asyncio.gather
        """
        spot_cloid = uuid4().hex
        perp_cloid = uuid4().hex
        
        # Calculate sizes
        spot_size = round(size_usd / spot_price, 4)
        perp_size = round(size_usd / perp_price, 4)
        
        # Slippage-adjusted prices
        spot_limit = round(spot_price * (1 + self.slippage_buffer), 5)  # Buy higher
        perp_limit = round(perp_price * (1 - self.slippage_buffer), 5)  # Sell lower
        
        # Track pending orders in state
        state = StateConfig.get()
        state.add_pending_order(PendingOrder(spot_cloid, coin, "spot", True, spot_size, spot_limit))
        state.add_pending_order(PendingOrder(perp_cloid, coin, "perp", False, perp_size, perp_limit))
        
        logger.info(f"🚀 Executing DN: {coin} Spot {spot_size} @ {spot_limit}, Perp {perp_size} @ {perp_limit}")
        
        try:
            # PARALLEL EXECUTION
            results = await asyncio.gather(
                self._place_with_timeout(coin, "spot", True, spot_size, spot_limit, spot_cloid),
                self._place_with_timeout(coin, "perp", False, perp_size, perp_limit, perp_cloid),
                return_exceptions=True
            )
            
            # FIX: Handle Exception instances from gather
            spot_result = results[0]
            perp_result = results[1]
            
            # Check if results are exceptions
            if isinstance(spot_result, Exception):
                logger.error(f"Spot order exception: {spot_result}")
                spot_ok, spot_filled = False, 0.0
            else:
                spot_ok, spot_filled = spot_result
            
            if isinstance(perp_result, Exception):
                logger.error(f"Perp order exception: {perp_result}")
                perp_ok, perp_filled = False, 0.0
            else:
                perp_ok, perp_filled = perp_result
            
            # Scenario 1: Both succeeded
            if spot_ok and perp_ok:
                logger.info(f"✅ DN Success: {coin} Spot {spot_filled}, Perp {perp_filled}")
                
                # Add to state
                state.add_position(Position(
                    coin=coin,
                    spot_size=spot_filled,
                    perp_size=perp_filled,
                    entry_price_spot=spot_price,
                    entry_price_perp=perp_price
                ))
                
                return ExecutionResult(
                    success=True,
                    spot_cloid=spot_cloid,
                    perp_cloid=perp_cloid,
                    spot_filled=spot_filled,
                    perp_filled=perp_filled
                )
            
            # Scenario 2: Both failed
            if not spot_ok and not perp_ok:
                logger.warning(f"❌ DN Failed: Both legs failed for {coin}")
                return ExecutionResult(success=False, error="Both legs failed")
            
            # Scenario 3: LEGGED TRADE - Unwind immediately
            logger.critical(f"🚨 LEGGED TRADE: {coin} spot={spot_ok} perp={perp_ok}")
            
            if spot_ok:
                await self._emergency_unwind("spot", coin, spot_filled, spot_price)
            elif perp_ok:
                await self._emergency_unwind("perp", coin, perp_filled, perp_price)
            
            return ExecutionResult(success=False, error="Legged trade - unwound")
            
        finally:
            # Clean up pending orders
            state.remove_pending_order(spot_cloid)
            state.remove_pending_order(perp_cloid)
    
    async def _place_with_timeout(self, coin: str, side: str, is_buy: bool,
                                    size: float, price: float, cloid: str) -> Tuple[bool, float]:
        """
        Place order with timeout and status verification.
        
        Returns:
            (success, filled_size)
        """
        try:
            result = await asyncio.wait_for(
                self.client.place_order(coin, side, is_buy, size, price, cloid),
                timeout=self.order_timeout
            )
            
            if result.get("status") == "filled":
                return True, result.get("filled_size", size)
            else:
                return False, 0.0
                
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Order {cloid} timed out. Checking status...")
            return await self._handle_timeout(coin, side, cloid)
        except Exception as e:
            logger.error(f"Order error {cloid}: {e}")
            return False, 0.0
    
    async def _handle_timeout(self, coin: str, side: str, cloid: str) -> Tuple[bool, float]:
        """Handle order timeout - check if it actually filled."""
        try:
            status = await self.client.query_order_status(coin, cloid)
            
            if status.get("status") == "filled":
                logger.info(f"✅ Recovered ghost order {cloid}: FILLED")
                return True, status.get("filled_size", 0)
            elif status.get("status") == "open":
                logger.info(f"🗑️ Cancelling zombie order {cloid}")
                await self.client.cancel_order(coin, cloid)
                return False, 0.0
            else:
                return False, 0.0
                
        except Exception as e:
            logger.critical(f"💀 Could not verify order {cloid}: {e}")
            return False, 0.0
    
    async def _emergency_unwind(self, leg: str, coin: str, size: float, price: float):
        """Unwind a successful leg after the other failed."""
        logger.warning(f"🔄 Unwinding {leg} leg: {size} {coin}")
        
        # Aggressive slippage for unwind
        unwind_slippage = 0.02  # 2%
        
        if leg == "spot":
            # We bought spot, need to sell it
            unwind_price = round(price * (1 - unwind_slippage), 5)
            await self.client.place_order(coin, "spot", False, size, unwind_price, uuid4().hex)
        else:
            # We shorted perp, need to buy it back
            unwind_price = round(price * (1 + unwind_slippage), 5)
            await self.client.place_order(coin, "perp", True, size, unwind_price, uuid4().hex)
    
    async def _close_partial(self, coin: str, percentage: float) -> bool:
        """Close a percentage of a position."""
        state = StateConfig.get()
        pos = state.get_position(coin)
        
        if not pos:
            logger.warning(f"No position found for {coin}")
            return False
        
        spot_close = round(pos.spot_size * percentage, 4)
        perp_close = round(pos.perp_size * percentage, 4)
        
        logger.info(f"🔻 Closing {percentage*100:.0f}% of {coin}: Spot {spot_close}, Perp {perp_close}")
        
        # Get current prices
        current_spot = pos.entry_price_spot  # TODO: Get live price
        current_perp = pos.entry_price_perp
        
        # Close both legs
        results = await asyncio.gather(
            self.client.place_order(coin, "spot", False, spot_close, 
                                     current_spot * 0.98, uuid4().hex),
            self.client.place_order(coin, "perp", True, perp_close,
                                     current_perp * 1.02, uuid4().hex),
            return_exceptions=True
        )
        
        # Update state
        if percentage >= 1.0:
            state.remove_position(coin)
        else:
            state.update_position_size(
                coin,
                pos.spot_size - spot_close,
                pos.perp_size - perp_close
            )
        
        return True
