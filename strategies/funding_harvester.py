"""
FundingHarvester - Delta Neutral Funding Rate Strategy

Orchestrates the delta neutral funding strategy:
1. Scan for opportunities
2. Execute entry when conditions met
3. Monitor and collect funding
4. Exit when funding turns negative or better opportunity exists
"""

import asyncio
import logging
import time

from core.state import StateConfig
from core.execution_guard import ExecutionGuard
from services.funding_scanner import FundingScanner
from services.database import DatabaseLogger

import config

logger = logging.getLogger(__name__)


class FundingHarvester:
    """
    Delta Neutral Funding Rate Harvester.
    
    Strategy:
    - Long Spot + Short Perp = Net zero exposure
    - Collect hourly funding payments from longs
    - Exit when funding goes negative
    """
    
    def __init__(self, execution_guard: ExecutionGuard, 
                 scanner: FundingScanner,
                 database: DatabaseLogger,
                 client):
        """
        Args:
            execution_guard: ExecutionGuard instance
            scanner: FundingScanner instance
            database: DatabaseLogger instance
            client: HyperliquidClient instance
        """
        self.guard = execution_guard
        self.scanner = scanner
        self.db = database
        self.client = client
        
        # Config
        self.max_position_usd = getattr(config, 'MAX_POSITION_PER_COIN_USD', 500)
        self.max_total_exposure = getattr(config, 'MAX_TOTAL_EXPOSURE_USD', 2000)
        self.scan_interval = 300  # 5 minutes
        self.funding_check_interval = 3600  # 1 hour
        
        # State
        self._running = False
        self._last_scan = 0
        self._last_funding_log = 0
    
    async def start(self):
        """Start the strategy loop."""
        self._running = True
        logger.info("🌾 Funding Harvester started")
        
        # Run main loop
        asyncio.create_task(self._strategy_loop())
        asyncio.create_task(self._funding_log_loop())
    
    async def stop(self):
        """Stop the strategy."""
        self._running = False
        logger.info("🌾 Funding Harvester stopped")
    
    async def _strategy_loop(self):
        """Main strategy loop - scans and enters positions."""
        while self._running:
            try:
                await self._check_and_execute()
            except Exception as e:
                logger.error(f"Strategy loop error: {e}")
            
            await asyncio.sleep(self.scan_interval)
    
    async def _check_and_execute(self):
        """Check for opportunities and execute if conditions met."""
        state = StateConfig.get()
        
        # Check if we have capacity
        if state.total_exposure_usd >= self.max_total_exposure:
            logger.debug("Max exposure reached, skipping scan")
            return
        
        # Scan for opportunities
        opportunities = await self.scanner.scan()
        viable = [o for o in opportunities if o.viable]
        
        if not viable:
            logger.debug("No viable opportunities found")
            return
        
        # Check each viable opportunity
        for opp in viable:
            # Skip if we already have a position in this coin
            if state.has_position(opp.coin):
                continue
            
            # Calculate position size
            remaining_capacity = self.max_total_exposure - state.total_exposure_usd
            size_usd = min(self.max_position_usd, remaining_capacity)
            
            if size_usd < 5:  # Minimum position lowered for testing
                continue
            
            # Get current prices
            prices = await self.client.get_prices(opp.coin)
            
            if prices["spot_ask"] == 0 or prices["perp_bid"] == 0:
                logger.warning(f"Invalid prices for {opp.coin}")
                continue
            
            # Check balances - lowered requirements for testing
            balances = await self.client.get_balances()
            required_spot = size_usd * 1.02  # 2% buffer instead of 5%
            required_margin = size_usd * 0.20  # 20% margin buffer instead of 30%
            
            if balances["spot_usdc"] < required_spot:
                logger.warning(f"Insufficient spot USDC: ${balances['spot_usdc']:.2f}")
                continue
            
            if balances["perp_margin"] < required_margin:
                logger.warning(f"Insufficient perp margin: ${balances['perp_margin']:.2f}")
                continue
            
            # Execute entry
            logger.info(f"🎯 Entering {opp.coin}: ${size_usd:.2f} (APR: {opp.funding_apr*100:.1f}%)")
            
            result = await self.guard.execute_delta_neutral(
                coin=opp.coin,
                size_usd=size_usd,
                spot_price=prices["spot_ask"],
                perp_price=prices["perp_bid"]
            )
            
            if result.success:
                logger.info(f"✅ Position opened: {opp.coin}")
                
                # Log to database
                self.db.log_position_open(
                    coin=opp.coin,
                    size=result.spot_filled,
                    size_usd=size_usd,
                    entry_spot=prices["spot_ask"],
                    entry_perp=prices["perp_bid"]
                )
                
                self.db.log_trade(
                    position_id=0,  # Will be updated
                    coin=opp.coin,
                    side="buy",
                    market="spot",
                    size=result.spot_filled,
                    price=prices["spot_ask"],
                    cloid=result.spot_cloid
                )
                
                self.db.log_trade(
                    position_id=0,
                    coin=opp.coin,
                    side="sell",
                    market="perp",
                    size=result.perp_filled,
                    price=prices["perp_bid"],
                    cloid=result.perp_cloid
                )
            else:
                logger.warning(f"❌ Entry failed for {opp.coin}: {result.error}")
            
            # Only enter one position per loop iteration
            break
    
    async def _funding_log_loop(self):
        """Log funding payments received."""
        while self._running:
            await asyncio.sleep(self.funding_check_interval)
            
            try:
                await self._log_funding_payments()
            except Exception as e:
                logger.error(f"Funding log error: {e}")
    
    async def _log_funding_payments(self):
        """Check and log funding payments for open positions."""
        state = StateConfig.get()
        
        for coin, pos in state.positions.items():
            # Get current funding rate
            rate = await self.client.get_funding_rate(coin)
            
            if rate <= 0:
                logger.warning(f"⚠️ Negative funding for {coin}: {rate:.4%}")
                # Let MarginMonitor handle this
                continue
            
            # Calculate funding payment
            # Funding = position_size * funding_rate
            funding_payment = pos.perp_size * rate * pos.entry_price_perp
            
            logger.info(f"💰 Funding received: ${funding_payment:.4f} for {coin} ({rate:.4%})")
            
            # Log to database
            self.db.log_funding(
                coin=coin,
                position_id=0,  # Would need proper ID tracking
                amount=funding_payment,
                rate=rate,
                size=pos.perp_size
            )
    
    async def check_exit_conditions(self, coin: str) -> bool:
        """Check if we should exit a position."""
        # Get current funding rate
        rate = await self.client.get_funding_rate(coin)
        
        # Exit if funding is negative for too long
        # (handled by MarginMonitor.check_funding_direction)
        if rate < 0:
            return True
        
        # Could add other exit conditions:
        # - Better opportunity elsewhere
        # - Target profit reached
        # - Time-based exit
        
        return False
    
    def get_status(self) -> dict:
        """Get strategy status for dashboard."""
        state = StateConfig.get()
        
        return {
            "running": self._running,
            "positions": len(state.positions),
            "total_exposure_usd": state.total_exposure_usd,
            "max_exposure_usd": self.max_total_exposure,
            "last_scan": self._last_scan,
            "scanner_summary": self.scanner.get_scan_summary()
        }
