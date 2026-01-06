"""
Configuration for Delta Neutral Arbitrage Bot - Hyperliquid
LIVE TRADING CONFIG - Data Collection Phase
"""

# =============================================================================
# WALLET CREDENTIALS
# =============================================================================
# Main wallet (email-linked custodial wallet with funds)
ACCOUNT_ADDRESS = "0x668D96ce5a56BD90B77130D2B50A653686838844"

# API Wallet "perfectwhale" (authorized to trade on behalf of main wallet until 7-6-2026)
API_WALLET_ADDRESS = "0x58476ffce97e43bc13f8802a35aae08760eb879f"
PRIVATE_KEY = "0xffa9fcf66fa61b77c6eb6d61c61322494135ce90fbe6716eecbd80205dac9944"

# =============================================================================
# TRADING PAIRS
# =============================================================================
SPOT_SYMBOL = "@107"      # HYPE Spot market (token index 107)
PERP_SYMBOL = "HYPE"      # HYPE Perpetual market

# =============================================================================
# STRATEGY THRESHOLDS - OPTIMIZED FOR DATA COLLECTION
# =============================================================================
# Entry: Open arb when (Perp_Bid - Spot_Ask) / Spot_Ask > MIN_SPREAD_THRESHOLD
MIN_SPREAD_THRESHOLD = 0.0012   # 0.15% - lower to catch more trades for data

# Exit: Close arb when spread converges below this threshold
EXIT_THRESHOLD = 0.0003         # 0.03% - quick exit for faster cycles

# =============================================================================
# RISK MANAGEMENT - CONSERVATIVE FOR $15/$15 BALANCE
# =============================================================================
MAX_POSITION_USD = 14.0        # $10 per trade (safe for $15 balance)
MAX_TOTAL_EXPOSURE_USD = 15.0  # Maximum total exposure

# Funding rate check: Skip opening if funding is negative (shorts pay longs)
CHECK_FUNDING_RATE = True

# =============================================================================
# BOT SETTINGS - LIVE MODE
# =============================================================================
# LIVE TRADING ENABLED
DRY_RUN = False

# Logging level - DEBUG for maximum data collection
LOG_LEVEL = "INFO"

# Order timeout in seconds (for IOC orders)
ORDER_TIMEOUT = 5.0

# =============================================================================
# DATA COLLECTION SETTINGS
# =============================================================================
# Save all trade data to JSON file for analysis
SAVE_TRADE_LOG = True
TRADE_LOG_FILE = "trade_log.json"

# Save spread data for analysis
SAVE_SPREAD_LOG = True
SPREAD_LOG_FILE = "spread_log.json"

# =============================================================================
# NETWORK SETTINGS
# =============================================================================
WS_URL = "wss://api.hyperliquid.xyz/ws"
API_URL = "https://api.hyperliquid.xyz"

# =============================================================================
# RECONNECTION SETTINGS
# =============================================================================
WS_RECONNECT_DELAY = 1.0
WS_RECONNECT_MAX_DELAY = 30.0
WS_PING_INTERVAL = 30.0
