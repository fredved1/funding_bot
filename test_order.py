#!/usr/bin/env python3
"""
Quick test script to debug order placement.
Run on VPS: python test_order.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

# Get credentials
pk = os.getenv('HL_PRIVATE_KEY')
addr = os.getenv('HL_ACCOUNT_ADDRESS')

if not pk or not addr:
    print("❌ Missing credentials in .env")
    sys.exit(1)

print(f"✅ Using address: {addr[:10]}...")

# Initialize
account = Account.from_key(pk)
info = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(account, constants.MAINNET_API_URL, account_address=addr)

# Get current prices
mids = info.all_mids()
hype_perp_price = float(mids.get('HYPE', 0))
print(f"📊 HYPE perp price: ${hype_perp_price:.4f}")

# Get spot book
spot_symbol = '@150'
spot_book = info.l2_snapshot(spot_symbol)
spot_ask = float(spot_book['levels'][1][0]['px']) if spot_book['levels'][1] else 1.0
print(f"📊 HYPE spot ask: ${spot_ask:.5f}")

# Calculate small test size
test_size = 0.1  # Very small: 0.1 HYPE = ~$2.70
print(f"\n🧪 Testing with {test_size} HYPE (≈${test_size * hype_perp_price:.2f})")

# Test PERP order first (simpler)
print("\n--- PERP ORDER TEST ---")
try:
    result = exchange.order(
        name='HYPE',
        is_buy=False,  # Sell/Short
        sz=test_size,
        limit_px=round(hype_perp_price * 0.99, 2),  # 1% below market
        order_type={'limit': {'tif': 'Ioc'}},
        reduce_only=False
    )
    print(f"📥 Perp result: {result}")
except Exception as e:
    print(f"❌ Perp error: {e}")

# Test SPOT order
print("\n--- SPOT ORDER TEST ---")
try:
    result = exchange.order(
        name=spot_symbol,  # @150
        is_buy=True,
        sz=test_size,
        limit_px=round(spot_ask * 1.01, 5),  # 1% above market
        order_type={'limit': {'tif': 'Ioc'}},
        reduce_only=False
    )
    print(f"📥 Spot result: {result}")
except Exception as e:
    print(f"❌ Spot error: {e}")

print("\n✅ Test complete!")
