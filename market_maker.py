"""
Market Maker Bot for Hyperliquid

A professional market making engine that:
- Quotes two-sided markets (bid + ask)
- Uses POST_ONLY orders for maker rebates
- Skews quotes based on inventory
- Auto-hedges via perpetual contracts
"""

import asyncio
import logging
import time
from typing import Optional
from datetime import datetime

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

import config
from websocket_manager import WebSocketManager, PriceState
from inventory_manager import InventoryManager
from order_manager import OrderManager

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MarketMaker:
    """
    Professional Market Making Engine.
    
    Strategy:
    1. Subscribe to L2 orderbook via WebSocket
    2. Calculate fair price based on mid-price
    3. Place grid of limit orders around fair price
    4. Adjust quotes based on inventory (skewing)
    5. Hedge excess inventory via perpetual contracts
    
    Revenue model:
    - Earn bid-ask spread on each roundtrip
    - Collect maker rebates (-0.01% on Hyperliquid)
    """
    
    def __init__(self):
        # SDK setup with retry
        self.account = Account.from_key(config.PRIVATE_KEY)
        
        max_retries = 5
        for i in range(max_retries):
            try:
                self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
                self.exchange = Exchange(
                    self.account,
                    constants.MAINNET_API_URL,
                    account_address=config.ACCOUNT_ADDRESS
                )
                break
            except Exception as e:
                logger.warning(f"Init attempt {i+1}/{max_retries} failed: {e}")
                if i == max_retries - 1:
                    raise
                time.sleep(5)
        
        # Managers
        self.inventory_mgr = InventoryManager(self.exchange, self.info)
        self.order_mgr = OrderManager(self.exchange, self.info)
        
        # WebSocket
        self.ws_manager: Optional[WebSocketManager] = None
        
        # State
        self.running = False
        self.last_quote_update = 0
        self.quote_refresh_interval = getattr(config, 'MM_REFRESH_SECONDS', 2)
        
        # Stats
        self.stats = {
            'start_time': datetime.now().isoformat(),
            'quote_updates': 0,
            'fills': 0,
            'volume_usd': 0.0,
            'gross_pnl': 0.0,
            'fees_earned': 0.0,  # Maker rebates
        }
        
        logger.info("=" * 60)
        logger.info("🏦 MARKET MAKER BOT INITIALIZED")
        logger.info("=" * 60)
        logger.info(f"Asset: {config.SPOT_SYMBOL}")
        logger.info(f"Spread: {getattr(config, 'MM_SPREAD_BPS', 8)} bps")
        logger.info(f"Quote Size: ${getattr(config, 'MM_QUOTE_SIZE_USD', 50)}")
        logger.info(f"Levels: {getattr(config, 'MM_NUM_LEVELS', 3)}")
        logger.info(f"Max Inventory: ${getattr(config, 'MM_MAX_INVENTORY_USD', 500)}")
        logger.info(f"DRY RUN: {getattr(config, 'DRY_RUN', False)}")
        logger.info("=" * 60)
    
    async def on_price_update(self, prices: PriceState) -> None:
        """
        Handle price updates from WebSocket.
        
        This is the main loop that:
        1. Calculates fair price
        2. Gets inventory skew
        3. Updates quote grid
        """
        now = time.time()
        
        # Rate limit quote updates
        if now - self.last_quote_update < self.quote_refresh_interval:
            return
        
        # Calculate fair price (mid-price)
        fair_price = (prices.spot.best_bid + prices.spot.best_ask) / 2
        
        if fair_price <= 0:
            return
        
        # Get inventory skew
        skew_bps = self.inventory_mgr.get_skew_bps(fair_price)
        
        # Log periodically
        self.stats['quote_updates'] += 1
        if self.stats['quote_updates'] % 30 == 0:  # Every 30 updates (~1 min)
            self._log_status(prices, fair_price, skew_bps)
        
        # Check if we need to hedge
        if self.inventory_mgr.should_hedge(fair_price):
            await self.inventory_mgr.execute_hedge(fair_price)
        
        # Update quote grid
        try:
            await self.order_mgr.place_grid(fair_price, skew_bps)
            self.last_quote_update = now
        except Exception as e:
            logger.error(f"Quote update failed: {e}")
    
    def _log_status(self, prices: PriceState, fair_price: float, skew_bps: float):
        """Log current status."""
        inv = self.inventory_mgr.state
        spread = prices.get_entry_spread() * 10000  # Convert to bps
        
        logger.info(
            f"📊 Fair: ${fair_price:.4f} | "
            f"Spread: {spread:.1f}bps | "
            f"Skew: {skew_bps:+.1f}bps | "
            f"Inv: ${inv.net_delta:.0f} | "
            f"Updates: {self.stats['quote_updates']}"
        )
    
    async def run(self):
        """Run the market maker."""
        logger.info("🚀 Starting Market Maker...")
        
        self.running = True
        
        # Setup WebSocket
        self.ws_manager = WebSocketManager(
            on_price_update=lambda p: asyncio.create_task(self.on_price_update(p))
        )
        
        # Test connection
        if not await self.ws_manager.test_connection():
            logger.error("WebSocket connection failed")
            return
        
        try:
            # Status loop
            async def status_loop():
                while self.running:
                    await asyncio.sleep(60)  # Every minute
                    logger.info(f"⏱️ Status: {self.stats['quote_updates']} updates, {self.order_mgr.get_active_order_count()} orders")
            
            # Start background tasks
            status_task = asyncio.create_task(status_loop())
            
            # Connect to WebSocket (blocks until disconnect)
            await self.ws_manager.connect()
            
        except asyncio.CancelledError:
            logger.info("Market maker cancelled")
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("🛑 Shutting down market maker...")
        
        self.running = False
        
        # Cancel all orders
        try:
            await self.order_mgr.cancel_all()
            logger.info("✅ All orders cancelled")
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")
        
        # Disconnect WebSocket
        if self.ws_manager:
            await self.ws_manager.disconnect()
        
        # Print final stats
        self._print_summary()
    
    def _print_summary(self):
        """Print session summary."""
        print("\n" + "=" * 60)
        print("📊 MARKET MAKER SESSION SUMMARY")
        print("=" * 60)
        print(f"Start: {self.stats['start_time']}")
        print(f"End: {datetime.now().isoformat()}")
        print(f"Quote Updates: {self.stats['quote_updates']}")
        print(f"Total Volume: ${self.stats['volume_usd']:.2f}")
        print(f"Gross PnL: ${self.stats['gross_pnl']:.2f}")
        print(f"Maker Rebates: ${self.stats['fees_earned']:.4f}")
        print("=" * 60)


async def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Hyperliquid Market Maker')
    parser.add_argument('--dry-run', action='store_true', help='Run without placing real orders')
    args = parser.parse_args()
    
    if args.dry_run:
        config.DRY_RUN = True
        logger.info("🔄 DRY RUN MODE ENABLED")
    
    mm = MarketMaker()
    await mm.run()


if __name__ == "__main__":
    asyncio.run(main())
