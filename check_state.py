import requests
import json
import config

def get_state():
    headers = {'Content-Type': 'application/json'}
    
    print("Fetching state...")
    try:
        # Perp state
        r = requests.post('https://api.hyperliquid.xyz/info', 
                         json={'type': 'clearinghouseState', 'user': config.ACCOUNT_ADDRESS}, 
                         headers=headers, timeout=10)
        perp = r.json()
        
        # Spot state
        r = requests.post('https://api.hyperliquid.xyz/info', 
                         json={'type': 'spotClearinghouseState', 'user': config.ACCOUNT_ADDRESS}, 
                         headers=headers, timeout=10)
        spot = r.json()
        
        print('\n=== PERP POSITION ===')
        has_pos = False
        for p in perp.get('assetPositions', []):
            pos = p.get('position', {})
            szi = float(pos.get('szi', 0))
            if szi != 0:
                has_pos = True
                print(f"Size: {szi} {pos.get('coin')}")
                print(f"Entry: ${pos.get('entryPx')}")
                print(f"Unrealized PnL: ${pos.get('unrealizedPnl')}")
                print(f"Liquidation: ${pos.get('liquidationPx')}")
        
        if not has_pos:
            print("No open perp positions.")
                
        print('\n=== SPOT BALANCES ===')
        for b in spot.get('balances', []):
            if float(b.get('total', 0)) > 0:
                print(f"{b.get('coin')}: {b.get('total')}")

        print('\n=== TRADE EVENTS (Last 5) ===')
        try:
            with open('trade_events.json') as f:
                data = json.load(f)
                for e in data.get('events', [])[-5:]:
                    print(f"{e.get('timestamp')} - {e.get('message')}")
        except FileNotFoundError:
            print('No trade_events.json found')
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    get_state()
