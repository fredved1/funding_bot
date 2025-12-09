import config
import requests
import time
from hyperliquid.exchange import Exchange
from eth_account import Account
from hyperliquid.utils import constants

def run():
    print("Cancelling all open orders...")
    
    account = Account.from_key(config.PRIVATE_KEY)
    address = config.ACCOUNT_ADDRESS
    exchange = Exchange(account, constants.MAINNET_API_URL, account_address=address)
    
    # Fetch open orders via API
    headers = {'Content-Type': 'application/json'}
    r = requests.post('https://api.hyperliquid.xyz/info', 
                     json={'type': 'openOrders', 'user': address}, 
                     headers=headers)
    orders = r.json()
    
    if not orders:
        print("No open orders found.")
        return

    for o in orders:
        print(f"Cancelling {o['coin']} {o['side']} {o['sz']} (ID: {o['oid']})")
        res = exchange.cancel(o['coin'], o['oid'])
        print(f"Result: {res}")
        time.sleep(0.5)

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"Error: {e}")
