#!/bin/bash
# Start Funding Bot Dashboard on port 8003
# Consistent with other bots: 8000, 8001, 8003
# Usage: ./run_dashboard.sh

cd "$(dirname "$0")"

python3 -m streamlit run dashboard/app.py \
    --server.port 8003 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false

