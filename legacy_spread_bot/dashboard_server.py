"""
Dashboard Server for Delta Neutral Arbitrage Bot

Provides a web interface to monitor:
- Real-time Spot & Perp prices
- Current spread
- Position status
- Trade history
- P&L overview
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Any, List
from dataclasses import dataclass, field, asdict
import threading
import time

from aiohttp import web
import aiohttp_cors

import config
from websocket_manager import WebSocketManager, PriceState
from trade_events import trade_events

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL))
logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Record of a single trade."""
    timestamp: str
    action: str  # "ENTRY" or "EXIT"
    spot_price: float
    perp_price: float
    size: float
    spread: float
    pnl: float = 0.0


@dataclass
class DashboardState:
    """Holds all dashboard state data."""
    # Current prices
    spot_bid: float = 0.0
    spot_ask: float = 0.0
    perp_bid: float = 0.0
    perp_ask: float = 0.0
    
    # Spread info
    entry_spread: float = 0.0
    exit_spread: float = 0.0
    spread_threshold: float = config.MIN_SPREAD_THRESHOLD
    
    # Position
    has_position: bool = False
    position_size: float = 0.0
    entry_spot_price: float = 0.0
    entry_perp_price: float = 0.0
    entry_time: str = ""
    unrealized_pnl: float = 0.0
    
    # Statistics
    opportunities_found: int = 0
    trades_executed: int = 0
    total_pnl: float = 0.0
    funding_rate: float = 0.0
    
    # Config
    dry_run: bool = config.DRY_RUN
    max_position_usd: float = config.MAX_POSITION_USD
    
    # Account
    account_equity: float = 0.0
    spot_value: float = 0.0
    perp_value: float = 0.0
    
    # Status
    bot_running: bool = False
    ws_connected: bool = False
    last_update: str = ""
    
    # Trade history
    trade_history: List[Dict] = field(default_factory=list)
    
    # Price history (last 100 points for chart)
    price_history: List[Dict] = field(default_factory=list)


class DashboardServer:
    """
    Web server for the arbitrage bot dashboard.
    
    Provides:
    - REST API for current state
    - WebSocket for real-time updates
    - Static file serving for frontend
    """
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.state = DashboardState()
        self.ws_clients: List[web.WebSocketResponse] = []
        self.ws_manager: WebSocketManager = None
        self._running = False
        self._price_history_max = 100
        self._last_position_fetch = 0
        
    async def _fetch_positions(self, prices: PriceState):
        """Fetch real positions from Hyperliquid API."""
        import requests
        
        now = time.time()
        # Only fetch every 5 seconds to avoid rate limits
        if now - self._last_position_fetch < 5:
            # Just calculate P&L with existing data
            if self.state.has_position:
                spot_pnl = (prices.spot.best_bid - self.state.entry_spot_price) * self.state.position_size
                perp_pnl = (self.state.entry_perp_price - prices.perp.best_ask) * self.state.position_size
                self.state.unrealized_pnl = spot_pnl + perp_pnl
            self._update_chart_and_broadcast(prices)
            return
            
        self._last_position_fetch = now
        
        try:
            # Fetch perp positions
            resp = requests.post('https://api.hyperliquid.xyz/info', 
                json={'type': 'clearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5)
            perp_data = resp.json()
            
            # Fetch spot balances
            resp = requests.post('https://api.hyperliquid.xyz/info',
                json={'type': 'spotClearinghouseState', 'user': config.ACCOUNT_ADDRESS},
                timeout=5)
            spot_data = resp.json()
            
            # Check for HYPE perp position
            perp_position = None
            for pos in perp_data.get('assetPositions', []):
                p = pos.get('position', {})
                if p.get('coin') == 'HYPE':
                    perp_position = p
                    break
            
            # Check for HYPE spot balance
            hype_spot = 0.0
            for b in spot_data.get('balances', []):
                if b.get('coin') == 'HYPE':
                    hype_spot = float(b.get('total', 0))
                    break
            
            # Update state
            if perp_position and float(perp_position.get('szi', 0)) != 0:
                self.state.has_position = True
                size = abs(float(perp_position.get('szi', 0)))
                self.state.position_size = size
                
                # Get true entry details from trade events
                te_stats = trade_events.get_stats()
                current_pos = te_stats.get("current_position")
                
                if current_pos:
                    self.state.entry_spot_price = current_pos.get("entry_spot", 0)
                    self.state.entry_perp_price = current_pos.get("entry_perp", 0)
                    self.state.entry_time = current_pos.get("entry_time", "")
                else:
                    # Fallback if no trade event found (should not happen in normal flow)
                    self.state.entry_perp_price = float(perp_position.get('entryPx', 0))
                    # Fallback spot entry: assume spread was around threshold at entry
                    self.state.entry_spot_price = self.state.entry_perp_price / (1 + config.MIN_SPREAD_THRESHOLD)

                # Calculate TOTAL PnL (Spot + Perp)
                # Spot PnL = (Current Bid - Entry) * Size
                # Perp PnL = API reported unrealized PnL
                
                spot_pnl = (prices.spot.best_bid - self.state.entry_spot_price) * size
                perp_pnl = float(perp_position.get('unrealizedPnl', 0))
                
                self.state.unrealized_pnl = spot_pnl + perp_pnl
            else:
                self.state.has_position = False
                self.state.position_size = 0
            
            # Fetch Account Equity (Total Value)
            perp_equity = float(perp_data.get('marginSummary', {}).get('accountValue', 0))
            spot_equity = sum(float(b.get('total', 0)) * prices.spot.best_bid 
                             for b in spot_data.get('balances', []) 
                             if b.get('coin') == 'HYPE')
            spot_equity += sum(float(b.get('total', 0)) 
                              for b in spot_data.get('balances', []) 
                              if b.get('coin') == 'USDC')
            
            self.state.perp_value = perp_equity
            self.state.spot_value = spot_equity
            self.state.account_equity = perp_equity + spot_equity
            
            # Fetch funding rate
            try:
                resp = requests.post('https://api.hyperliquid.xyz/info',
                    json={'type': 'meta'},
                    timeout=5)
                meta = resp.json()
                for asset in meta.get('universe', []):
                    if asset.get('name') == 'HYPE':
                        self.state.funding_rate = float(asset.get('funding', 0))
                        break
            except Exception as e:
                logger.error(f"Funding rate fetch error: {e}")
                
        except Exception as e:
            logger.error(f"Position fetch error: {e}")
        
        self._update_chart_and_broadcast(prices)
    
    def _update_chart_and_broadcast(self, prices: PriceState):
        """Update chart data and broadcast to clients."""
        now = datetime.now().isoformat()
        
        # Check for opportunity
        if self.state.entry_spread > config.MIN_SPREAD_THRESHOLD:
            self.state.opportunities_found += 1
        
        # Add to price history
        self.state.price_history.append({
            "time": now,
            "spot": prices.spot.best_ask,
            "perp": prices.perp.best_bid,
            "spread": self.state.entry_spread * 100
        })
        
        # Trim history
        if len(self.state.price_history) > self._price_history_max:
            self.state.price_history = self.state.price_history[-self._price_history_max:]
        
        # Broadcast to WebSocket clients
        asyncio.create_task(self._broadcast_state())
        
    async def start(self):
        """Start the dashboard server."""
        app = web.Application()
        
        # Setup CORS
        cors = aiohttp_cors.setup(app, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
        
        # Routes
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/api/state", self.handle_get_state)
        app.router.add_get("/api/history", self.handle_get_history)
        app.router.add_get("/ws", self.handle_websocket)
        app.router.add_static("/static", "./static", show_index=True)
        
        # Apply CORS to API routes
        for route in list(app.router.routes()):
            if "/api" in str(route.resource):
                cors.add(route)
        
        # Start WebSocket manager for price data
        self.ws_manager = WebSocketManager(on_price_update=self._on_price_update)
        
        self._running = True
        
        # Start price feed in background
        asyncio.create_task(self._run_price_feed())
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        logger.info(f"🖥️  Dashboard running at http://localhost:{self.port}")
        
        # Keep running
        while self._running:
            await asyncio.sleep(1)
    
    async def _run_price_feed(self):
        """Run the WebSocket price feed."""
        try:
            self.state.ws_connected = True
            await self.ws_manager.connect()
        except Exception as e:
            logger.error(f"Price feed error: {e}")
            self.state.ws_connected = False
    
    def _on_price_update(self, prices: PriceState):
        """Handle price updates from WebSocket."""
        now = datetime.now().isoformat()
        
        # Update state
        self.state.spot_bid = prices.spot.best_bid
        self.state.spot_ask = prices.spot.best_ask
        self.state.perp_bid = prices.perp.best_bid
        self.state.perp_ask = prices.perp.best_ask
        self.state.entry_spread = prices.get_entry_spread()
        self.state.exit_spread = prices.get_exit_spread()
        self.state.last_update = now
        self.state.ws_connected = True
        self.state.bot_running = True
        
        # Fetch real positions from Hyperliquid every 5 seconds
        asyncio.create_task(self._fetch_positions(prices))
    
    async def _broadcast_state(self):
        """Broadcast state to all WebSocket clients."""
        if not self.ws_clients:
            return
            
        state_dict = self._get_state_dict()
        message = json.dumps(state_dict)
        
        dead_clients = []
        for ws in self.ws_clients:
            try:
                await ws.send_str(message)
            except:
                dead_clients.append(ws)
        
        # Remove dead clients
        for ws in dead_clients:
            self.ws_clients.remove(ws)
    
    def _get_state_dict(self) -> Dict[str, Any]:
        """Get state as dictionary for JSON serialization."""
        # Get trade events stats
        te_stats = trade_events.get_stats()
        te_events = trade_events.get_events(10)  # Last 10 events
        
        return {
            "prices": {
                "spot_bid": self.state.spot_bid,
                "spot_ask": self.state.spot_ask,
                "perp_bid": self.state.perp_bid,
                "perp_ask": self.state.perp_ask,
            },
            "spread": {
                "entry": self.state.entry_spread * 100,
                "exit": self.state.exit_spread * 100,
                "threshold": self.state.spread_threshold * 100,
                "is_opportunity": self.state.entry_spread > self.state.spread_threshold,
            },
            "position": {
                "has_position": self.state.has_position,
                "size": self.state.position_size,
                "entry_spot": self.state.entry_spot_price,
                "entry_perp": self.state.entry_perp_price,
                "entry_time": self.state.entry_time,
                "unrealized_pnl": self.state.unrealized_pnl,
            },
            "stats": {
                "opportunities": self.state.opportunities_found,
                "trades": te_stats.get("trades_executed", 0),
                "total_pnl": te_stats.get("total_pnl", 0.0),
                "funding_rate": self.state.funding_rate,
            },
            "config": {
                "dry_run": self.state.dry_run,
                "max_position": self.state.max_position_usd,
                "spot_symbol": config.SPOT_SYMBOL,
                "perp_symbol": config.PERP_SYMBOL,
            },
            "status": {
                "bot_running": self.state.bot_running,
                "ws_connected": self.state.ws_connected,
                "last_update": self.state.last_update,
            },
            "account": {
                "total": self.state.account_equity,
                "spot": self.state.spot_value,
                "perp": self.state.perp_value,
            },
            "trade_events": te_events,
            "spread_log": self._get_spread_log_summary()
        }
    
    def _get_spread_log_summary(self) -> Dict:
        """Get spread log summary from file."""
        import os
        try:
            if os.path.exists(config.SPREAD_LOG_FILE):
                with open(config.SPREAD_LOG_FILE, 'r') as f:
                    data = json.load(f)
                    return {
                        "start_time": data.get("start_time", ""),
                        "total_checks": data.get("total_checks", 0),
                        "above_threshold": data.get("above_threshold", 0),
                        "threshold": data.get("threshold", 0),
                        "data_points": len(data.get("data", []))
                    }
        except:
            pass
        return {"total_checks": 0, "above_threshold": 0, "data_points": 0}
    
    async def handle_index(self, request):
        """Serve the main dashboard page."""
        return web.FileResponse("./static/index.html")
    
    async def handle_get_state(self, request):
        """Get current state via REST API."""
        return web.json_response(self._get_state_dict())
    
    async def handle_get_history(self, request):
        """Get price history for charts."""
        return web.json_response({
            "prices": self.state.price_history,
            "trades": self.state.trade_history
        })
    
    async def handle_websocket(self, request):
        """Handle WebSocket connections for real-time updates."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self.ws_clients.append(ws)
        logger.info(f"Dashboard client connected. Total: {len(self.ws_clients)}")
        
        # Send initial state
        await ws.send_str(json.dumps(self._get_state_dict()))
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Handle commands from frontend
                    data = json.loads(msg.data)
                    if data.get("command") == "ping":
                        await ws.send_str(json.dumps({"pong": True}))
        except:
            pass
        finally:
            if ws in self.ws_clients:
                self.ws_clients.remove(ws)
            logger.info(f"Dashboard client disconnected. Total: {len(self.ws_clients)}")
        
        return ws
    
    def add_trade(self, trade: TradeRecord):
        """Add a trade to history."""
        self.state.trade_history.append(asdict(trade))
        if trade.action == "EXIT":
            self.state.total_pnl += trade.pnl
            self.state.trades_executed += 1


async def main():
    """Run the dashboard server."""
    server = DashboardServer(port=8080)
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())
