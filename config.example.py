"""
Configuration for Delta Neutral Funding Bot
Copy this file to config.py and set your credentials via environment variables.

SECURITY: Never hardcode private keys! Use .env file or environment variables.
"""

import os

# Try to load .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ======== API CREDENTIALS (from environment) ========
# Load from environment - NEVER hardcode these!
PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "")
ACCOUNT_ADDRESS = os.getenv("HL_ACCOUNT_ADDRESS", "")
API_WALLET = os.getenv("HL_API_WALLET", "")  # Optional: separate API wallet

# Validate on import
if not PRIVATE_KEY or PRIVATE_KEY == "":
    import warnings
    warnings.warn("⚠️ HL_PRIVATE_KEY not set! Bot will fail to trade.")

if not ACCOUNT_ADDRESS or ACCOUNT_ADDRESS == "":
    import warnings
    warnings.warn("⚠️ HL_ACCOUNT_ADDRESS not set! Bot will fail to connect.")

# ======== TRADING PAIR (Dynamic Resolution) ========
# We use COIN_NAME and resolve the spot asset ID at runtime
COIN_NAME = "HYPE"  # The coin to trade
PERP_SYMBOL = "HYPE"  # Perp always uses coin name directly

# SPOT_SYMBOL will be resolved dynamically at runtime via info.meta()
# Do NOT hardcode "@107" - asset IDs can change!
SPOT_SYMBOL = None  # Will be set by main.py on startup

# ======== STRATEGY TYPE ========
IS_FUNDING_STRATEGY = True  # True = Funding harvester, False = Spread arbitrage

# ======== FUNDING BOT SETTINGS ========
# Minimum APR to enter position (20% = 0.20)
MIN_FUNDING_APR = 0.20

# Maximum position per coin (USD)
MAX_POSITION_PER_COIN_USD = 500.0

# Total exposure limit across all positions
MAX_TOTAL_EXPOSURE_USD = 2000.0

# Buffer to keep on perps side (% of position value)
MARGIN_BUFFER_PERCENT = 0.20

# Margin ratio below which to add collateral
DANGER_MARGIN_RATIO = 0.15

# Close position if funding goes negative for X hours
NEGATIVE_FUNDING_TOLERANCE_HOURS = 2

# ======== EXIT CONDITIONS (Strategy-Dependent) ========
if IS_FUNDING_STRATEGY:
    # FUNDING STRATEGY: Exit based on funding rate, NOT spread
    # Exit if funding rate goes negative (shorts start paying)
    EXIT_ON_NEGATIVE_FUNDING = True
    
    # Emergency exit if spread deviates too much (spot-perp gap widens dangerously)
    EMERGENCY_SPREAD_THRESHOLD = -0.005  # -0.5% = Something is wrong
    
    # Legacy spread exit is DISABLED for funding strategy
    EXIT_THRESHOLD = None
else:
    # SPREAD ARBITRAGE: Exit based on spread convergence
    EXIT_THRESHOLD = 0.0003  # Exit when spread falls to 0.03%
    EXIT_ON_NEGATIVE_FUNDING = False
    EMERGENCY_SPREAD_THRESHOLD = None

# ======== PROFITABILITY THRESHOLDS ========
# Roundtrip fees: ~0.125% (0.04% spot + 0.03% perp + slippage) x2
# Minimum spread must be > fees to profit!
MIN_SPREAD_THRESHOLD = 0.0025  # 0.25% minimum (covers fees + profit margin)

# ======== RISK MANAGEMENT ========
# Maximum position size in USD (per trade)
MAX_POSITION_USD = 500.0

# Enable dry-run mode (no real trades) - DEFAULT TO SAFE!
DRY_RUN = True

# ======== BOT SETTINGS ========
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Database file
DATABASE_FILE = "funding_bot.db"

# ======== NETWORK ========
WS_URL = "wss://api.hyperliquid.xyz/ws"
API_URL = "https://api.hyperliquid.xyz"

# WebSocket settings
WS_RECONNECT_DELAY = 5
WS_RECONNECT_MAX_DELAY = 60
WS_PING_INTERVAL = 30

# ======== LEGACY SETTINGS (backward compatibility) ========
CHECK_FUNDING_RATE = True
SAVE_SPREAD_LOG = True
SAVE_TRADE_LOG = True
SPREAD_LOG_FILE = "spread_log.json"
TRADE_LOG_FILE = "trade_log.json"
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY_SECONDS = 5
