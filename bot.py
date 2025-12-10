"""
Delta Neutral Arbitrage Bot for Hyperliquid - DATA COLLECTION VERSION

Enhanced with comprehensive logging for profitability analysis.
Tracks every spread check, opportunity, and trade for later analysis.
"""

import asyncio
import logging
import json
import os
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime
import time

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

import config
from websocket_manager import WebSocketManager, PriceState
from trade_events import trade_events

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PositionState(Enum):
    FLAT = "flat"
    OPEN = "open"
    PENDING = "pending"


@dataclass
class SpreadDataPoint:
    """Single spread observation for analysis."""
    timestamp: str
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    entry_spread: float
    exit_spread: float
    is_opportunity: bool
    funding_rate: float = 0.0


@dataclass
class TradeRecord:
    """Complete record of a trade cycle."""
    id: int
    entry_time: str
    exit_time: str = ""
    
    # Entry details
    entry_spot_price: float = 0.0
    entry_perp_price: float = 0.0
    entry_spread: float = 0.0
    size: float = 0.0
    
    # Exit details
    exit_spot_price: float = 0.0
    exit_perp_price: float = 0.0
    exit_spread: float = 0.0
    
    # P&L
    spot_pnl: float = 0.0
    perp_pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    
    # Status
    status: str = "open"  # open, closed, error
    error_message: str = ""


@dataclass
class DataCollector:
    """Collects and saves all data for analysis."""
    spread_history: List[SpreadDataPoint] = field(default_factory=list)
    trades: List[TradeRecord] = field(default_factory=list)
    opportunities_found: int = 0
    opportunities_missed: int = 0  # Had opportunity but couldn't trade
    
    # Statistics
    start_time: str = ""
    total_spread_checks: int = 0
    spreads_above_threshold: int = 0
    
    def save(self):
        """Save all data to JSON files."""
        # Save spread log
        if config.SAVE_SPREAD_LOG:
            spread_data = {
                "start_time": self.start_time,
                "total_checks": self.total_spread_checks,
                "above_threshold": self.spreads_above_threshold,
                "threshold": config.MIN_SPREAD_THRESHOLD,
                "data": [asdict(s) for s in self.spread_history[-1000:]]  # Last 1000
            }
            with open(config.SPREAD_LOG_FILE, 'w') as f:
                json.dump(spread_data, f, indent=2)
        
        # Save trade log
        if config.SAVE_TRADE_LOG:
            trade_data = {
                "start_time": self.start_time,
                "total_trades": len(self.trades),
                "opportunities": self.opportunities_found,
                "trades": [asdict(t) for t in self.trades]
            }
            with open(config.TRADE_LOG_FILE, 'w') as f:
                json.dump(trade_data, f, indent=2)


class ArbitrageBotDataCollection:
    """
    Delta Neutral Arbitrage Bot with comprehensive data collection.
    """
    
    def __init__(self):
        self.position_state = PositionState.FLAT
        self.current_trade: Optional[TradeRecord] = None
        self.trade_counter = 0
        
        # Data collection
        self.data = DataCollector()
        self.data.start_time = datetime.now().isoformat()
        
        # Position tracking
        self.position_size = 0.0
        self.entry_spot_price = 0.0
        self.entry_perp_price = 0.0
        
        # SDK setup
        self.account = Account.from_key(config.PRIVATE_KEY)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(
            self.account, 
            constants.MAINNET_API_URL,
            account_address=config.ACCOUNT_ADDRESS
        )
        
        self.ws_manager: Optional[WebSocketManager] = None
        self._last_funding_check = 0
        self._cached_funding = 0.0
        self._last_failed_entry = 0  # Cooldown after failed entry
        self._failed_entry_cooldown = 60  # Seconds to wait after failed entry
        
        logger.info(f"Bot initialized - Data Collection Mode")
        logger.info(f"Wallet: {config.ACCOUNT_ADDRESS}")
        logger.info(f"DRY_RUN: {config.DRY_RUN}")
        logger.info(f"Threshold: {config.MIN_SPREAD_THRESHOLD*100:.2f}%")
    
    def get_funding_rate(self) -> float:
        """Get funding rate with caching (check every 60s)."""
        now = time.time()
        if now - self._last_funding_check > 60:
            try:
                meta = self.info.meta()
                for asset in meta.get("universe", []):
                    if asset.get("name") == config.PERP_SYMBOL:
                        self._cached_funding = float(asset.get("funding", 0))
                        break
                self._last_funding_check = now
            except Exception as e:
                logger.error(f"Funding rate error: {e}")
        return self._cached_funding
    
    async def on_price_update(self, prices: PriceState) -> None:
        """Handle price updates - main trading logic."""
        now = datetime.now()
        
        # Calculate spreads
        entry_spread = prices.get_entry_spread()
        exit_spread = prices.get_exit_spread()
        funding = self.get_funding_rate()
        
        is_opportunity = entry_spread > config.MIN_SPREAD_THRESHOLD
        
        # Record spread data (every 10th check to avoid huge files)
        self.data.total_spread_checks += 1
        if self.data.total_spread_checks % 10 == 0:
            self.data.spread_history.append(SpreadDataPoint(
                timestamp=now.isoformat(),
                spot_bid=prices.spot.best_bid,
                spot_ask=prices.spot.best_ask,
                perp_bid=prices.perp.best_bid,
                perp_ask=prices.perp.best_ask,
                entry_spread=entry_spread,
                exit_spread=exit_spread,
                is_opportunity=is_opportunity,
                funding_rate=funding
            ))
        
        if is_opportunity:
            self.data.spreads_above_threshold += 1
        
        # Log periodically
        if self.data.total_spread_checks % 100 == 0:
            logger.info(f"Spread: {entry_spread*100:+.4f}% | Opportunities: {self.data.opportunities_found} | Trades: {len(self.data.trades)}")
        
        # Trading logic
        if self.position_state == PositionState.PENDING:
            return
        
        # ENTRY
        if self.position_state == PositionState.FLAT and is_opportunity:
            self.data.opportunities_found += 1
            
            # Cooldown check after failed entry
            if time.time() - self._last_failed_entry < self._failed_entry_cooldown:
                logger.info(f"⏳ Cooldown active ({int(self._failed_entry_cooldown - (time.time() - self._last_failed_entry))}s remaining)")
                return
            
            # Check funding
            if config.CHECK_FUNDING_RATE and funding < 0:
                logger.info(f"⏭️ Skip: Negative funding {funding*100:.4f}%")
                self.data.opportunities_missed += 1
                return
            
            logger.info(f"🎯 OPPORTUNITY #{self.data.opportunities_found}: Spread {entry_spread*100:.4f}%")
            await self.execute_entry(prices)
        
        # EXIT
        elif self.position_state == PositionState.OPEN:
            if exit_spread < config.EXIT_THRESHOLD:
                logger.info(f"📉 EXIT SIGNAL: Spread {exit_spread*100:.4f}%")
                await self.execute_exit(prices)
    
    async def _unwind_partial_entry(self, spot_ok: bool, perp_ok: bool, size: float, spot_px: float, perp_px: float):
        """Immediately unwind partial fills to prevent naked positions."""
        logger.warning(f"🔄 UNWINDING Partial Fill - Spot: {spot_ok}, Perp: {perp_ok}")
        trade_events.error(f"Partial Fill Unwind Triggered - Spot: {spot_ok}, Perp: {perp_ok}")
        
        try:
            if spot_ok:
                # Sell Spot immediately
                logger.info(f"🔙 Reversing Spot Buy: Selling {size}...")
                await self._place_order(config.SPOT_SYMBOL, False, size, spot_px * 0.95) # 5% slip
            
            if perp_ok:
                # Close Perp immediately
                logger.info(f"🔙 Reversing Perp Short: Closing {size}...")
                await self._place_order(config.PERP_SYMBOL, True, size, perp_px * 1.05, reduce_only=True) # 5% slip
                
        except Exception as e:
            logger.error(f"CRITICAL: Unwind failed: {e}")
            trade_events.error(f"CRITICAL: Unwind failed: {e}")

    async def execute_entry(self, prices: PriceState) -> bool:
        """Execute entry trade with sequential orders (spot first, then perp)."""
        import requests
        
        spot_ask = prices.spot.best_ask
        perp_bid = prices.perp.best_bid
        
        # Calculate size
        size = round(config.MAX_POSITION_USD / spot_ask, 2)
        required_usd = size * spot_ask * 1.1  # 10% buffer for fees
        
        # Pre-flight Balance Check - BOTH Spot AND Perps wallets
        try:
            # Check SPOT balance (for buying HYPE)
            resp = requests.post('https://api.hyperliquid.xyz/info',
                json={'type': 'spotClearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5)
            spot_state = resp.json()
            spot_usdc = sum(float(b.get('total', 0)) for b in spot_state.get('balances', []) if b.get('coin') == 'USDC')
            
            # Check PERPS balance (for margin on short)
            resp_perp = requests.post('https://api.hyperliquid.xyz/info',
                json={'type': 'clearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5)
            perp_state = resp_perp.json()
            # withdrawable = available margin for new positions
            perp_margin = float(perp_state.get('withdrawable', 0))
            
            # Required: spot needs full amount, perps needs ~20% margin (5x leverage default)
            required_spot = required_usd
            required_perp_margin = size * perp_bid * 0.25  # 25% margin buffer for safety
            
            if spot_usdc < required_spot:
                logger.warning(f"⚠️ Insufficient SPOT balance: ${spot_usdc:.2f} < ${required_spot:.2f}")
                self._last_failed_entry = time.time()
                return False
            
            if perp_margin < required_perp_margin:
                logger.warning(f"⚠️ Insufficient PERP margin: ${perp_margin:.2f} < ${required_perp_margin:.2f}")
                self._last_failed_entry = time.time()
                return False
                
            logger.info(f"💰 Balance OK - Spot: ${spot_usdc:.2f}, Perp Margin: ${perp_margin:.2f}")
            
        except Exception as e:
            logger.error(f"Balance check failed: {e}")
            self._last_failed_entry = time.time()
            return False

        logger.info(f"🟢 ENTRY: Buy {size} Spot @ ${spot_ask:.4f}, Short Perp @ ${perp_bid:.4f}")
        
        if config.DRY_RUN:
            logger.info("📝 DRY RUN - Not executing")
            return False
        
        self.position_state = PositionState.PENDING
        self.trade_counter += 1
        
        # Create trade record
        self.current_trade = TradeRecord(
            id=self.trade_counter,
            entry_time=datetime.now().isoformat(),
            entry_spot_price=spot_ask,
            entry_perp_price=perp_bid,
            entry_spread=prices.get_entry_spread(),
            size=size
        )
        
        try:
            # SEQUENTIAL EXECUTION: Spot first
            logger.info("📦 Step 1/2: Placing Spot order...")
            spot_result = await self._place_order(config.SPOT_SYMBOL, True, size, spot_ask)
            spot_ok = self._check_fill(spot_result, "Spot Buy")
            
            if not spot_ok:
                logger.warning("❌ Spot order failed - aborting entry (no perp order placed)")
                self.current_trade.status = "error"
                self.current_trade.error_message = "Spot failed"
                self.data.trades.append(self.current_trade)
                self._last_failed_entry = time.time()
                self.position_state = PositionState.FLAT
                
                # Sync state to be safe
                await self._sync_state()
                return False
            
            # Spot succeeded, now do perp
            logger.info("📊 Step 2/2: Placing Perp order...")
            perp_result = await self._place_order(config.PERP_SYMBOL, False, size, perp_bid)
            perp_ok = self._check_fill(perp_result, "Perp Short")
            
            if not perp_ok:
                logger.warning("❌ Perp order failed - REVERSING spot immediately")
                # Must reverse spot
                logger.info(f"🔙 Selling {size} Spot to reverse...")
                await self._place_order(config.SPOT_SYMBOL, False, size, spot_ask * 0.95)
                
                self.current_trade.status = "error"
                self.current_trade.error_message = "Perp failed after spot success"
                self.data.trades.append(self.current_trade)
                self._last_failed_entry = time.time()
                self.position_state = PositionState.FLAT
                
                trade_events.error(f"Perp failed - Reversed spot {size} HYPE")
                
                # Mandatory sync
                await self._sync_state()
                return False
            
            # Both succeeded!
            self.position_state = PositionState.OPEN
            self.position_size = size
            self.entry_spot_price = spot_ask
            self.entry_perp_price = perp_bid
            
            # Estimate fees
            self.current_trade.fees = (spot_ask * size + perp_bid * size) * 0.00025
            
            # Log trade event
            trade_events.entry_executed(size, spot_ask, perp_bid, prices.get_entry_spread())
            
            logger.info(f"✅ ENTRY COMPLETE - Size: {size} HYPE")
            return True
                
        except Exception as e:
            logger.error(f"Entry error: {e}")
            self.current_trade.status = "error"
            self.current_trade.error_message = str(e)
            self.data.trades.append(self.current_trade)
            self._last_failed_entry = time.time()
            self.position_state = PositionState.FLAT
            
            # Mandatory sync after error
            await self._sync_state()
            return False
    
    async def execute_exit(self, prices: PriceState) -> bool:
        """Execute exit trade."""
        spot_bid = prices.spot.best_bid
        perp_ask = prices.perp.best_ask
        size = self.position_size
        
        logger.info(f"🔴 EXIT: Sell {size} Spot @ ${spot_bid:.4f}, Close Perp @ ${perp_ask:.4f}")
        
        if config.DRY_RUN:
            logger.info("📝 DRY RUN - Not executing")
            return False
        
        self.position_state = PositionState.PENDING
        
        try:
            spot_result, perp_result = await asyncio.gather(
                self._place_order(config.SPOT_SYMBOL, False, size, spot_bid),
                self._place_order(config.PERP_SYMBOL, True, size, perp_ask, reduce_only=True),
                return_exceptions=True
            )
            
            spot_ok = self._check_fill(spot_result, "Spot Sell")
            perp_ok = self._check_fill(perp_result, "Perp Close")
            
            if spot_ok and perp_ok:
                # Calculate P&L
                spot_pnl = (spot_bid - self.entry_spot_price) * size
                perp_pnl = (self.entry_perp_price - perp_ask) * size
                exit_fees = (spot_bid * size + perp_ask * size) * 0.00025
                
                self.current_trade.exit_time = datetime.now().isoformat()
                self.current_trade.exit_spot_price = spot_bid
                self.current_trade.exit_perp_price = perp_ask
                self.current_trade.exit_spread = prices.get_exit_spread()
                self.current_trade.spot_pnl = spot_pnl
                self.current_trade.perp_pnl = perp_pnl
                self.current_trade.gross_pnl = spot_pnl + perp_pnl
                self.current_trade.fees += exit_fees
                self.current_trade.net_pnl = self.current_trade.gross_pnl - self.current_trade.fees
                self.current_trade.status = "closed"
                
                self.data.trades.append(self.current_trade)
                
                # Log trade event
                trade_events.exit_executed(size, spot_bid, perp_ask, self.current_trade.net_pnl)
                
                logger.info(f"✅ EXIT COMPLETE - Net P&L: ${self.current_trade.net_pnl:+.4f}")
                
                # Reset
                self.position_state = PositionState.FLAT
                self.position_size = 0
                self.current_trade = None
                
                # Save data
                self.data.save()
                
                return True
            else:
                logger.error("⚠️ Exit partial - manual intervention needed")
                return False
                
        except Exception as e:
            logger.error(f"Exit error: {e}")
            return False
    
    async def _place_order(self, symbol: str, is_buy: bool, size: float, price: float, reduce_only: bool = False) -> Dict:
        """Place order via exchange."""
        loop = asyncio.get_event_loop()
        
        def place():
            return self.exchange.order(
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=price,
                order_type={"limit": {"tif": "Ioc"}},  # IOC to prevent stuck orders
                reduce_only=reduce_only
            )
        
        return await loop.run_in_executor(None, place)
    
    def _check_fill(self, result: Any, name: str) -> bool:
        """Check if order filled."""
        if isinstance(result, Exception):
            logger.error(f"{name} exception: {result}")
            return False
        
        if result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if "filled" in s:
                    filled = s["filled"]
                    logger.info(f"{name}: Filled {filled.get('totalSz')} @ ${filled.get('avgPx')}")
                    return True
                elif "resting" in s:
                    # Resting with IOC means it failed to fill immediately?
                    # Actually IOC should cancel if not filled.
                    # But if we see resting here, it means it didn't fill fully?
                    # Safer to treat RESTING as FAILURE for now to trigger unwind
                    # Because we don't want to manage open orders.
                    logger.warning(f"{name}: Order resting (Partial/No Fill) - treating as Fail")
                    return False
                elif "error" in s:
                    logger.error(f"{name}: {s['error']}")
                    return False
        
        logger.error(f"{name}: Unknown result - {result}")
        return False
    
    def print_summary(self):
        """Print data collection summary."""
        print("\n" + "=" * 60)
        print("📊 DATA COLLECTION SUMMARY")
        print("=" * 60)
        print(f"Run time: {self.data.start_time} to {datetime.now().isoformat()}")
        print(f"Total spread checks: {self.data.total_spread_checks}")
        print(f"Spreads above {config.MIN_SPREAD_THRESHOLD*100:.2f}%: {self.data.spreads_above_threshold}")
        print(f"Opportunities found: {self.data.opportunities_found}")
        print(f"Trades executed: {len(self.data.trades)}")
        
        if self.data.trades:
            total_pnl = sum(t.net_pnl for t in self.data.trades if t.status == "closed")
            print(f"Total Net P&L: ${total_pnl:+.4f}")
        
        print(f"\nData saved to:")
        print(f"  - {config.SPREAD_LOG_FILE}")
        print(f"  - {config.TRADE_LOG_FILE}")
        print("=" * 60)
    
    async def _sync_state(self):
        """Sync local state with exchange"""
        try:
            logger.info("🔄 Syncing state with exchange...")
            loop = asyncio.get_running_loop()
            user_state = await loop.run_in_executor(None, self.info.user_state, config.ACCOUNT_ADDRESS)
            
            for p in user_state.get('assetPositions', []):
                pos = p['position']
                if pos['coin'] == config.PERP_SYMBOL:
                    sz = float(pos['szi'])
                    if sz != 0:
                        self.position_size = abs(sz)
                        self.position_state = PositionState.OPEN
                        self.entry_perp_price = float(pos['entryPx'])
                        # Spot fallback estimate
                        self.entry_spot_price = self.entry_perp_price / (1 + config.MIN_SPREAD_THRESHOLD)
                        
                        logger.info(f"🔄 State Synced: OPEN {self.position_size} HYPE @ ~${self.entry_perp_price}")
                        
                        # Reconstruct current trade object for logging/exit logic
                        self.current_trade = TradeRecord(
                            id=0, # Unknown
                            entry_time=datetime.now().isoformat(),
                            entry_spot_price=self.entry_spot_price,
                            entry_perp_price=self.entry_perp_price,
                            entry_spread=config.MIN_SPREAD_THRESHOLD,
                            size=self.position_size
                        )
                        return
                        
            logger.info("🔄 State Synced: FLAT")
            self.position_state = PositionState.FLAT
            self.position_size = 0.0
            
        except Exception as e:
            logger.error(f"Sync error: {e}")

    async def run(self):
        """Run the bot."""
        logger.info("🚀 Starting Arbitrage Bot - DATA COLLECTION MODE")
        
        await self._sync_state()
        
        self.ws_manager = WebSocketManager(
            on_price_update=lambda p: asyncio.create_task(self.on_price_update(p))
        )
        
        if not await self.ws_manager.test_connection():
            logger.error("WebSocket connection failed")
            return
        
        try:
            # Status task
            async def status_loop():
                while True:
                    await asyncio.sleep(300)  # Every 5 min
                    self.data.save()
                    logger.info(f"📊 Status: {self.data.total_spread_checks} checks, {self.data.opportunities_found} opportunities, {len(self.data.trades)} trades")
            
            status_task = asyncio.create_task(status_loop())
            await self.ws_manager.connect()
            
        except asyncio.CancelledError:
            pass
        finally:
            self.data.save()
            self.print_summary()
            if self.ws_manager:
                await self.ws_manager.disconnect()


if __name__ == "__main__":
    bot = ArbitrageBotDataCollection()
    asyncio.run(bot.run())
