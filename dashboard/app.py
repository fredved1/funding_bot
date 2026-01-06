"""
Funding Bot Dashboard - Streamlit Monitoring App

Read-only visualization of bot state from SQLite database.
Run separately from bot: streamlit run dashboard/app.py

IMPORTANT: Opens DB in read-only mode to prevent locking.
"""

import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta
import os
import sys
import time

# Add parent directory to path for config import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Page config
st.set_page_config(
    page_title="🌾 Funding Bot Dashboard",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Database path
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "funding_bot.db")

# Config defaults (in case config.py not available)
MIN_FUNDING_APR = 0.20
COIN_NAME = "HYPE"

try:
    import config
    MIN_FUNDING_APR = getattr(config, 'MIN_FUNDING_APR', 0.20)
    COIN_NAME = getattr(config, 'COIN_NAME', 'HYPE')
except:
    pass


# ========== MARKET DATA FETCHING ==========

@st.cache_data(ttl=30)  # Cache for 30 seconds
def fetch_market_data():
    """Fetch live market data from Hyperliquid API."""
    try:
        # Get all mids for price
        mids_response = requests.post(
            'https://api.hyperliquid.xyz/info',
            json={'type': 'allMids'},
            timeout=5
        )
        mids = mids_response.json()
        price = float(mids.get(COIN_NAME, 0))
        
        # Get meta for funding rate - use metaAndAssetCtxs which has actual funding data
        meta_response = requests.post(
            'https://api.hyperliquid.xyz/info',
            json={'type': 'metaAndAssetCtxs'},
            timeout=5
        )
        data = meta_response.json()
        meta, asset_ctxs = data[0], data[1]
        
        funding_rate = 0.0
        for i, asset in enumerate(meta.get('universe', [])):
            if asset.get('name') == COIN_NAME:
                # Funding rate is in the corresponding asset context
                ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                funding_rate = float(ctx.get('funding', 0))
                break
        
        # Get L2 book for spread
        l2_response = requests.post(
            'https://api.hyperliquid.xyz/info',
            json={'type': 'l2Book', 'coin': COIN_NAME},
            timeout=5
        )
        l2 = l2_response.json()
        
        best_bid = 0.0
        best_ask = 0.0
        if l2.get('levels'):
            bids = l2['levels'][0]
            asks = l2['levels'][1]
            if bids:
                best_bid = float(bids[0]['px'])
            if asks:
                best_ask = float(asks[0]['px'])
        
        spread = ((best_ask - best_bid) / best_bid * 100) if best_bid > 0 else 0
        
        return {
            'price': price,
            'funding_rate': funding_rate,
            'funding_apr': funding_rate * 24 * 365 * 100,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'spread_pct': spread,
            'timestamp': datetime.now(),
            'success': True
        }
    except Exception as e:
        return {
            'price': 0,
            'funding_rate': 0,
            'funding_apr': 0,
            'best_bid': 0,
            'best_ask': 0,
            'spread_pct': 0,
            'timestamp': datetime.now(),
            'success': False,
            'error': str(e)
        }


@st.cache_data(ttl=300)  # Cache for 5 minutes
def fetch_funding_history(hours: int = 48):
    """Fetch historical funding rates from Hyperliquid API."""
    try:
        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
        
        response = requests.post(
            'https://api.hyperliquid.xyz/info',
            json={
                'type': 'fundingHistory',
                'coin': COIN_NAME,
                'startTime': start_time,
                'endTime': end_time
            },
            timeout=10
        )
        data = response.json()
        
        if not data:
            return pd.DataFrame()
        
        df = pd.DataFrame(data)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df['fundingRate'] = df['fundingRate'].astype(float)
        df['apr'] = df['fundingRate'] * 24 * 365 * 100  # Annual percentage rate
        df['hourly_pct'] = df['fundingRate'] * 100  # Hourly percentage
        return df.sort_values('time')
    except Exception as e:
        st.error(f"Failed to fetch funding history: {e}")
        return pd.DataFrame()


# ========== DATABASE HELPERS ==========

def get_db_connection():
    """Get read-only database connection to prevent locking."""
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def query_df(sql: str) -> pd.DataFrame:
    """Execute query and return DataFrame."""
    try:
        with get_db_connection() as conn:
            return pd.read_sql_query(sql, conn)
    except Exception as e:
        return pd.DataFrame()


def check_db_exists() -> bool:
    """Check if database file exists."""
    return os.path.exists(DB_PATH)


# ========== SIDEBAR ==========

def render_sidebar():
    """Render sidebar with bot status and balances."""
    st.sidebar.title("🌾 Funding Bot")
    
    # Market data in sidebar
    market = fetch_market_data()
    if market['success']:
        st.sidebar.success("🟢 Market Connected")
        st.sidebar.metric(f"{COIN_NAME} Price", f"${market['price']:.4f}")
    else:
        st.sidebar.error("🔴 Market Disconnected")
    
    st.sidebar.markdown("---")
    
    # Check database
    if not check_db_exists():
        st.sidebar.warning("⚠️ Database not found")
        st.sidebar.info("Start the bot first to create the database.")
    else:
        st.sidebar.success("✅ Database connected")
    
        # Funding stats from DB
        st.sidebar.subheader("💰 Earnings")
        
        funding_df = query_df("SELECT SUM(amount_usdc) as total FROM funding_log")
        if not funding_df.empty:
            total_funding = funding_df.iloc[0]['total'] or 0
            st.sidebar.metric("Total Funded", f"${total_funding:.4f}")
        
        # Position count
        pos_df = query_df("SELECT COUNT(*) as count FROM positions WHERE status = 'OPEN'")
        if not pos_df.empty:
            st.sidebar.metric("Open Positions", pos_df.iloc[0]['count'])
        
        # Trade count
        trade_df = query_df("SELECT COUNT(*) as count FROM trades")
        if not trade_df.empty:
            st.sidebar.metric("Total Trades", trade_df.iloc[0]['count'])
    
    # Refresh info
    st.sidebar.markdown("---")
    st.sidebar.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")
    st.sidebar.caption("Auto-refresh: 30s")
    
    return True


# ========== LIVE MARKET MONITOR ==========

def render_market_monitor():
    """Render live market monitor at top of page."""
    st.subheader("📡 Live Market Monitor")
    
    market = fetch_market_data()
    
    if not market['success']:
        st.error(f"❌ Failed to fetch market data: {market.get('error', 'Unknown')}")
        return
    
    # 3-column metrics
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric(
            label="Market Status",
            value="🟢 Scanning",
            delta="Live"
        )
    
    with col2:
        apr = market['funding_apr']
        target_apr = MIN_FUNDING_APR * 100
        hourly_rate = market['funding_rate']
        
        # Color based on value: Green (good), Orange (low), Red (zero/negative)
        if apr >= target_apr:
            # GREEN: Above target
            st.metric(
                label="Funding APR",
                value=f"{apr:.2f}%",
                delta=f"✅ Above {target_apr:.0f}% target"
            )
        elif apr > 0:
            # ORANGE: Positive but below target
            st.metric(
                label="Funding APR",
                value=f"{apr:.2f}%",
                delta=f"⚠️ Below {target_apr:.0f}% target",
                delta_color="off"
            )
        else:
            # RED: Zero or negative
            st.metric(
                label="Funding APR",
                value=f"{apr:.2f}%",
                delta="🔴 No funding / Negative",
                delta_color="inverse"
            )
        
        # Debug info (smaller text below metric)
        st.caption(f"Raw hourly: {hourly_rate:.6f} ({hourly_rate*100:.4f}%)")
    
    with col3:
        st.metric(
            label="Spread",
            value=f"{market['spread_pct']:.4f}%",
            delta=f"${market['best_ask'] - market['best_bid']:.4f}"
        )
    
    # Price info row
    col4, col5, col6 = st.columns(3)
    
    with col4:
        st.metric(f"{COIN_NAME} Price", f"${market['price']:.4f}")
    
    with col5:
        st.metric("Best Bid", f"${market['best_bid']:.4f}")
    
    with col6:
        st.metric("Best Ask", f"${market['best_ask']:.4f}")
    
    # "Why am I waiting?" explanation
    st.markdown("---")
    
    pos_count = 0
    if check_db_exists():
        pos_df = query_df("SELECT COUNT(*) as count FROM positions WHERE status = 'OPEN'")
        if not pos_df.empty:
            pos_count = pos_df.iloc[0]['count']
    
    if pos_count == 0:
        apr = market['funding_apr']
        target = MIN_FUNDING_APR * 100
        
        if apr < target:
            st.info(f"""
            🎯 **Waiting for opportunity...**
            
            The bot is targeting funding rates above **{target:.0f}% APR**.
            
            Current market: **{apr:.2f}% APR** (too low)
            
            When funding rate rises above threshold, the bot will automatically open a delta-neutral position.
            """)
        else:
            st.success(f"""
            🚀 **Opportunity detected!**
            
            Funding rate is **{apr:.2f}% APR** - above the {target:.0f}% target.
            
            The bot should be entering a position shortly...
            """)
    else:
        st.success(f"✅ **{pos_count} active position(s)** - Bot is harvesting funding.")


# ========== TAB 1: LIVE STATUS ==========

def render_live_status():
    """Render live status tab."""
    
    # Live market monitor at top
    render_market_monitor()
    
    # Funding Rate History Chart
    st.markdown("---")
    st.header("📈 Funding Rate History (48h)")
    
    funding_df = fetch_funding_history(48)
    if not funding_df.empty:
        # Create the chart
        fig = go.Figure()
        
        # Add APR line
        fig.add_trace(go.Scatter(
            x=funding_df['time'],
            y=funding_df['apr'],
            mode='lines+markers',
            name='Funding APR %',
            line=dict(color='#00d4aa', width=2),
            marker=dict(size=4),
            hovertemplate='%{x|%H:%M %d/%m}<br>APR: %{y:.2f}%<extra></extra>'
        ))
        
        # Add target line
        target_apr = MIN_FUNDING_APR * 100
        fig.add_hline(
            y=target_apr, 
            line_dash="dash", 
            line_color="orange",
            annotation_text=f"Target: {target_apr:.0f}%",
            annotation_position="right"
        )
        
        # Add zero line
        fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)
        
        fig.update_layout(
            title=f"{COIN_NAME} Funding Rate (Annualized)",
            xaxis_title="Time",
            yaxis_title="APR %",
            height=350,
            margin=dict(l=40, r=40, t=60, b=40),
            hovermode='x unified',
            showlegend=False,
            plot_bgcolor='rgba(0,0,0,0)',
            paper_bgcolor='rgba(0,0,0,0)'
        )
        
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.1)')
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='rgba(128,128,128,0.1)')
        
        st.plotly_chart(fig, use_container_width=True)
        
        # Stats row
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            avg_apr = funding_df['apr'].mean()
            st.metric("Avg APR (48h)", f"{avg_apr:.2f}%")
        with col2:
            max_apr = funding_df['apr'].max()
            st.metric("Max APR", f"{max_apr:.2f}%")
        with col3:
            min_apr = funding_df['apr'].min()
            st.metric("Min APR", f"{min_apr:.2f}%")
        with col4:
            current_apr = funding_df['apr'].iloc[-1] if len(funding_df) > 0 else 0
            st.metric("Current APR", f"{current_apr:.2f}%")
    else:
        st.warning("No funding history data available.")
    
    st.markdown("---")
    st.header("📊 Positions & Trades")
    
    # Open positions
    st.subheader("Active Positions")
    
    if not check_db_exists():
        st.info("Database not initialized yet.")
        return
    
    positions_df = query_df("""
        SELECT 
            coin,
            size,
            size_usd,
            entry_price_spot,
            entry_price_perp,
            opened_at,
            status
        FROM positions 
        WHERE status = 'OPEN'
        ORDER BY opened_at DESC
    """)
    
    if positions_df.empty:
        st.info("No open positions. Bot is waiting for opportunities.")
    else:
        st.dataframe(
            positions_df,
            use_container_width=True,
            hide_index=True
        )
        
        # Metrics
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Positions", len(positions_df))
        with col2:
            total_size = positions_df['size_usd'].sum()
            st.metric("Total Exposure", f"${total_size:.2f}")
        with col3:
            avg_entry = positions_df['entry_price_perp'].mean()
            st.metric("Avg Entry Price", f"${avg_entry:.4f}")
    
    # Recent trades
    st.subheader("Recent Trades")
    
    trades_df = query_df("""
        SELECT 
            coin,
            side,
            market,
            size,
            price,
            timestamp
        FROM trades 
        ORDER BY timestamp DESC 
        LIMIT 10
    """)
    
    if trades_df.empty:
        st.info("No trades executed yet.")
    else:
        st.dataframe(
            trades_df,
            use_container_width=True,
            hide_index=True
        )


# ========== TAB 2: PERFORMANCE ==========

def render_performance():
    """Render performance tab."""
    st.header("📈 Performance")
    
    if not check_db_exists():
        st.info("Database not initialized yet.")
        return
    
    # Funding payments over time
    st.subheader("Funding Payments")
    
    funding_df = query_df("""
        SELECT 
            date(timestamp) as date,
            SUM(amount_usdc) as daily_funding,
            COUNT(*) as payments
        FROM funding_log 
        GROUP BY date(timestamp)
        ORDER BY date DESC
        LIMIT 30
    """)
    
    if funding_df.empty:
        st.info("No funding payments recorded yet. Payments are credited hourly when you have an open position.")
    else:
        # Chart
        fig = px.bar(
            funding_df,
            x='date',
            y='daily_funding',
            title="Daily Funding Income",
            labels={'daily_funding': 'USD', 'date': 'Date'}
        )
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        
        # Summary metrics
        col1, col2, col3 = st.columns(3)
        with col1:
            total = funding_df['daily_funding'].sum()
            st.metric("Total Funding", f"${total:.4f}")
        with col2:
            avg = funding_df['daily_funding'].mean()
            st.metric("Avg Daily", f"${avg:.4f}")
        with col3:
            payments = funding_df['payments'].sum()
            st.metric("Total Payments", payments)
        
        # Table
        st.dataframe(
            funding_df,
            use_container_width=True,
            hide_index=True
        )
    
    # Cumulative equity
    st.subheader("Cumulative Earnings")
    
    cumulative_df = query_df("""
        SELECT 
            timestamp,
            amount_usdc,
            SUM(amount_usdc) OVER (ORDER BY timestamp) as cumulative
        FROM funding_log 
        ORDER BY timestamp
    """)
    
    if not cumulative_df.empty:
        fig = px.line(
            cumulative_df,
            x='timestamp',
            y='cumulative',
            title="Cumulative Funding Income",
            labels={'cumulative': 'USD', 'timestamp': 'Time'}
        )
        st.plotly_chart(fig, use_container_width=True)


# ========== TAB 3: LOGS ==========

def render_logs():
    """Render logs tab."""
    st.header("📋 Activity Logs")
    
    if not check_db_exists():
        st.info("Database not initialized yet.")
        return
    
    # Rebalance events
    st.subheader("Rebalance Events")
    
    rebalance_df = query_df("""
        SELECT 
            event_type,
            margin_ratio_before,
            margin_ratio_after,
            amount_usd,
            notes,
            timestamp
        FROM rebalance_events 
        ORDER BY timestamp DESC 
        LIMIT 20
    """)
    
    if rebalance_df.empty:
        st.info("No rebalance events recorded. These occur when margin gets low.")
    else:
        st.dataframe(
            rebalance_df,
            use_container_width=True,
            hide_index=True
        )
    
    # All trades
    st.subheader("Trade History")
    
    all_trades_df = query_df("""
        SELECT 
            coin,
            side,
            market,
            size,
            price,
            cloid,
            timestamp
        FROM trades 
        ORDER BY timestamp DESC 
        LIMIT 50
    """)
    
    if all_trades_df.empty:
        st.info("No trades in history.")
    else:
        st.dataframe(
            all_trades_df,
            use_container_width=True,
            hide_index=True
        )
    
    # Position history
    st.subheader("Position History")
    
    positions_df = query_df("""
        SELECT 
            coin,
            size_usd,
            entry_price_spot,
            entry_price_perp,
            exit_price_spot,
            exit_price_perp,
            status,
            close_reason,
            opened_at,
            closed_at
        FROM positions 
        ORDER BY opened_at DESC 
        LIMIT 20
    """)
    
    if not positions_df.empty:
        st.dataframe(
            positions_df,
            use_container_width=True,
            hide_index=True
        )


# ========== MAIN ==========

def main():
    """Main dashboard entry point."""
    
    # Sidebar
    render_sidebar()
    
    # Main content tabs
    tab1, tab2, tab3 = st.tabs(["📊 Live Status", "📈 Performance", "📋 Logs"])
    
    with tab1:
        render_live_status()
    
    with tab2:
        render_performance()
    
    with tab3:
        render_logs()
    
    # Auto-refresh
    time.sleep(30)
    st.rerun()


if __name__ == "__main__":
    main()
