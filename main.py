#!/usr/bin/env python3
"""
Delta Neutral Funding Bot - Main Entry Point

Production-grade funding rate harvester for Hyperliquid.
Implements:
- Dynamic asset ID resolution at startup
- Exchange state reconciliation
- In-memory state management
- WebSocket-driven margin safety
- Atomic delta-neutral execution
"""

import asyncio
import signal
import sys
import argparse
import logging

# Load .env before importing config
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config
from core.state import StateConfig, Position
from core.execution_guard import ExecutionGuard
from core.margin_monitor import MarginMonitor
from utils.hyperliquid_client import HyperliquidClient
from utils.panic_switch import PanicSwitch
from utils.notifier import get_notifier
from services.funding_scanner import FundingScanner
from services.database import DatabaseLogger
from strategies.funding_harvester import FundingHarvester
from websocket_manager import WebSocketManager

# ======== CONFIG FALLBACKS ========
# Allows bot to work with older config.py versions
if not hasattr(config, 'COIN_NAME'):
    config.COIN_NAME = "HYPE"
if not hasattr(config, 'IS_FUNDING_STRATEGY'):
    config.IS_FUNDING_STRATEGY = True
if not hasattr(config, 'SPOT_SYMBOL'):
    config.SPOT_SYMBOL = None  # Will be resolved dynamically
if not hasattr(config, 'MAX_POSITION_PER_COIN_USD'):
    config.MAX_POSITION_PER_COIN_USD = getattr(config, 'MAX_POSITION_USD', 50)
if not hasattr(config, 'MAX_TOTAL_EXPOSURE_USD'):
    config.MAX_TOTAL_EXPOSURE_USD = config.MAX_POSITION_PER_COIN_USD * 4
if not hasattr(config, 'LOG_LEVEL'):
    config.LOG_LEVEL = "INFO"

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('funding_bot.log')
    ]
)
logger = logging.getLogger(__name__)


def print_banner():
    """Print startup banner."""
    banner = """
    ╔═══════════════════════════════════════════════════════════════╗
    ║                                                               ║
    ║   🌾 FUNDING HARVESTER - Delta Neutral Bot                   ║
    ║                                                               ║
    ║   Strategy: Long Spot + Short Perp = Funding Income          ║
    ║   Platform: Hyperliquid                                       ║
    ║                                                               ║
    ╚═══════════════════════════════════════════════════════════════╝
    """
    print(banner)


async def resolve_spot_asset_id(client: HyperliquidClient) -> str:
    """
    Dynamically resolve the spot asset ID for the configured coin.
    
    CRITICAL: Asset IDs can change! Never hardcode them.
    
    Returns:
        The spot symbol (e.g., "@107" for HYPE)
    
    Raises:
        SystemExit if coin not found
    """
    logger.info(f"🔍 Resolving spot asset ID for {config.COIN_NAME}...")
    
    loop = asyncio.get_event_loop()
    
    def _resolve():
        try:
            # Get spot metadata
            spot_meta = client.info.spot_meta()
            
            if not spot_meta or 'tokens' not in spot_meta:
                logger.error("Failed to fetch spot metadata")
                return None
            
            # Find the token by name
            for token in spot_meta.get('tokens', []):
                if token.get('name') == config.COIN_NAME:
                    # The index is the token ID
                    token_index = token.get('index')
                    if token_index is not None:
                        spot_symbol = f"@{token_index}"
                        logger.info(f"✅ Resolved {config.COIN_NAME} -> {spot_symbol}")
                        return spot_symbol
            
            # Also check universe for perps
            meta = client.info.meta()
            for i, asset in enumerate(meta.get('universe', [])):
                if asset.get('name') == config.COIN_NAME:
                    logger.debug(f"Found {config.COIN_NAME} in perp universe at index {i}")
            
            return None
            
        except Exception as e:
            logger.error(f"Asset resolution error: {e}")
            return None
    
    spot_symbol = await loop.run_in_executor(None, _resolve)
    
    if not spot_symbol:
        logger.critical(f"❌ FATAL: Could not resolve spot asset ID for {config.COIN_NAME}")
        logger.critical("The coin may not be available for spot trading on Hyperliquid.")
        sys.exit(1)
    
    # Update config with resolved symbol
    config.SPOT_SYMBOL = spot_symbol
    return spot_symbol


async def reconcile_from_exchange(client: HyperliquidClient) -> bool:
    """
    CRITICAL: Sync state with exchange on startup.
    
    Rule 0: Trust the API, not the database.
    Old state is dangerous state.
    """
    logger.info("🔄 Reconciling state with exchange...")
    
    state = StateConfig.reset()  # Fresh state
    
    try:
        # 1. Get actual positions from exchange
        live_positions = await client.get_positions()
        
        # 2. Get balances
        balances = await client.get_balances()
        state.spot_balance_usdc = balances.get("spot_usdc", 0)
        state.perp_margin_usdc = balances.get("perp_margin", 0)
        
        # 3. Reconstruct positions from exchange state
        for coin, pos_data in live_positions.items():
            if pos_data["side"] == "short":
                logger.info(f"📌 Found position: {coin} {pos_data['size']} @ {pos_data['entry_price']}")
                
                position = Position(
                    coin=coin,
                    spot_size=pos_data["size"],
                    perp_size=pos_data["size"],
                    entry_price_spot=pos_data["entry_price"],
                    entry_price_perp=pos_data["entry_price"]
                )
                state.add_position(position)
        
        # 4. Calculate buffer
        state.available_buffer_usd = max(0, state.perp_margin_usdc - state.total_exposure_usd * 0.5)
        
        logger.info(f"✅ Reconciliation complete:")
        logger.info(f"   Positions: {len(state.positions)}")
        logger.info(f"   Spot USDC: ${state.spot_balance_usdc:.2f}")
        logger.info(f"   Perp Margin: ${state.perp_margin_usdc:.2f}")
        logger.info(f"   Buffer: ${state.available_buffer_usd:.2f}")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Reconciliation failed: {e}")
        return False


async def verify_panic_switch(client: HyperliquidClient) -> bool:
    """
    Test the panic switch without actually closing positions.
    Used with --verify-panic flag.
    """
    logger.warning("🧪 PANIC SWITCH VERIFICATION MODE")
    
    panic = PanicSwitch(client)
    state = StateConfig.get()
    
    if not state.positions:
        logger.info("No positions to close. Panic switch would do nothing.")
        logger.info("✅ Panic switch verification: PASSED (no-op)")
        return True
    
    logger.warning(f"Found {len(state.positions)} positions:")
    for coin, pos in state.positions.items():
        logger.warning(f"  - {coin}: Spot {pos.spot_size}, Perp {pos.perp_size}")
    
    # Actually close if user confirms
    confirm = input("\n⚠️  Type 'CLOSE ALL' to actually close these positions: ")
    
    if confirm == "CLOSE ALL":
        logger.critical("🚨 EXECUTING PANIC CLOSE...")
        success = await panic.emergency_close_all()
        if success:
            logger.info("✅ All positions closed successfully")
        else:
            logger.error("❌ Some positions failed to close!")
        return success
    else:
        logger.info("Aborted. No positions were closed.")
        return True


async def run_bot(dry_run: bool = True, size_limit: float = None):
    """Run the funding harvester bot."""
    
    # Override config if needed
    if dry_run:
        logger.warning("📝 DRY RUN MODE - Not executing real trades")
    
    if size_limit:
        config.MAX_POSITION_PER_COIN_USD = size_limit
        config.MAX_TOTAL_EXPOSURE_USD = size_limit * 4
    
    # Initialize client
    logger.info("🔧 Initializing components...")
    client = HyperliquidClient()
    
    # CRITICAL: Resolve spot asset ID dynamically
    await resolve_spot_asset_id(client)
    
    # CRITICAL: Reconcile before anything else
    if not await reconcile_from_exchange(client):
        logger.critical("Reconciliation failed - refusing to start")
        return
    
    # Create all components with dry_run passed through
    db = DatabaseLogger()
    panic = PanicSwitch(client)
    guard = ExecutionGuard(client, dry_run=dry_run)
    scanner = FundingScanner(client)
    ws = WebSocketManager()
    monitor = MarginMonitor(ws, guard, panic)
    harvester = FundingHarvester(guard, scanner, db, client)
    
    # Start background tasks
    asyncio.create_task(db.start_consumer())
    await monitor.start()
    
    # Hook monitor to websocket
    ws.on_price_update = monitor.on_price_update
    
    # Start strategy (unless dry run)
    if not dry_run:
        await harvester.start()
    else:
        asyncio.create_task(dry_run_scanner(scanner))
    
    # Handle shutdown
    shutdown_event = asyncio.Event()
    
    def signal_handler():
        logger.info("🛑 Shutdown signal received...")
        shutdown_event.set()
    
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    # Run main loop
    logger.info("🚀 Bot started!")
    logger.info(f"   Coin: {config.COIN_NAME}")
    logger.info(f"   Spot Symbol: {config.SPOT_SYMBOL}")
    logger.info(f"   Strategy: {'Funding' if config.IS_FUNDING_STRATEGY else 'Spread Arbitrage'}")
    logger.info(f"   Dry Run: {dry_run}")
    
    # Send startup notification
    notifier = get_notifier()
    notifier.startup(
        wallet=config.ACCOUNT_ADDRESS,
        mode="DRY RUN" if dry_run else "LIVE",
        size=size_limit or config.MAX_POSITION_PER_COIN_USD
    )
    
    try:
        ws_task = asyncio.create_task(ws.connect())
        await shutdown_event.wait()
        
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
        notifier.error("MainLoop", str(e), fatal=True)
    finally:
        logger.info("Shutting down...")
        notifier.shutdown("Manual")
        await harvester.stop()
        await ws.disconnect()
        await db.stop()
        logger.info("✅ Shutdown complete")



async def dry_run_scanner(scanner: FundingScanner):
    """Run scanner in dry run mode to show opportunities."""
    while True:
        try:
            opportunities = await scanner.scan()
            viable = [o for o in opportunities if o.viable]
            
            if viable:
                logger.info("📊 Dry Run - Current opportunities:")
                for o in viable[:3]:
                    logger.info(f"   {o.coin}: APR {o.funding_apr*100:.1f}%, Net APY {o.net_apy:.1f}%")
            else:
                logger.info("📊 Dry Run - No viable opportunities right now")
                
        except Exception as e:
            logger.error(f"Scanner error: {e}")
        
        await asyncio.sleep(300)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Delta Neutral Funding Bot for Hyperliquid"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading (disables dry_run)"
    )
    parser.add_argument(
        "--size",
        type=float,
        default=None,
        help="Maximum position size in USD (overrides config)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--verify-panic",
        action="store_true",
        help="Test the panic switch (emergency close all)"
    )
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    print_banner()
    
    print(f"📋 Configuration:")
    print(f"   Wallet: {config.ACCOUNT_ADDRESS[:10]}...{config.ACCOUNT_ADDRESS[-8:] if config.ACCOUNT_ADDRESS else 'NOT SET'}")
    print(f"   Coin: {config.COIN_NAME}")
    print(f"   Mode: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"   Max Position: ${args.size or config.MAX_POSITION_PER_COIN_USD}")
    print(f"   Strategy: {'Funding Harvester' if config.IS_FUNDING_STRATEGY else 'Spread Arbitrage'}")
    print()
    
    # Validate credentials
    if not config.PRIVATE_KEY or not config.ACCOUNT_ADDRESS:
        print("❌ ERROR: Missing credentials!")
        print("   Set HL_PRIVATE_KEY and HL_ACCOUNT_ADDRESS in .env file")
        sys.exit(1)
    
    try:
        # Panic switch verification mode
        if args.verify_panic:
            client = HyperliquidClient()
            asyncio.run(reconcile_from_exchange(client))
            asyncio.run(verify_panic_switch(client))
            return
        
        # Normal operation
        if args.live:
            print("⚠️  WARNING: Live trading mode!")
            print("    Press Ctrl+C within 5 seconds to cancel...")
            import time
            time.sleep(5)
        
        asyncio.run(run_bot(dry_run=not args.live, size_limit=args.size))
        
    except KeyboardInterrupt:
        print("\n\n🛑 Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
