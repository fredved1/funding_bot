"""
DatabaseLogger - Async Queue-Based SQLite Logging

Cold path logging via asyncio.Queue with dedicated consumer coroutine.
Never blocks the hot path.
"""

import asyncio
import aiosqlite
import logging
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LogEvent:
    """Event to be logged to database."""
    event_type: str
    data: Dict[str, Any]
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()


class DatabaseLogger:
    """
    Cold path logging via asyncio.Queue.
    
    Design:
    - log() is non-blocking, just puts on queue
    - start_consumer() runs as background task
    - All SQLite I/O happens in consumer, never in hot path
    """
    
    def __init__(self, db_file: str = "funding_bot.db"):
        self.db_file = db_file
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
    
    def log(self, event_type: str, data: Dict[str, Any]):
        """
        Non-blocking log. Called from hot path.
        
        Just puts event on queue - never blocks.
        """
        try:
            self._queue.put_nowait(LogEvent(event_type, data))
        except asyncio.QueueFull:
            logger.warning("Log queue full, dropping event")
    
    def log_trade(self, position_id: int, coin: str, side: str, 
                  market: str, size: float, price: float, cloid: str = None):
        """Log a trade execution."""
        self.log("trade", {
            "position_id": position_id,
            "coin": coin,
            "side": side,
            "market": market,
            "size": size,
            "price": price,
            "cloid": cloid
        })
    
    def log_funding(self, coin: str, position_id: int, 
                    amount: float, rate: float, size: float):
        """Log a funding payment received."""
        self.log("funding", {
            "coin": coin,
            "position_id": position_id,
            "amount": amount,
            "rate": rate,
            "size": size
        })
    
    def log_rebalance(self, position_id: int, event_type: str,
                       margin_before: float, margin_after: float,
                       amount_usd: float = 0, notes: str = ""):
        """Log a rebalance event."""
        self.log("rebalance", {
            "position_id": position_id,
            "event_type": event_type,
            "margin_before": margin_before,
            "margin_after": margin_after,
            "amount_usd": amount_usd,
            "notes": notes
        })
    
    def log_position_open(self, coin: str, size: float, size_usd: float,
                           entry_spot: float, entry_perp: float) -> None:
        """Log position opening."""
        self.log("position_open", {
            "coin": coin,
            "size": size,
            "size_usd": size_usd,
            "entry_spot": entry_spot,
            "entry_perp": entry_perp
        })
    
    def log_position_close(self, position_id: int, reason: str,
                            exit_spot: float = 0, exit_perp: float = 0):
        """Log position closing."""
        self.log("position_close", {
            "position_id": position_id,
            "reason": reason,
            "exit_spot": exit_spot,
            "exit_perp": exit_perp
        })
    
    async def start_consumer(self):
        """
        Run as background task. Processes log events.
        
        All database I/O happens here, off the hot path.
        """
        self._running = True
        logger.info("💾 Database consumer started")
        
        async with aiosqlite.connect(self.db_file) as db:
            await self._init_tables(db)
            
            while self._running:
                try:
                    # Wait for event with timeout for graceful shutdown
                    event = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                    await self._process_event(db, event)
                    self._queue.task_done()
                    
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Database consumer error: {e}")
    
    async def stop(self):
        """Stop the consumer gracefully."""
        self._running = False
        # Process remaining events
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                async with aiosqlite.connect(self.db_file) as db:
                    await self._process_event(db, event)
            except:
                break
        logger.info("💾 Database consumer stopped")
    
    async def _init_tables(self, db):
        """Initialize database tables."""
        # Positions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                size REAL NOT NULL,
                size_usd REAL NOT NULL,
                entry_price_spot REAL,
                entry_price_perp REAL,
                exit_price_spot REAL,
                exit_price_perp REAL,
                status TEXT DEFAULT 'OPEN',
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                close_reason TEXT
            )
        """)
        
        # Funding payments
        await db.execute("""
            CREATE TABLE IF NOT EXISTS funding_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                position_id INTEGER,
                amount_usdc REAL NOT NULL,
                rate_applied REAL NOT NULL,
                position_size REAL NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Trades
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                market TEXT NOT NULL,
                size REAL NOT NULL,
                price REAL NOT NULL,
                cloid TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Rebalance events
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rebalance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                event_type TEXT NOT NULL,
                margin_ratio_before REAL,
                margin_ratio_after REAL,
                amount_usd REAL,
                notes TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.commit()
        logger.info("💾 Database tables initialized")
    
    async def _process_event(self, db, event: LogEvent):
        """Process a single log event."""
        try:
            if event.event_type == "trade":
                await db.execute("""
                    INSERT INTO trades (position_id, coin, side, market, size, price, cloid)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.data.get("position_id"),
                    event.data.get("coin"),
                    event.data.get("side"),
                    event.data.get("market"),
                    event.data.get("size"),
                    event.data.get("price"),
                    event.data.get("cloid")
                ))
                
            elif event.event_type == "funding":
                await db.execute("""
                    INSERT INTO funding_log (coin, position_id, amount_usdc, rate_applied, position_size)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    event.data.get("coin"),
                    event.data.get("position_id"),
                    event.data.get("amount"),
                    event.data.get("rate"),
                    event.data.get("size")
                ))
                
            elif event.event_type == "rebalance":
                await db.execute("""
                    INSERT INTO rebalance_events 
                    (position_id, event_type, margin_ratio_before, margin_ratio_after, amount_usd, notes)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    event.data.get("position_id"),
                    event.data.get("event_type"),
                    event.data.get("margin_before"),
                    event.data.get("margin_after"),
                    event.data.get("amount_usd"),
                    event.data.get("notes")
                ))
                
            elif event.event_type == "position_open":
                await db.execute("""
                    INSERT INTO positions (coin, size, size_usd, entry_price_spot, entry_price_perp)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    event.data.get("coin"),
                    event.data.get("size"),
                    event.data.get("size_usd"),
                    event.data.get("entry_spot"),
                    event.data.get("entry_perp")
                ))
                
            elif event.event_type == "position_close":
                await db.execute("""
                    UPDATE positions 
                    SET status = 'CLOSED', close_reason = ?, closed_at = CURRENT_TIMESTAMP,
                        exit_price_spot = ?, exit_price_perp = ?
                    WHERE id = ?
                """, (
                    event.data.get("reason"),
                    event.data.get("exit_spot"),
                    event.data.get("exit_perp"),
                    event.data.get("position_id")
                ))
            
            await db.commit()
            
        except Exception as e:
            logger.error(f"Failed to process event {event.event_type}: {e}")
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get database statistics (for dashboard)."""
        async with aiosqlite.connect(self.db_file) as db:
            # Total funding earned
            cursor = await db.execute("SELECT SUM(amount_usdc) FROM funding_log")
            total_funding = (await cursor.fetchone())[0] or 0
            
            # Trade count
            cursor = await db.execute("SELECT COUNT(*) FROM trades")
            trade_count = (await cursor.fetchone())[0]
            
            # Open positions
            cursor = await db.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            open_positions = (await cursor.fetchone())[0]
            
            return {
                "total_funding_usd": round(total_funding, 4),
                "total_trades": trade_count,
                "open_positions": open_positions
            }
