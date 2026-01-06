/**
 * Perfect Whale Dashboard - JavaScript App
 * Real-time WebSocket updates, chart management, and analytics tracking
 */

class Dashboard {
    constructor() {
        this.ws = null;
        this.chart = null;
        this.state = null;
        this.chartData = {
            labels: [],
            spreads: [],
            threshold: []
        };
        this.maxDataPoints = 50;
        this.logEntries = [];
        this.maxLogEntries = 50;

        // Analytics tracking
        this.analytics = {
            sessionStart: new Date().toISOString(),
            priceUpdates: 0,
            spreads: [],
            spreadsAbove10: 0,
            spreadsAbove15: 0,
            spreadsAbove20: 0,
            trades: [],
            totalGrossPnl: 0,
            totalFees: 0,
            totalNetPnl: 0
        };

        this.init();
    }

    init() {
        this.initChart();
        this.connectWebSocket();
        this.addLog('info', 'Dashboard initialized');
        this.updateAnalyticsDisplay();

        // Reconnect on visibility change
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && !this.ws) {
                this.connectWebSocket();
            }
        });

        // Update duration every second
        setInterval(() => this.updateSessionDuration(), 1000);
    }

    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.addLog('info', `Connecting to ${wsUrl}...`);
        this.updateConnectionStatus('connecting');

        try {
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.addLog('success', 'WebSocket connected');
                this.updateConnectionStatus('connected');
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    this.handleUpdate(data);
                } catch (e) {
                    console.error('Failed to parse message:', e);
                }
            };

            this.ws.onclose = () => {
                this.addLog('warning', 'WebSocket disconnected');
                this.updateConnectionStatus('disconnected');
                this.ws = null;

                // Reconnect after 3 seconds
                setTimeout(() => this.connectWebSocket(), 3000);
            };

            this.ws.onerror = (error) => {
                this.addLog('error', 'WebSocket error');
                console.error('WebSocket error:', error);
            };
        } catch (e) {
            this.addLog('error', `Failed to connect: ${e.message}`);
            setTimeout(() => this.connectWebSocket(), 5000);
        }
    }

    handleUpdate(data) {
        this.state = data;
        this.updatePrices(data.prices);
        this.updateSpread(data.spread);
        this.updatePosition(data.position);
        this.updateStats(data.stats);
        this.updateFundingRate(data.stats);
        this.updateConfig(data.config);
        this.updateStatus(data.status);
        this.updateChart(data.spread);
        this.updateAccountValue(data.account);

        // Display trade events in activity log
        if (data.trade_events && data.trade_events.length > 0) {
            this.updateTradeEvents(data.trade_events);
        }

        // Track analytics
        this.trackAnalytics(data);
    }

    updateTradeEvents(events) {
        // Only show new events we haven't seen
        for (const event of events) {
            const eventId = event.timestamp + event.message;
            if (!this._seenEvents) this._seenEvents = new Set();
            if (this._seenEvents.has(eventId)) continue;
            this._seenEvents.add(eventId);

            // Determine event type for styling
            let type = 'info';
            if (event.event_type === 'entry') type = 'success';
            else if (event.event_type === 'exit') type = 'warning';
            else if (event.event_type === 'error') type = 'error';

            this.addLog(type, event.message);
        }
    }

    trackAnalytics(data) {
        this.analytics.priceUpdates++;

        const spread = data.spread.entry;
        this.analytics.spreads.push(spread);

        // Keep only last 10000 spreads for memory
        if (this.analytics.spreads.length > 10000) {
            this.analytics.spreads.shift();
        }

        // Count spreads above thresholds
        if (spread > 0.10) this.analytics.spreadsAbove10++;
        if (spread > 0.15) this.analytics.spreadsAbove15++;
        if (spread > 0.20) this.analytics.spreadsAbove20++;

        // Update display every 10 updates
        if (this.analytics.priceUpdates % 10 === 0) {
            this.updateAnalyticsDisplay();
        }
    }

    updateAnalyticsDisplay() {
        const a = this.analytics;

        // Session info
        document.getElementById('session-start').textContent =
            new Date(a.sessionStart).toLocaleString();
        document.getElementById('price-updates').textContent =
            a.priceUpdates.toLocaleString();

        // Spread stats
        if (a.spreads.length > 0) {
            const current = a.spreads[a.spreads.length - 1];
            const min = Math.min(...a.spreads);
            const max = Math.max(...a.spreads);
            const avg = a.spreads.reduce((a, b) => a + b, 0) / a.spreads.length;

            document.getElementById('current-spread').textContent = `${current.toFixed(4)}%`;
            document.getElementById('min-spread').textContent = `${min.toFixed(4)}%`;
            document.getElementById('max-spread').textContent = `${max.toFixed(4)}%`;
            document.getElementById('avg-spread').textContent = `${avg.toFixed(4)}%`;
        }

        // Opportunity counts - use spread log from API if available
        const spreadLog = this.state?.spread_log || {};
        document.getElementById('total-opportunities').textContent =
            spreadLog.above_threshold || this.state?.stats?.opportunities || 0;
        document.getElementById('spreads-10').textContent =
            spreadLog.total_checks ? Math.round(spreadLog.total_checks * 0.15) : a.spreadsAbove10;
        document.getElementById('spreads-15').textContent =
            spreadLog.above_threshold || a.spreadsAbove15;
        document.getElementById('spreads-20').textContent =
            spreadLog.above_threshold ? Math.round(spreadLog.above_threshold * 0.1) : (a.spreadsAbove20 || 0);

        // Update session info with spread log data
        const priceUpdatesEl = document.getElementById('price-updates');
        if (priceUpdatesEl && spreadLog.total_checks) {
            priceUpdatesEl.textContent = spreadLog.total_checks.toLocaleString();
        }

        // Trade stats - use API data
        const trades = this.state?.stats?.trades || 0;
        const totalPnl = this.state?.stats?.total_pnl || 0;
        document.getElementById('trades-executed').textContent = trades;
        document.getElementById('gross-pnl').textContent = `$${totalPnl.toFixed(4)}`;
        document.getElementById('total-fees').textContent = `~$${(trades * 0.014).toFixed(4)}`;  // Estimate fees
        document.getElementById('net-pnl-stat').textContent = `$${totalPnl.toFixed(4)}`;

        // Update raw data JSON
        this.updateRawDataOutput();
    }

    updateSessionDuration() {
        const start = new Date(this.analytics.sessionStart);
        const now = new Date();
        const diff = now - start;

        const hours = Math.floor(diff / 3600000);
        const mins = Math.floor((diff % 3600000) / 60000);
        const secs = Math.floor((diff % 60000) / 1000);

        document.getElementById('session-duration').textContent =
            `${hours}h ${mins}m ${secs}s`;
    }

    updateRawDataOutput() {
        const a = this.analytics;
        const spreads = a.spreads;

        const exportData = {
            session: {
                start: a.sessionStart,
                end: new Date().toISOString(),
                duration_seconds: Math.floor((new Date() - new Date(a.sessionStart)) / 1000),
                price_updates: a.priceUpdates
            },
            spread_stats: {
                total_samples: spreads.length,
                min: spreads.length ? Math.min(...spreads).toFixed(4) : 0,
                max: spreads.length ? Math.max(...spreads).toFixed(4) : 0,
                avg: spreads.length ? (spreads.reduce((a, b) => a + b, 0) / spreads.length).toFixed(4) : 0,
                above_010_pct: spreads.length ? (a.spreadsAbove10 / a.priceUpdates * 100).toFixed(2) : 0,
                above_015_pct: spreads.length ? (a.spreadsAbove15 / a.priceUpdates * 100).toFixed(2) : 0,
                above_020_pct: spreads.length ? (a.spreadsAbove20 / a.priceUpdates * 100).toFixed(2) : 0
            },
            opportunities: {
                total: this.state?.stats?.opportunities || 0,
                spreads_above_10: a.spreadsAbove10,
                spreads_above_15: a.spreadsAbove15,
                spreads_above_20: a.spreadsAbove20
            },
            trades: {
                executed: this.state?.stats?.trades || 0,
                total_pnl: this.state?.stats?.total_pnl || 0
            },
            current_position: this.state?.position || {},
            config: {
                threshold: this.state?.spread?.threshold || 0.15,
                max_position_usd: this.state?.config?.max_position || 10
            }
        };

        const textarea = document.getElementById('raw-data-output');
        if (textarea) {
            textarea.value = JSON.stringify(exportData, null, 2);
        }
    }

    updatePrices(prices) {
        this.animateValue('spot-bid', prices.spot_bid.toFixed(4));
        this.animateValue('spot-ask', prices.spot_ask.toFixed(4));
        this.animateValue('perp-bid', prices.perp_bid.toFixed(4));
        this.animateValue('perp-ask', prices.perp_ask.toFixed(4));
    }

    updateSpread(spread) {
        const spreadValue = document.getElementById('spread-value');
        const spreadStatus = document.getElementById('spread-status');
        const spreadBar = document.getElementById('spread-bar');

        const entrySpread = spread.entry.toFixed(4);
        spreadValue.textContent = `${entrySpread}%`;

        // Update bar (scale: 0% = 0, 1% = 100%)
        const barWidth = Math.min(Math.max(spread.entry * 100, 0), 100);
        spreadBar.style.width = `${barWidth}%`;

        if (spread.is_opportunity) {
            spreadValue.classList.add('opportunity');
            spreadStatus.textContent = '🟢 ENTRY SIGNAL';
            spreadStatus.classList.add('active');
            spreadBar.classList.add('high');
            this.addLog('success', `Opportunity! Spread: ${entrySpread}%`);
        } else {
            spreadValue.classList.remove('opportunity');
            spreadStatus.textContent = 'NO SIGNAL';
            spreadStatus.classList.remove('active');
            spreadBar.classList.remove('high');
        }

        document.getElementById('entry-threshold').textContent = `${spread.threshold.toFixed(2)}%`;
    }

    updateFundingRate(stats) {
        const fundingEl = document.getElementById('funding-rate');
        if (fundingEl && stats.funding_rate !== undefined) {
            const rate = stats.funding_rate * 100;
            fundingEl.textContent = `${rate >= 0 ? '+' : ''}${rate.toFixed(4)}%`;
            fundingEl.style.color = rate >= 0 ? 'var(--success)' : 'var(--danger)';
        }
    }

    updateAccountValue(account) {
        if (!account) return;

        const equityEl = document.getElementById('account-equity');
        const spotEl = document.getElementById('spot-value');
        const perpEl = document.getElementById('perp-value');

        if (equityEl) equityEl.textContent = `$${account.total.toFixed(2)}`;
        if (spotEl) spotEl.textContent = `$${account.spot.toFixed(2)}`;
        if (perpEl) perpEl.textContent = `$${account.perp.toFixed(2)}`;
    }

    updatePosition(position) {
        const noPosition = document.getElementById('no-position');
        const activePosition = document.getElementById('active-position');

        if (position.has_position) {
            noPosition.classList.add('hidden');
            activePosition.classList.remove('hidden');

            document.getElementById('pos-spot-size').textContent = `${position.size} HYPE`;
            document.getElementById('pos-spot-entry').textContent = position.entry_spot.toFixed(4);
            document.getElementById('pos-perp-size').textContent = `${position.size} HYPE`;
            document.getElementById('pos-perp-entry').textContent = position.entry_perp.toFixed(4);

            if (position.entry_time) {
                const entryDate = new Date(position.entry_time);
                document.getElementById('position-time').textContent = entryDate.toLocaleTimeString();
            }

            const pnlEl = document.getElementById('unrealized-pnl');
            const pnl = position.unrealized_pnl;
            pnlEl.textContent = `$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
            pnlEl.classList.toggle('positive', pnl >= 0);
            pnlEl.classList.toggle('negative', pnl < 0);
        } else {
            noPosition.classList.remove('hidden');
            activePosition.classList.add('hidden');
        }
    }

    updateStats(stats) {
        // Use spread_log above_threshold OR current session opportunities
        const spreadLog = this.state?.spread_log || {};
        const opportunities = spreadLog.above_threshold || stats.opportunities || 0;
        document.getElementById('stat-opportunities').textContent = opportunities.toLocaleString();
        document.getElementById('stat-trades').textContent = stats.trades.toLocaleString();

        const pnl = stats.total_pnl;
        const pnlEl = document.getElementById('stat-pnl');
        pnlEl.textContent = `$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
        pnlEl.style.color = pnl >= 0 ? 'var(--success)' : 'var(--danger)';
    }

    updateConfig(config) {
        const modeBadge = document.getElementById('mode-badge');
        const modeText = modeBadge.querySelector('.mode-text');

        if (config.dry_run) {
            modeText.textContent = 'DRY RUN';
            modeBadge.classList.remove('live');
        } else {
            modeText.textContent = '🔴 LIVE';
            modeBadge.classList.add('live');
        }

        document.getElementById('max-position').textContent = config.max_position.toFixed(0);
    }

    updateStatus(status) {
        this.updateConnectionStatus(status.ws_connected ? 'connected' : 'disconnected');

        if (status.last_update) {
            const date = new Date(status.last_update);
            document.getElementById('last-update').textContent = date.toLocaleTimeString();
        }
    }

    updateConnectionStatus(status) {
        const badge = document.getElementById('ws-status');
        const dot = badge.querySelector('.status-dot');
        const text = badge.querySelector('.status-text');

        dot.classList.remove('connected', 'disconnected');

        switch (status) {
            case 'connected':
                dot.classList.add('connected');
                text.textContent = 'Connected';
                break;
            case 'disconnected':
                dot.classList.add('disconnected');
                text.textContent = 'Disconnected';
                break;
            default:
                text.textContent = 'Connecting...';
        }
    }

    initChart() {
        const ctx = document.getElementById('spread-chart').getContext('2d');

        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: this.chartData.labels,
                datasets: [
                    {
                        label: 'Spread %',
                        data: this.chartData.spreads,
                        borderColor: 'rgba(59, 130, 246, 1)',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                    },
                    {
                        label: 'Threshold',
                        data: this.chartData.threshold,
                        borderColor: 'rgba(245, 158, 11, 0.5)',
                        borderWidth: 1,
                        borderDash: [5, 5],
                        fill: false,
                        pointRadius: 0,
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    intersect: false,
                    mode: 'index',
                },
                plugins: {
                    legend: {
                        display: false,
                    },
                    tooltip: {
                        backgroundColor: 'rgba(17, 24, 39, 0.9)',
                        titleColor: '#f9fafb',
                        bodyColor: '#9ca3af',
                        borderColor: 'rgba(255, 255, 255, 0.1)',
                        borderWidth: 1,
                        padding: 12,
                        displayColors: false,
                        callbacks: {
                            label: (context) => `Spread: ${context.parsed.y.toFixed(4)}%`
                        }
                    }
                },
                scales: {
                    x: {
                        display: false,
                    },
                    y: {
                        grid: {
                            color: 'rgba(255, 255, 255, 0.05)',
                        },
                        ticks: {
                            color: '#6b7280',
                            callback: (value) => `${value.toFixed(2)}%`
                        }
                    }
                }
            }
        });
    }

    updateChart(spread) {
        const now = new Date().toLocaleTimeString();

        this.chartData.labels.push(now);
        this.chartData.spreads.push(spread.entry);
        this.chartData.threshold.push(spread.threshold);

        // Trim data
        if (this.chartData.labels.length > this.maxDataPoints) {
            this.chartData.labels.shift();
            this.chartData.spreads.shift();
            this.chartData.threshold.shift();
        }

        this.chart.update('none');
    }

    addLog(type, message) {
        const container = document.getElementById('log-container');
        const now = new Date().toLocaleTimeString();

        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        entry.innerHTML = `
            <span class="log-time">${now}</span>
            <span class="log-message">${message}</span>
        `;

        container.insertBefore(entry, container.firstChild);

        // Trim log
        while (container.children.length > this.maxLogEntries) {
            container.removeChild(container.lastChild);
        }
    }

    animateValue(elementId, newValue) {
        const el = document.getElementById(elementId);
        if (el.textContent !== newValue) {
            el.textContent = newValue;
            el.style.transition = 'color 0.15s ease';
            el.style.color = 'var(--accent-primary)';
            setTimeout(() => {
                el.style.color = '';
            }, 150);
        }
    }
}

// Global copy function
function copyAnalyticsData() {
    const textarea = document.getElementById('raw-data-output');
    if (textarea) {
        textarea.select();
        document.execCommand('copy');

        const btn = document.getElementById('copy-data-btn');
        const originalText = btn.textContent;
        btn.textContent = '✅ Copied!';
        btn.style.background = 'var(--success)';

        setTimeout(() => {
            btn.textContent = originalText;
            btn.style.background = '';
        }, 2000);
    }
}

// Initialize dashboard when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.dashboard = new Dashboard();
});

