"""
MarginMonitor - WebSocket-Driven Margin Safety

Monitors margin ratio on every price update and triggers rebalancing
when approaching liquidation. Includes smart watchdog that attempts
reconnection before panic closing.

FIX APPLIED: Event spam prevention via _is_rebalancing flag
"""

import asyncio
import logging
import sys
import time

from core.state import StateConfig

logger = logging.getLogger(__name__)


class MarginMonitor:
    """
    WebSocket-driven margin safety with graceful failure handling.
    
    Features:
    - Reacts to EVERY price update (no polling)
    - Event spam prevention (only one rebalance at a time)
    - Smart watchdog: reconnect → panic → exit
    """
    
    def __init__(self, ws_manager, execution_guard, panic_switch, 
                 danger_threshold: float = 0.15,
                 critical_threshold: float = 0.10):
        """
        Args:
            ws_manager: WebSocketManager instance
            execution_guard: ExecutionGuard instance
            panic_switch: PanicSwitch instance
            danger_threshold: Margin ratio to trigger 25% reduction
            critical_threshold: Margin ratio to trigger 50% reduction
        """
        self.ws = ws_manager
        self.guard = execution_guard
        self.panic = panic_switch
        
        self.danger_threshold = danger_threshold
        self.critical_threshold = critical_threshold
        
        # Heartbeat for watchdog
        self.last_heartbeat = time.time()
        
        # FIX: Event spam prevention
        self._is_rebalancing = False
        
        # Negative funding tracking
        self._negative_funding_since = None
        self.negative_funding_tolerance_hours = 2
    
    def on_price_update(self, prices):
        """
        Called on EVERY WebSocket price update.
        
        FIX: Prevents spawning multiple rebalance tasks
        """
        self.last_heartbeat = time.time()
        
        # Calculate margin ratio
        margin = self._calc_margin_ratio(prices)
        state = StateConfig.get()
        state.margin_ratio = margin
        state.last_price_update = time.time()
        
        # Skip if already rebalancing (prevent event spam)
        if self._is_rebalancing:
            return
        
        # Check if we have any positions to protect
        if not state.positions:
            return
        
        # Trigger rebalance if needed
        if margin < self.critical_threshold:
            # CRITICAL - close 50%
            logger.critical(f"🚨 CRITICAL MARGIN: {margin:.2%} - Closing 50%")
            self._spawn_rebalance(0.50)
        elif margin < self.danger_threshold:
            # DANGER - close 25%
            logger.warning(f"⚠️ LOW MARGIN: {margin:.2%} - Closing 25%")
            self._spawn_rebalance(0.25)
    
    def _spawn_rebalance(self, percentage: float):
        """Spawn rebalance with spam protection."""
        self._is_rebalancing = True
        asyncio.create_task(self._do_rebalance(percentage))
    
    async def _do_rebalance(self, percentage: float):
        """Execute rebalance with flag cleanup."""
        try:
            state = StateConfig.get()
            for coin in list(state.positions.keys()):
                await self.guard.safety_rebalance(coin, percentage)
        except Exception as e:
            logger.error(f"Rebalance error: {e}")
        finally:
            # FIX: Always reset flag
            self._is_rebalancing = False
    
    def _calc_margin_ratio(self, prices) -> float:
        """
        Calculate current margin ratio for perp positions.
        
        margin_ratio = account_equity / position_value
        
        Returns 1.0 if no positions (safe).
        """
        state = StateConfig.get()
        
        if not state.positions:
            return 1.0
        
        # Sum up position values
        total_position_value = 0.0
        for pos in state.positions.values():
            # Use current perp price if available
            if hasattr(prices, 'perp') and prices.perp.best_bid > 0:
                current_price = prices.perp.best_bid
            else:
                current_price = pos.entry_price_perp
            
            total_position_value += pos.perp_size * current_price
        
        if total_position_value == 0:
            return 1.0
        
        # Margin ratio approximation
        # In reality, you'd fetch this from the exchange
        equity = state.perp_margin_usdc
        return equity / total_position_value if total_position_value > 0 else 1.0
    
    def check_funding_direction(self, funding_rate: float) -> bool:
        """
        Check if funding has been negative too long.
        
        Returns True if we should exit due to negative funding.
        """
        if funding_rate >= 0:
            self._negative_funding_since = None
            return False
        
        # Funding is negative
        if self._negative_funding_since is None:
            self._negative_funding_since = time.time()
            logger.warning(f"⚠️ Funding went negative: {funding_rate:.4%}")
        
        hours_negative = (time.time() - self._negative_funding_since) / 3600
        
        if hours_negative >= self.negative_funding_tolerance_hours:
            logger.critical(f"🚨 NEGATIVE FUNDING for {hours_negative:.1f}h - Should exit!")
            return True
        
        return False
    
    async def watchdog_loop(self):
        """
        Graceful failure: reconnect → panic → exit.
        
        Runs as background task. Checks websocket health every 5 seconds.
        """
        while True:
            await asyncio.sleep(5)
            
            stale_seconds = time.time() - self.last_heartbeat
            
            if stale_seconds > 10:
                logger.warning(f"⚠️ WebSocket stale for {stale_seconds:.0f}s. Attempting reconnect...")
                
                # Step 1: Try reconnect
                try:
                    if hasattr(self.ws, 'reconnect'):
                        success = await asyncio.wait_for(
                            self.ws.reconnect(),
                            timeout=10.0
                        )
                        if success:
                            self.last_heartbeat = time.time()
                            logger.info("✅ WebSocket reconnected")
                            continue
                except asyncio.TimeoutError:
                    logger.warning("Reconnect timed out")
                except Exception as e:
                    logger.warning(f"Reconnect failed: {e}")
                
                # Step 2: Reconnect failed - panic close via REST
                logger.critical("🚨 Reconnect failed. Panic closing all positions...")
                
                try:
                    success = await self.panic.emergency_close_all()
                    
                    if success:
                        logger.info("✅ Positions closed safely. Exiting.")
                        sys.exit(0)
                except Exception as e:
                    logger.critical(f"Panic close failed: {e}")
                
                # Step 3: Nothing worked - exit for systemd restart
                logger.critical("💀 All recovery attempts failed. Dying for restart.")
                sys.exit(1)
    
    async def start(self):
        """Start the watchdog as a background task."""
        asyncio.create_task(self.watchdog_loop())
        logger.info("🐕 Watchdog started")
