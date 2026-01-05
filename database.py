"""
Database Module for Delta Neutral Funding Bot

SQLite persistence layer for positions, funding payments, trades, and events.
Uses aiosqlite for async operations.
"""

import aiosqlite
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database for funding bot state management."""
    
    def __init__(self, db_file: str = "funding_bot.db"):
        self.db_file = db_file
    
    async def init_tables(self):
        """Initialize all database tables."""
        async with aiosqlite.connect(self.db_file) as db:
            # Table 1: Positions (State)
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
            
            # Table 2: Funding Payments (Your Salary)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS funding_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT NOT NULL,
                    position_id INTEGER,
                    amount_usdc REAL NOT NULL,
                    rate_applied REAL NOT NULL,
                    position_size REAL NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (position_id) REFERENCES positions(id)
                )
            """)
            
            # Table 3: Trade Executions (Audit Trail)
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
                    status TEXT DEFAULT 'FILLED',
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (position_id) REFERENCES positions(id)
                )
            """)
            
            # Table 4: Rebalance Events (Safety Log)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS rebalance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER,
                    event_type TEXT NOT NULL,
                    margin_ratio_before REAL,
                    margin_ratio_after REAL,
                    amount_usd REAL,
                    notes TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (position_id) REFERENCES positions(id)
                )
            """)
            
            await db.commit()
            logger.info("💾 Database initialized with all tables.")
    
    # ==================== Position Methods ====================
    
    async def create_position(self, coin: str, size: float, size_usd: float,
                               entry_spot: float, entry_perp: float) -> int:
        """Create a new open position. Returns position ID."""
        async with aiosqlite.connect(self.db_file) as db:
            cursor = await db.execute("""
                INSERT INTO positions (coin, size, size_usd, entry_price_spot, entry_price_perp)
                VALUES (?, ?, ?, ?, ?)
            """, (coin, size, size_usd, entry_spot, entry_perp))
            await db.commit()
            logger.info(f"📥 Created position #{cursor.lastrowid}: {size} {coin}")
            return cursor.lastrowid
    
    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        async with aiosqlite.connect(self.db_file) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE status = 'OPEN'"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_position_by_coin(self, coin: str) -> Optional[Dict[str, Any]]:
        """Get open position for a specific coin."""
        async with aiosqlite.connect(self.db_file) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM positions WHERE coin = ? AND status = 'OPEN'",
                (coin,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
    
    async def has_position(self, coin: str) -> bool:
        """Check if there's an open position for a coin."""
        pos = await self.get_position_by_coin(coin)
        return pos is not None
    
    async def mark_closed(self, position_id: int, reason: str,
                          exit_spot: float = 0, exit_perp: float = 0):
        """Mark a position as closed with reason."""
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                UPDATE positions 
                SET status = 'CLOSED', close_reason = ?, closed_at = ?,
                    exit_price_spot = ?, exit_price_perp = ?
                WHERE id = ?
            """, (reason, datetime.now().isoformat(), exit_spot, exit_perp, position_id))
            await db.commit()
            logger.info(f"📤 Closed position #{position_id}: {reason}")
    
    async def create_recovery_position(self, coin: str, size: float):
        """Create a recovery position for orphaned exchange positions."""
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                INSERT INTO positions (coin, size, size_usd, status, close_reason)
                VALUES (?, ?, 0, 'OPEN', 'RECOVERY_DETECTED')
            """, (coin, size))
            await db.commit()
            logger.warning(f"🔧 Created recovery position for orphaned {coin}")
    
    # ==================== Funding Log Methods ====================
    
    async def log_funding_payment(self, coin: str, position_id: int, 
                                   amount: float, rate: float, size: float):
        """Log a funding payment received."""
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                INSERT INTO funding_log (coin, position_id, amount_usdc, rate_applied, position_size)
                VALUES (?, ?, ?, ?, ?)
            """, (coin, position_id, amount, rate, size))
            await db.commit()
            logger.info(f"💰 Funding received: ${amount:.4f} for {coin}")
    
    async def get_total_funding_earned(self, position_id: Optional[int] = None) -> float:
        """Get total funding earned, optionally for specific position."""
        async with aiosqlite.connect(self.db_file) as db:
            if position_id:
                cursor = await db.execute(
                    "SELECT SUM(amount_usdc) FROM funding_log WHERE position_id = ?",
                    (position_id,)
                )
            else:
                cursor = await db.execute("SELECT SUM(amount_usdc) FROM funding_log")
            result = await cursor.fetchone()
            return result[0] or 0.0
    
    # ==================== Trade Log Methods ====================
    
    async def log_trade(self, position_id: int, coin: str, side: str, 
                        market: str, size: float, price: float, cloid: str = None):
        """Log a trade execution."""
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                INSERT INTO trades (position_id, coin, side, market, size, price, cloid)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (position_id, coin, side, market, size, price, cloid))
            await db.commit()
    
    # ==================== Rebalance Event Methods ====================
    
    async def log_rebalance_event(self, position_id: int, event_type: str,
                                   margin_before: float, margin_after: float,
                                   amount_usd: float = 0, notes: str = ""):
        """Log a rebalancing event."""
        async with aiosqlite.connect(self.db_file) as db:
            await db.execute("""
                INSERT INTO rebalance_events 
                (position_id, event_type, margin_ratio_before, margin_ratio_after, amount_usd, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (position_id, event_type, margin_before, margin_after, amount_usd, notes))
            await db.commit()
            logger.info(f"🔄 Rebalance event: {event_type} (margin {margin_before:.2%} → {margin_after:.2%})")
    
    # ==================== Stats Methods ====================
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get summary statistics."""
        async with aiosqlite.connect(self.db_file) as db:
            # Open positions count
            cursor = await db.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            open_count = (await cursor.fetchone())[0]
            
            # Total funding earned
            cursor = await db.execute("SELECT SUM(amount_usdc) FROM funding_log")
            total_funding = (await cursor.fetchone())[0] or 0
            
            # Total trades
            cursor = await db.execute("SELECT COUNT(*) FROM trades")
            trade_count = (await cursor.fetchone())[0]
            
            # Rebalance events
            cursor = await db.execute("SELECT COUNT(*) FROM rebalance_events")
            rebalance_count = (await cursor.fetchone())[0]
            
            return {
                "open_positions": open_count,
                "total_funding_usd": total_funding,
                "total_trades": trade_count,
                "rebalance_events": rebalance_count
            }
