"""
HyperliquidClient - SDK Wrapper for Hyperliquid API

Provides unified interface for Spot and Perp trading on Hyperliquid.
Handles SDK initialization, order placement, and state queries.
"""

import asyncio
import logging
import requests
from typing import Dict, Any, Optional
from uuid import uuid4

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

import config

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Unified client for Hyperliquid Spot and Perp trading.
    
    Wraps the hyperliquid-python-sdk with async compatibility.
    """
    
    def __init__(self):
        """Initialize SDK clients."""
        self.account = Account.from_key(config.PRIVATE_KEY)
        self.address = config.ACCOUNT_ADDRESS
        
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)
        self.exchange = Exchange(
            self.account,
            constants.MAINNET_API_URL,
            account_address=self.address
        )
        
        # Cache for meta info
        self._meta_cache = None
        self._sz_decimals = {}
        
        logger.info(f"📡 Client initialized for {self.address[:10]}...")
    
    async def place_order(self, coin: str, side: str, is_buy: bool,
                          size: float, price: float, cloid: str) -> Dict[str, Any]:
        """
        Place an order on Spot or Perp market.
        
        Args:
            coin: Asset name (e.g., "HYPE")
            side: "spot" or "perp"
            is_buy: True for buy, False for sell
            size: Order size in base asset
            price: Limit price
            cloid: Client order ID
        
        Returns:
            {"status": "filled"|"failed", "filled_size": float}
        """
        loop = asyncio.get_event_loop()
        
        # Get symbol for order
        symbol = self._get_symbol(coin, side)
        
        # Round size to proper decimals
        size = self._round_size(coin, size)
        
        logger.info(f"📤 Placing {side} order: symbol={symbol}, is_buy={is_buy}, size={size}, price={price}")
        
        def _place():
            try:
                result = self.exchange.order(
                    name=symbol,
                    is_buy=is_buy,
                    sz=size,
                    limit_px=price,
                    order_type={"limit": {"tif": "Ioc"}},  # IOC for immediate fill
                    reduce_only=False  # Fixed: was buggy logic
                )
                logger.info(f"📥 Order response: {result}")
                return self._parse_order_result(result)
            except Exception as e:
                logger.error(f"Order placement error: {e}", exc_info=True)
                return {"status": "failed", "error": str(e)}
        
        return await loop.run_in_executor(None, _place)
    
    async def cancel_order(self, coin: str, cloid: str) -> bool:
        """Cancel an order by client order ID."""
        loop = asyncio.get_event_loop()
        
        def _cancel():
            try:
                # For IOC orders this shouldn't be needed, but just in case
                result = self.exchange.cancel(coin, cloid)
                return result.get("status") == "ok"
            except Exception as e:
                logger.error(f"Cancel error: {e}")
                return False
        
        return await loop.run_in_executor(None, _cancel)
    
    async def query_order_status(self, coin: str, cloid: str) -> Dict[str, Any]:
        """Query status of an order by client order ID."""
        loop = asyncio.get_event_loop()
        
        def _query():
            try:
                # This is a simplified version - actual implementation depends on SDK
                orders = self.info.open_orders(self.address)
                for order in orders:
                    if order.get("cloid") == cloid:
                        return {"status": "open", "filled_size": 0}
                # If not in open orders, assume filled or cancelled
                return {"status": "filled", "filled_size": 0}  # Simplified
            except Exception as e:
                logger.error(f"Query error: {e}")
                return {"status": "unknown"}
        
        return await loop.run_in_executor(None, _query)
    
    async def get_prices(self, coin: str) -> Dict[str, float]:
        """Get current bid/ask prices for spot and perp."""
        loop = asyncio.get_event_loop()
        
        def _get():
            try:
                mids = self.info.all_mids()
                perp_mid = float(mids.get(coin, 0))
                
                # Get L2 for better prices
                spot_book = self.info.l2_snapshot(config.SPOT_SYMBOL)
                perp_book = self.info.l2_snapshot(coin)
                
                return {
                    "spot_bid": float(spot_book["levels"][0][0]["px"]) if spot_book["levels"][0] else perp_mid,
                    "spot_ask": float(spot_book["levels"][1][0]["px"]) if spot_book["levels"][1] else perp_mid,
                    "perp_bid": float(perp_book["levels"][0][0]["px"]) if perp_book["levels"][0] else perp_mid,
                    "perp_ask": float(perp_book["levels"][1][0]["px"]) if perp_book["levels"][1] else perp_mid,
                }
            except Exception as e:
                logger.error(f"Price fetch error: {e}")
                return {"spot_bid": 0, "spot_ask": 0, "perp_bid": 0, "perp_ask": 0}
        
        return await loop.run_in_executor(None, _get)
    
    async def get_balances(self) -> Dict[str, float]:
        """Get USDC balances for spot and perp accounts."""
        loop = asyncio.get_event_loop()
        
        def _get():
            try:
                # Spot balance
                spot_state = requests.post(
                    'https://api.hyperliquid.xyz/info',
                    json={'type': 'spotClearinghouseState', 'user': self.address},
                    timeout=5
                ).json()
                
                spot_usdc = sum(
                    float(b.get('total', 0)) 
                    for b in spot_state.get('balances', []) 
                    if b.get('coin') == 'USDC'
                )
                
                # Perp balance
                perp_state = requests.post(
                    'https://api.hyperliquid.xyz/info',
                    json={'type': 'clearinghouseState', 'user': self.address},
                    timeout=5
                ).json()
                
                perp_margin = float(perp_state.get('withdrawable', 0))
                
                return {"spot_usdc": spot_usdc, "perp_margin": perp_margin}
                
            except Exception as e:
                logger.error(f"Balance fetch error: {e}")
                return {"spot_usdc": 0, "perp_margin": 0}
        
        return await loop.run_in_executor(None, _get)
    
    async def get_positions(self) -> Dict[str, Dict]:
        """Get all open perp positions."""
        loop = asyncio.get_event_loop()
        
        def _get():
            try:
                state = self.info.user_state(self.address)
                positions = {}
                
                for p in state.get('assetPositions', []):
                    pos = p['position']
                    size = float(pos.get('szi', 0))
                    if size != 0:
                        positions[pos['coin']] = {
                            "size": abs(size),
                            "side": "short" if size < 0 else "long",
                            "entry_price": float(pos.get('entryPx', 0)),
                            "liquidation_price": float(pos.get('liquidationPx', 0)),
                            "unrealized_pnl": float(pos.get('unrealizedPnl', 0))
                        }
                
                return positions
                
            except Exception as e:
                logger.error(f"Position fetch error: {e}")
                return {}
        
        return await loop.run_in_executor(None, _get)
    
    async def get_funding_rate(self, coin: str) -> float:
        """Get current funding rate for a coin."""
        loop = asyncio.get_event_loop()
        
        def _get():
            try:
                # Use metaAndAssetCtxs which contains actual funding rate
                result = requests.post(
                    'https://api.hyperliquid.xyz/info',
                    json={'type': 'metaAndAssetCtxs'},
                    timeout=5
                ).json()
                
                meta, asset_ctxs = result[0], result[1]
                for i, asset in enumerate(meta.get('universe', [])):
                    if asset.get('name') == coin:
                        ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                        return float(ctx.get('funding', 0))
                return 0.0
            except Exception as e:
                logger.error(f"Funding rate error: {e}")
                return 0.0
        
        return await loop.run_in_executor(None, _get)
    
    def _get_symbol(self, coin: str, side: str) -> str:
        """Get the correct symbol for spot or perp."""
        if side == "spot":
            return config.SPOT_SYMBOL  # e.g., "@107" for HYPE spot
        else:
            return coin  # e.g., "HYPE" for perp
    
    def _round_size(self, coin: str, size: float) -> float:
        """Round size to proper decimals for the coin."""
        if coin not in self._sz_decimals:
            try:
                meta = self.info.meta()
                for asset in meta.get("universe", []):
                    if asset.get("name") == coin:
                        self._sz_decimals[coin] = asset.get("szDecimals", 2)
                        break
            except:
                self._sz_decimals[coin] = 2
        
        decimals = self._sz_decimals.get(coin, 2)
        return round(size, decimals)
    
    def _parse_order_result(self, result: Dict) -> Dict[str, Any]:
        """Parse SDK order result into standardized format."""
        if result.get("status") != "ok":
            return {"status": "failed", "error": str(result)}
        
        response = result.get("response", {})
        data = response.get("data", {})
        statuses = data.get("statuses", [])
        
        for s in statuses:
            if "filled" in s:
                filled = s["filled"]
                return {
                    "status": "filled",
                    "filled_size": float(filled.get("totalSz", 0)),
                    "avg_price": float(filled.get("avgPx", 0)),
                    "oid": filled.get("oid")
                }
            elif "error" in s:
                return {"status": "failed", "error": s["error"]}
        
        return {"status": "failed", "error": "Unknown response format"}
