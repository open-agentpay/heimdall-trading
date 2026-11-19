-- Heimdall Initial Schema Migration
-- Designed for PostgreSQL 16 & TimescaleDB

CREATE TABLE IF NOT EXISTS trading_agents (
    agent_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    strategy_type VARCHAR(64) NOT NULL,            -- e.g., FUNDING_ARB, TREND_FOLLOW, GRID, BULL_BEAR_DEBATE
    status VARCHAR(32) NOT NULL DEFAULT 'STOPPED', -- RUNNING, STOPPED, CIRCUIT_BROKEN, PAUSED
    allocated_capital NUMERIC(24, 8) NOT NULL,
    max_drawdown_limit NUMERIC(6, 4) NOT NULL DEFAULT 0.05, -- 5% hard drawdown trigger
    model_provider VARCHAR(64) NOT NULL,           -- e.g., claude-3-5-sonnet, gpt-4o
    system_prompt TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id VARCHAR(64) PRIMARY KEY,
    agent_id VARCHAR(64) NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    as_of_ts TIMESTAMPTZ NOT NULL,
    market_regime VARCHAR(64),
    thinking_process TEXT NOT NULL,                -- Full chain of thought / debate transcription
    proposal JSONB NOT NULL,                       -- Structured JSON proposal from Strategy/Risk Agent
    risk_review_status VARCHAR(32) NOT NULL,       -- APPROVED, REJECTED, MUTED
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_agent_ts ON agent_decisions (agent_id, as_of_ts DESC);

CREATE TABLE IF NOT EXISTS trades (
    trade_id VARCHAR(64) PRIMARY KEY,
    order_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(64) NOT NULL REFERENCES trading_agents(agent_id),
    decision_id VARCHAR(64) REFERENCES agent_decisions(decision_id),
    exchange VARCHAR(32) NOT NULL,                 -- HYPERLIQUID, BINANCE, OKX
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,                     -- BUY, SELL
    position_side VARCHAR(16) NOT NULL,            -- LONG, SHORT
    price NUMERIC(24, 8) NOT NULL,
    quantity NUMERIC(24, 8) NOT NULL,
    realized_pnl NUMERIC(24, 8) DEFAULT 0,
    fee NUMERIC(24, 8) NOT NULL DEFAULT 0,
    fee_asset VARCHAR(16) NOT NULL DEFAULT 'USDT',
    executed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_agent_exec ON trades (agent_id, executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, executed_at DESC);

CREATE TABLE IF NOT EXISTS agent_equity_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    agent_id VARCHAR(64) NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    timestamp TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC(24, 8) NOT NULL,          -- Total assets in USD
    cash_balance NUMERIC(24, 8) NOT NULL,          -- Available cash
    unrealized_pnl NUMERIC(24, 8) NOT NULL,        -- Floating PnL
    shares NUMERIC(24, 8) NOT NULL,                -- Total accounting shares
    nav NUMERIC(14, 6) NOT NULL,                   -- Unit Net Asset Value (e.g. 1.054321)
    high_water_mark NUMERIC(14, 6) NOT NULL,       -- All-time high NAV
    drawdown NUMERIC(8, 6) NOT NULL,               -- Current drawdown from peak (0.00 ~ 1.00)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_equity_snapshots_agent_ts ON agent_equity_snapshots (agent_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS agent_daily_metrics (
    date DATE NOT NULL,
    agent_id VARCHAR(64) NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    starting_nav NUMERIC(14, 6) NOT NULL,
    ending_nav NUMERIC(14, 6) NOT NULL,
    daily_return NUMERIC(8, 6) NOT NULL,
    cumulative_return NUMERIC(8, 6) NOT NULL,
    max_drawdown NUMERIC(8, 6) NOT NULL,
    sharpe_ratio NUMERIC(8, 4),
    win_rate NUMERIC(6, 4),
    total_trades INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (date, agent_id)
);
