"""
Backtester for Market Maker Strategy

Uses historical spread data to simulate market making performance.
"""

import json
import logging
import argparse
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
import statistics

logger = logging.getLogger(__name__)


@dataclass
class SimulatedFill:
    """A simulated trade fill."""
    timestamp: str
    side: str  # 'buy' or 'sell'
    price: float
    size: float
    pnl: float = 0.0


@dataclass 
class BacktestResult:
    """Results from a backtest run."""
    start_time: str
    end_time: str
    num_observations: int
    
    # Trading metrics
    num_fills: int
    volume_usd: float
    gross_pnl: float
    fees: float
    net_pnl: float
    
    # Risk metrics
    max_inventory: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    
    # Per-day metrics
    avg_daily_pnl: float
    avg_daily_volume: float


class Backtester:
    """
    Backtests market making strategy on historical spread data.
    
    Simulation logic:
    1. Load spread data from spread_log.json
    2. For each observation, check if our bid/ask would be hit
    3. Track fills, PnL, and inventory
    4. Calculate performance metrics
    """
    
    def __init__(
        self,
        spread_bps: int = 8,
        quote_size_usd: float = 50,
        max_inventory_usd: float = 500,
        maker_rebate_bps: float = 1.0,  # 0.01% = 1 bps rebate
    ):
        self.spread_bps = spread_bps
        self.quote_size_usd = quote_size_usd
        self.max_inventory_usd = max_inventory_usd
        self.maker_rebate_bps = maker_rebate_bps
        
        # State
        self.inventory = 0.0  # In HYPE
        self.inventory_value = 0.0  # In USD
        self.pnl = 0.0
        self.fills: List[SimulatedFill] = []
        
        # Tracking
        self.pnl_history: List[float] = []
        self.inventory_history: List[float] = []
    
    def load_data(self, filepath: str) -> List[Dict]:
        """Load spread data from JSON file."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        
        observations = data.get('data', [])
        logger.info(f"Loaded {len(observations)} observations from {filepath}")
        return observations
    
    def simulate(self, observations: List[Dict]) -> BacktestResult:
        """
        Run simulation on historical data.
        
        Args:
            observations: List of spread observations
            
        Returns:
            BacktestResult with performance metrics
        """
        if not observations:
            raise ValueError("No observations to simulate")
        
        # Reset state
        self.inventory = 0.0
        self.pnl = 0.0
        self.fills = []
        self.pnl_history = []
        self.inventory_history = []
        
        total_volume = 0.0
        total_fees = 0.0
        max_inventory = 0.0
        
        for obs in observations:
            spot_bid = obs.get('spot_bid', 0)
            spot_ask = obs.get('spot_ask', 0)
            
            if spot_bid <= 0 or spot_ask <= 0:
                continue
            
            # Calculate our quotes
            mid_price = (spot_bid + spot_ask) / 2
            our_bid = mid_price * (1 - self.spread_bps / 10000)
            our_ask = mid_price * (1 + self.spread_bps / 10000)
            
            # Calculate quote size based on current price
            quote_size = self.quote_size_usd / mid_price
            
            # Check inventory limits
            can_buy = abs(self.inventory * mid_price) < self.max_inventory_usd
            can_sell = self.inventory > 0 or abs(self.inventory * mid_price) < self.max_inventory_usd
            
            # Check if our bid would be hit
            # Our bid is hit if spot_ask <= our_bid (someone sells to us)
            if spot_ask <= our_bid and can_buy:
                fill_price = our_bid
                fill_size = min(quote_size, (self.max_inventory_usd - self.inventory * mid_price) / mid_price)
                
                if fill_size > 0.1:
                    self.inventory += fill_size
                    cost = fill_size * fill_price
                    rebate = cost * self.maker_rebate_bps / 10000
                    
                    self.fills.append(SimulatedFill(
                        timestamp=obs.get('timestamp', ''),
                        side='buy',
                        price=fill_price,
                        size=fill_size
                    ))
                    
                    total_volume += cost
                    total_fees -= rebate  # Negative = we earn
            
            # Check if our ask would be hit
            # Our ask is hit if spot_bid >= our_ask (someone buys from us)
            if spot_bid >= our_ask and can_sell:
                fill_price = our_ask
                fill_size = min(quote_size, self.inventory + self.max_inventory_usd / mid_price)
                
                if fill_size > 0.1:
                    self.inventory -= fill_size
                    revenue = fill_size * fill_price
                    rebate = revenue * self.maker_rebate_bps / 10000
                    
                    self.fills.append(SimulatedFill(
                        timestamp=obs.get('timestamp', ''),
                        side='sell',
                        price=fill_price,
                        size=fill_size
                    ))
                    
                    total_volume += revenue
                    total_fees -= rebate  # Negative = we earn
            
            # Track inventory
            self.inventory_value = self.inventory * mid_price
            self.inventory_history.append(self.inventory_value)
            max_inventory = max(max_inventory, abs(self.inventory_value))
            
            # Calculate mark-to-market PnL
            # Total value = cash proceeds from sells - cash spent on buys + inventory value
            # Simplified: PnL = spread earned on roundtrips + inventory MTM
            # For now, just track realized PnL from closed positions
        
        # Calculate realized PnL from fills
        realized_pnl = self._calculate_realized_pnl()
        
        # Calculate metrics
        net_pnl = realized_pnl - total_fees  # fees is negative (rebates), so this adds
        
        # Win rate
        winning_fills = sum(1 for f in self.fills if f.pnl > 0)
        win_rate = winning_fills / len(self.fills) if self.fills else 0
        
        # Max drawdown
        max_drawdown = self._calculate_max_drawdown()
        
        # Sharpe ratio (annualized)
        sharpe = self._calculate_sharpe(net_pnl, len(observations))
        
        # Time range
        start_time = observations[0].get('timestamp', '') if observations else ''
        end_time = observations[-1].get('timestamp', '') if observations else ''
        
        # Per-day estimates (assuming ~30 observations per hour, 24 hours)
        est_hours = len(observations) / 30 if len(observations) > 30 else 1
        est_days = est_hours / 24 if est_hours > 24 else 1
        
        return BacktestResult(
            start_time=start_time,
            end_time=end_time,
            num_observations=len(observations),
            num_fills=len(self.fills),
            volume_usd=total_volume,
            gross_pnl=realized_pnl,
            fees=total_fees,
            net_pnl=net_pnl,
            max_inventory=max_inventory,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe,
            win_rate=win_rate,
            avg_daily_pnl=net_pnl / est_days,
            avg_daily_volume=total_volume / est_days
        )
    
    def _calculate_realized_pnl(self) -> float:
        """
        Calculate realized PnL from closed positions.
        Uses FIFO matching.
        """
        buys = []
        pnl = 0.0
        
        for fill in self.fills:
            if fill.side == 'buy':
                buys.append((fill.price, fill.size))
            else:  # sell
                remaining = fill.size
                while remaining > 0 and buys:
                    buy_price, buy_size = buys[0]
                    
                    match_size = min(remaining, buy_size)
                    trade_pnl = match_size * (fill.price - buy_price)
                    pnl += trade_pnl
                    fill.pnl = trade_pnl
                    
                    remaining -= match_size
                    if match_size >= buy_size:
                        buys.pop(0)
                    else:
                        buys[0] = (buy_price, buy_size - match_size)
        
        return pnl
    
    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from inventory history."""
        if not self.inventory_history:
            return 0.0
        
        peak = self.inventory_history[0]
        max_dd = 0.0
        
        for value in self.inventory_history:
            if value > peak:
                peak = value
            dd = peak - value
            max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _calculate_sharpe(self, total_pnl: float, num_obs: int) -> float:
        """Calculate Sharpe ratio (simplified)."""
        if num_obs < 2:
            return 0.0
        
        # Estimate daily returns
        # Assume each observation is ~3 seconds apart
        obs_per_day = 24 * 60 * 60 / 3
        num_days = num_obs / obs_per_day if obs_per_day > 0 else 1
        
        daily_return = total_pnl / num_days if num_days > 0 else 0
        
        # Assume some volatility based on inventory swings
        if self.inventory_history:
            volatility = statistics.stdev(self.inventory_history) if len(self.inventory_history) > 1 else 1
        else:
            volatility = 1
        
        # Annualized Sharpe
        if volatility > 0:
            sharpe = (daily_return * 365) / (volatility * (365 ** 0.5))
        else:
            sharpe = 0
        
        return sharpe


def print_results(result: BacktestResult):
    """Print backtest results in a nice format."""
    print("\n" + "=" * 60)
    print("📊 BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period: {result.start_time[:10]} to {result.end_time[:10]}")
    print(f"Observations: {result.num_observations:,}")
    print()
    print("💰 Performance:")
    print(f"  Total Fills: {result.num_fills}")
    print(f"  Volume: ${result.volume_usd:,.2f}")
    print(f"  Gross PnL: ${result.gross_pnl:,.4f}")
    print(f"  Maker Rebates: ${-result.fees:,.4f}")
    print(f"  Net PnL: ${result.net_pnl:,.4f}")
    print()
    print("📈 Daily Estimates:")
    print(f"  Avg Daily PnL: ${result.avg_daily_pnl:,.2f}")
    print(f"  Avg Daily Volume: ${result.avg_daily_volume:,.2f}")
    print()
    print("⚠️ Risk Metrics:")
    print(f"  Max Inventory: ${result.max_inventory:,.2f}")
    print(f"  Max Drawdown: ${result.max_drawdown:,.2f}")
    print(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"  Win Rate: {result.win_rate*100:.1f}%")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='Backtest Market Making Strategy')
    parser.add_argument('--data', default='spread_log.json', help='Path to spread data file')
    parser.add_argument('--spread', type=int, default=8, help='Quote spread in basis points')
    parser.add_argument('--size', type=float, default=50, help='Quote size in USD')
    parser.add_argument('--max-inv', type=float, default=500, help='Max inventory in USD')
    parser.add_argument('--days', type=int, default=None, help='Limit to last N days of data')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO)
    
    print(f"\n🔬 Running backtest with:")
    print(f"   Spread: {args.spread} bps")
    print(f"   Quote Size: ${args.size}")
    print(f"   Max Inventory: ${args.max_inv}")
    
    bt = Backtester(
        spread_bps=args.spread,
        quote_size_usd=args.size,
        max_inventory_usd=args.max_inv
    )
    
    observations = bt.load_data(args.data)
    
    if args.days and len(observations) > 0:
        # Estimate observations per day (assuming ~3s per observation, 10 saved = 30s)
        obs_per_day = 24 * 60 * 2  # ~2880 per day
        limit = args.days * obs_per_day
        observations = observations[-limit:]
        print(f"   Using last {len(observations)} observations (~{args.days} days)")
    
    result = bt.simulate(observations)
    print_results(result)


if __name__ == "__main__":
    main()
