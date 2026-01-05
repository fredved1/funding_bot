"""
Configuration for Delta Neutral Arbitrage Bot
OPTIMIZED FOR $1000 ACCOUNT - Testing Mode

Analysis results:
- Avg spread: 0.013%, Max spread: 0.08%
- Roundtrip fees: ~0.125%
- Current strategy may not be profitable, but testing to collect data
"""

# ======== API CREDENTIALS ========
# Import from environment or local .env file
import os
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")

# ======== TRADING PAIRS ========
SPOT_SYMBOL = "@107"  # HYPE spot
PERP_SYMBOL = "HYPE"  # HYPE perpetual

# ======== STRATEGY THRESHOLDS (OPTIMIZED) ========
# Lower threshold to catch more opportunities for data collection
# Note: With 0.125% roundtrip fees, need spread > 0.125% to profit
MIN_SPREAD_THRESHOLD = 0.0008  # 0.08% - catches top ~1% of spreads

# Exit when spread collapses (aim to capture ~0.05% of the spread)
EXIT_THRESHOLD = 0.0003  # 0.03%

# Check funding rate before entry (skip if negative)
CHECK_FUNDING_RATE = True

# ======== RISK MANAGEMENT (OPTIMIZED FOR $1000) ========
# Position sizing: 5-10% of capital per trade
# With $1000 account, $50-100 per position is reasonable
MAX_POSITION_USD = 75.0  # $75 per trade (7.5% of $1000)

# Enable dry-run mode for testing first
DRY_RUN = False  # Set to True to test without real trades

# ======== BOT SETTINGS ========
LOG_LEVEL = "INFO"

# Data collection - KEEP ENABLED for analysis
SAVE_SPREAD_LOG = True
SAVE_TRADE_LOG = True
SPREAD_LOG_FILE = "spread_log.json"
TRADE_LOG_FILE = "trade_log.json"

# ======== NETWORK ========
WS_URL = "wss://api.hyperliquid.xyz/ws"
API_URL = "https://api.hyperliquid.xyz"

# WebSocket settings
WS_PING_INTERVAL = 20
WS_RECONNECT_DELAY = 1
WS_RECONNECT_MAX_DELAY = 60

# Reconnection settings
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY_SECONDS = 5
