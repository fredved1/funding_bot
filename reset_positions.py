import config
import time
import json
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account
from trade_events import trade_events
import traceback

def run():
    print("Resetting positions (V3)...")
    
    # 1. Setup
    account = Account.from_key(config.PRIVATE_KEY)
    address = config.ACCOUNT_ADDRESS
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=address)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    mids = info.all_mids()
    
    # Need generic decimals helper
    meta = info.meta()
    universe = meta['universe']
    def get_sz_decimals(coin):
        for u in universe:
            if u['name'] == coin:
                return u['szDecimals']
        return 2

    headers = {'Content-Type': 'application/json'}

    # 2. Close Perps
    print("Checking perps...")
    # Use direct API for state to be safe
    r = requests.post('https://api.hyperliquid.xyz/info', 
                     json={'type': 'clearinghouseState', 'user': address}, 
                     headers=headers)
    state = r.json()
    
    for p in state.get('assetPositions', []):
        pos = p['position']
        coin = pos['coin']
        sz = float(pos['szi'])
        if sz != 0:
            print(f"Closing Perp {coin} {sz}...")
            price = float(mids.get(coin, 0))
            if price == 0: continue
            
            is_buy = sz < 0
            # Tighter slippage (2%)
            limit_px = price * (1.02 if is_buy else 0.98)
            limit_px = round(limit_px, 5) # Price precision
            
            decimals = get_sz_decimals(coin)
            abs_sz = round(abs(sz), decimals)
            
            print(f"Order: {coin} {'Buy' if is_buy else 'Sell'} {abs_sz} @ {limit_px}")
            
            res = exchange.order(coin, is_buy, abs_sz, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=True)
            print(f"Result: {res}")
            trade_events.add_event("exit", f"⚠️ RESET V3: Closed {sz} {coin} Perp", {"response": str(res)})
            time.sleep(1)

    # 3. Sell Spot
    print("Checking spot...")
    r = requests.post('https://api.hyperliquid.xyz/info', 
                     json={'type': 'spotClearinghouseState', 'user': address}, 
                     headers=headers)
    spot_state = r.json()
    
    ref_price = float(mids.get('HYPE', 0))
    if ref_price == 0: ref_price = 28.0
    
    for b in spot_state.get('balances', []):
        coin = b['coin']
        total = float(b['total'])
        if coin == 'HYPE' and total > 0.05:
             symbol = config.SPOT_SYMBOL
             limit_px = round(ref_price * 0.95, 4) # Sell cheap (5% slip)
             
             sz_round = float(int(total * 1000) / 1000)
             
             print(f"Selling Spot {coin} {sz_round} @ {limit_px}...")
             res = exchange.order(symbol, False, sz_round, limit_px, {"limit": {"tif": "Ioc"}})
             print(f"Result: {res}")
             trade_events.add_event("exit", f"⚠️ RESET V3: Sold {sz_round} {coin} Spot", {"response": str(res)})
             time.sleep(1)

    print("Reset V3 complete.")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        traceback.print_exc()
