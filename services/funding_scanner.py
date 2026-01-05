"""
FundingScanner - Opportunity Detection with Break-Even Validation

Scans Hyperliquid for coins with high funding rates and sufficient liquidity.
Includes break-even validation to avoid fee traps.
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

import config

logger = logging.getLogger(__name__)


@dataclass
class FundingOpportunity:
    """A validated funding opportunity."""
    coin: str
    funding_rate_hourly: float
    funding_apr: float
    liquidity_usd: float
    days_to_breakeven: float
    net_apy: float
    viable: bool
    reason: str = ""


class FundingScanner:
    """
    Scans for funding rate opportunities and validates profitability.
    
    Combines scanner + validator into one robust module.
    """
    
    def __init__(self, client, 
                 min_apr: float = 0.20,
                 min_liquidity_usd: float = 1_000_000,
                 max_breakeven_days: float = 5.0):
        """
        Args:
            client: HyperliquidClient instance
            min_apr: Minimum annualized funding rate (0.20 = 20%)
            min_liquidity_usd: Minimum 24h volume in USD
            max_breakeven_days: Maximum days acceptable to break even on fees
        """
        self.client = client
        self.min_apr = min_apr
        self.min_liquidity_usd = min_liquidity_usd
        self.max_breakeven_days = max_breakeven_days
        
        # Fee structure (Hyperliquid)
        self.fee_spot_taker = 0.0004  # 0.04%
        self.fee_perp_taker = 0.0003  # 0.03%
        self.slippage_estimate = 0.001  # 0.1%
        
        # Calculate roundtrip cost
        one_way = self.fee_spot_taker + self.fee_perp_taker + (self.slippage_estimate * 2)
        self.roundtrip_cost = one_way * 2
        
        # Cache
        self._last_scan = 0
        self._scan_cache: List[FundingOpportunity] = []
        self._cache_ttl = 60  # seconds
    
    async def scan(self, force: bool = False) -> List[FundingOpportunity]:
        """
        Scan for funding opportunities.
        
        Args:
            force: Bypass cache
        
        Returns:
            List of viable opportunities, sorted by APY
        """
        # Check cache
        if not force and (time.time() - self._last_scan) < self._cache_ttl:
            return self._scan_cache
        
        logger.info("🔍 Scanning for funding opportunities...")
        
        opportunities = []
        
        try:
            # Get all funding rates
            funding_rates = await self._get_all_funding_rates()
            
            for coin, rate in funding_rates.items():
                # Skip if funding is negative (shorts pay)
                if rate <= 0:
                    continue
                
                # Calculate APR
                apr = rate * 24 * 365
                
                # Check minimum APR
                if apr < self.min_apr:
                    continue
                
                # Check liquidity
                liquidity = await self._get_liquidity(coin)
                if liquidity < self.min_liquidity_usd:
                    opportunities.append(FundingOpportunity(
                        coin=coin,
                        funding_rate_hourly=rate,
                        funding_apr=apr,
                        liquidity_usd=liquidity,
                        days_to_breakeven=999,
                        net_apy=0,
                        viable=False,
                        reason=f"Low liquidity: ${liquidity:,.0f}"
                    ))
                    continue
                
                # Validate break-even
                validation = self._validate_opportunity(rate, apr)
                
                opportunities.append(FundingOpportunity(
                    coin=coin,
                    funding_rate_hourly=rate,
                    funding_apr=apr,
                    liquidity_usd=liquidity,
                    days_to_breakeven=validation["days_to_breakeven"],
                    net_apy=validation["net_apy"],
                    viable=validation["viable"],
                    reason=validation.get("reason", "")
                ))
            
            # Sort by net APY descending, viable first
            opportunities.sort(key=lambda x: (x.viable, x.net_apy), reverse=True)
            
            # Update cache
            self._scan_cache = opportunities
            self._last_scan = time.time()
            
            # Log summary
            viable = [o for o in opportunities if o.viable]
            logger.info(f"📊 Found {len(viable)} viable opportunities out of {len(opportunities)} scanned")
            
            for o in viable[:5]:  # Top 5
                logger.info(f"  ✅ {o.coin}: APR {o.funding_apr*100:.1f}%, Net APY {o.net_apy:.1f}%, BE: {o.days_to_breakeven:.1f}d")
            
            return opportunities
            
        except Exception as e:
            logger.error(f"Scan error: {e}")
            return []
    
    async def get_best_opportunity(self) -> Optional[FundingOpportunity]:
        """Get the single best opportunity right now."""
        opportunities = await self.scan()
        viable = [o for o in opportunities if o.viable]
        return viable[0] if viable else None
    
    def _validate_opportunity(self, hourly_rate: float, apr: float) -> Dict:
        """
        Validate if an opportunity is profitable after fees.
        
        Uses 40% capital efficiency (due to 40/40/20 split).
        """
        # Effective position is only 40% of capital (rest is margin + buffer)
        capital_efficiency = 0.40
        
        # Daily income from funding (on effective position)
        daily_income_pct = hourly_rate * 24 * capital_efficiency
        
        # Break-even days = total fees / daily income
        if daily_income_pct <= 0:
            return {"viable": False, "days_to_breakeven": 999, "net_apy": 0, "reason": "Zero income"}
        
        days_to_breakeven = self.roundtrip_cost / daily_income_pct
        
        # Net APY after accounting for fees
        annual_income = daily_income_pct * 365
        net_apy = (annual_income - self.roundtrip_cost) * 100  # As percentage
        
        # Viability check
        viable = (days_to_breakeven < self.max_breakeven_days) and (net_apy > 15.0)
        
        reason = ""
        if not viable:
            if days_to_breakeven >= self.max_breakeven_days:
                reason = f"Break-even too slow: {days_to_breakeven:.1f} days"
            else:
                reason = f"Net APY too low: {net_apy:.1f}%"
        
        return {
            "viable": viable,
            "days_to_breakeven": round(days_to_breakeven, 1),
            "net_apy": round(net_apy, 1),
            "reason": reason
        }
    
    async def _get_all_funding_rates(self) -> Dict[str, float]:
        """Get funding rates for all coins."""
        try:
            # For MVP, just check HYPE
            rate = await self.client.get_funding_rate("HYPE")
            return {"HYPE": rate}
        except Exception as e:
            logger.error(f"Funding rate fetch error: {e}")
            return {}
    
    async def _get_liquidity(self, coin: str) -> float:
        """Get 24h volume for a coin."""
        # Simplified - in production would fetch actual volume
        # For HYPE on Hyperliquid, assume adequate liquidity
        return 10_000_000  # $10M placeholder
    
    def get_scan_summary(self) -> Dict:
        """Get summary of last scan for dashboard."""
        viable = [o for o in self._scan_cache if o.viable]
        return {
            "last_scan": self._last_scan,
            "total_scanned": len(self._scan_cache),
            "viable_count": len(viable),
            "best_opportunity": viable[0].coin if viable else None,
            "best_apr": viable[0].funding_apr if viable else 0,
            "best_net_apy": viable[0].net_apy if viable else 0
        }
