-- ============================================================================
-- Heimdall Trading Platform - Production PostgreSQL Schema Migration (v2)
-- 兼容 PostgreSQL 14+ / 16+，覆盖全部 9 张核心业务表与高并发复合索引
-- 执行方式：psql "$HEIMDALL_DATABASE_URL" -f migrations/000002_production_postgres_schema.up.sql
-- ============================================================================

-- 1. 交易机器人元数据表（含多租户归属列 owner_wallet）
CREATE TABLE IF NOT EXISTS trading_agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'STOPPED',
    allocated_capital DOUBLE PRECISION NOT NULL,
    max_drawdown_limit DOUBLE PRECISION NOT NULL DEFAULT 0.05,
    owner_wallet TEXT NOT NULL DEFAULT 'system',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

ALTER TABLE trading_agents ADD COLUMN IF NOT EXISTS owner_wallet TEXT NOT NULL DEFAULT 'system';
CREATE INDEX IF NOT EXISTS idx_trading_agents_owner ON trading_agents (owner_wallet, created_at DESC);

-- 2. 智能体决策与多空辩论审计表
CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    market_regime TEXT,
    thinking_logs_json TEXT NOT NULL,
    proposal_json TEXT,
    risk_approved INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    execution_json TEXT                 -- 2026-11-24：本轮执行结果（状态、原因、每笔订单），与风控结论分开
);

CREATE INDEX IF NOT EXISTS idx_decisions_agent_ts ON agent_decisions (agent_id, timestamp DESC);

-- 3. 成交流水表
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    decision_id TEXT,
    symbol TEXT NOT NULL,
    order_side TEXT NOT NULL,
    position_side TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    size_in_usd DOUBLE PRECISION NOT NULL,
    fee_usd DOUBLE PRECISION NOT NULL,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    executed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_agent_exec ON trades (agent_id, executed_at DESC);

-- 4. 单位净值 (NAV) 与权益时序快照表
CREATE TABLE IF NOT EXISTS agent_equity_snapshots (
    id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES trading_agents(agent_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    total_equity DOUBLE PRECISION NOT NULL,
    cash_balance DOUBLE PRECISION NOT NULL,
    margin_used DOUBLE PRECISION NOT NULL,
    unrealized_pnl DOUBLE PRECISION NOT NULL,
    realized_pnl_cum DOUBLE PRECISION NOT NULL,
    total_shares DOUBLE PRECISION NOT NULL,
    nav DOUBLE PRECISION NOT NULL,
    peak_nav DOUBLE PRECISION NOT NULL,
    drawdown_pct DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nav_agent_ts ON agent_equity_snapshots (agent_id, timestamp ASC);

-- 5. Web3 钱包登录与 Bearer 会话表
CREATE TABLE IF NOT EXISTS user_wallets (
    address TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    chain_id INTEGER NOT NULL DEFAULT 1,
    session_token TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_wallets_session ON user_wallets (session_token);

-- 6. 安全金库密文凭证表 (AES-256-GCM)
CREATE TABLE IF NOT EXISTS user_api_keys (
    key_id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    category TEXT NOT NULL,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    key_mask TEXT NOT NULL,
    encrypted_data TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_tested_at TEXT,
    last_test_status TEXT DEFAULT 'PENDING',
    last_test_latency_ms DOUBLE PRECISION DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vault_wallet_provider ON user_api_keys (wallet_address, provider, is_active);

-- 7. 多 Agent 流水线检查点表 (Durable Execution Checkpoints)
CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    step_name TEXT NOT NULL,
    state_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON pipeline_checkpoints (task_id, created_at ASC);

-- 8. 平仓反思经验表 (lessons_learned)
CREATE TABLE IF NOT EXISTS agent_reflections (
    reflection_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_id TEXT,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    market_regime TEXT,
    lesson_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reflections_agent_sym ON agent_reflections (agent_id, symbol, created_at DESC);

-- 9. OMS 订单生命周期状态机表 (Write-Ahead + 原生条件止损/止盈)
CREATE TABLE IF NOT EXISTS oms_orders (
    client_order_id TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    bot_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    position_side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    requested_qty TEXT NOT NULL,
    filled_qty TEXT NOT NULL DEFAULT '0',
    limit_price TEXT,
    avg_fill_price TEXT,
    trigger_price TEXT,
    parent_client_order_id TEXT,
    state TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oms_orders_bot_state ON oms_orders (bot_id, state, created_at DESC);
