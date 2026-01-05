"""
Notifier - Discord Webhook Notifications

Non-blocking async notifications for critical bot events.
Uses Discord webhooks for free push notifications to phone.
"""

import asyncio
import aiohttp
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    """
    Discord webhook notifier for critical bot events.
    
    All methods are async and non-blocking.
    Configure webhook URL via DISCORD_WEBHOOK_URL env var.
    """
    
    def __init__(self, webhook_url: Optional[str] = None):
        """
        Args:
            webhook_url: Discord webhook URL. Falls back to env var.
        """
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        self.enabled = bool(self.webhook_url)
        self.bot_name = "🌾 Funding Bot"
        
        if not self.enabled:
            logger.warning("⚠️ Discord notifications disabled (no webhook URL)")
    
    async def _send(self, embed: dict):
        """Send embed to Discord webhook (non-blocking)."""
        if not self.enabled:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "username": self.bot_name,
                    "embeds": [embed]
                }
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 204:
                        logger.warning(f"Discord webhook failed: {resp.status}")
        except Exception as e:
            logger.error(f"Notification error: {e}")
    
    def _fire_and_forget(self, embed: dict):
        """Fire notification without blocking."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._send(embed))
            else:
                asyncio.run(self._send(embed))
        except Exception as e:
            logger.error(f"Fire and forget error: {e}")
    
    # ========== EVENT METHODS ==========
    
    def startup(self, wallet: str, mode: str, size: float):
        """Bot started."""
        embed = {
            "title": "🚀 Bot Started",
            "color": 0x00ff00,  # Green
            "fields": [
                {"name": "Wallet", "value": f"`{wallet[:10]}...`", "inline": True},
                {"name": "Mode", "value": mode, "inline": True},
                {"name": "Max Size", "value": f"${size}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def shutdown(self, reason: str = "Manual"):
        """Bot stopped."""
        embed = {
            "title": "🛑 Bot Stopped",
            "color": 0xffff00,  # Yellow
            "description": f"Reason: {reason}",
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def panic_triggered(self, positions: int, reason: str):
        """Panic switch activated."""
        embed = {
            "title": "🚨 PANIC SWITCH TRIGGERED",
            "color": 0xff0000,  # Red
            "description": f"**Reason:** {reason}",
            "fields": [
                {"name": "Positions Closed", "value": str(positions), "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def error(self, error_type: str, message: str, fatal: bool = False):
        """Error occurred."""
        embed = {
            "title": "❌ FATAL ERROR" if fatal else "⚠️ Error",
            "color": 0xff0000 if fatal else 0xff9900,
            "fields": [
                {"name": "Type", "value": error_type, "inline": True},
                {"name": "Message", "value": f"```{message[:500]}```", "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def trade_entry(self, coin: str, size_usd: float, 
                     spot_price: float, perp_price: float, 
                     funding_apr: float):
        """Position opened."""
        embed = {
            "title": "📈 Position Opened",
            "color": 0x00ff00,
            "fields": [
                {"name": "Coin", "value": coin, "inline": True},
                {"name": "Size", "value": f"${size_usd:.2f}", "inline": True},
                {"name": "Funding APR", "value": f"{funding_apr:.1f}%", "inline": True},
                {"name": "Spot Entry", "value": f"${spot_price:.4f}", "inline": True},
                {"name": "Perp Entry", "value": f"${perp_price:.4f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def trade_exit(self, coin: str, size_usd: float, 
                    pnl: float, reason: str):
        """Position closed."""
        color = 0x00ff00 if pnl >= 0 else 0xff0000
        embed = {
            "title": "📉 Position Closed",
            "color": color,
            "fields": [
                {"name": "Coin", "value": coin, "inline": True},
                {"name": "Size", "value": f"${size_usd:.2f}", "inline": True},
                {"name": "PnL", "value": f"${pnl:+.4f}", "inline": True},
                {"name": "Reason", "value": reason, "inline": False},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def funding_received(self, coin: str, amount: float, total: float):
        """Funding payment received."""
        embed = {
            "title": "💰 Funding Received",
            "color": 0x00ff00,
            "fields": [
                {"name": "Coin", "value": coin, "inline": True},
                {"name": "Amount", "value": f"${amount:.4f}", "inline": True},
                {"name": "Total Earned", "value": f"${total:.4f}", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def margin_warning(self, margin_ratio: float, action: str):
        """Margin getting low."""
        embed = {
            "title": "⚠️ Margin Warning",
            "color": 0xff9900,  # Orange
            "fields": [
                {"name": "Margin Ratio", "value": f"{margin_ratio:.1%}", "inline": True},
                {"name": "Action", "value": action, "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)
    
    def opportunity_found(self, coin: str, funding_apr: float, net_apy: float):
        """New trading opportunity detected."""
        embed = {
            "title": "🎯 Opportunity Found",
            "color": 0x00ffff,  # Cyan
            "fields": [
                {"name": "Coin", "value": coin, "inline": True},
                {"name": "Funding APR", "value": f"{funding_apr:.1f}%", "inline": True},
                {"name": "Net APY", "value": f"{net_apy:.1f}%", "inline": True},
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        self._fire_and_forget(embed)


# Singleton instance
_notifier: Optional[Notifier] = None

def get_notifier() -> Notifier:
    """Get or create the global notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier
