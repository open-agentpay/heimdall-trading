/**
 * ================================================================================
 * Heimdall 交易平台 - 左侧机器人与数据源探索栏 (Sidebar)
 * ================================================================================
 *
 * 【P1 真实性对齐】：
 * 1. 移除写死的 "280k" / "529k" Token 与 "$10,420.50 / 1.20%" 假数字，绑定后端真实统计；
 * 2. 显示每个机器人的运行模式徽章 (PAPER / TESTNET / MAINNET) 与 MacroBias 门控状态。
 */

import React from 'react';
import { Key, Plus, PieChart, Trash2 } from 'lucide-react';

export default function Sidebar({
  activeBotId,
  onSelectBot,
  onDeleteBot,
  walletState,
  onOpenWalletModal,
  onOpenKeyVaultModal,
  onOpenCreateBotModal,
  onOpenCapitalModal,
  botsList = [],
}) {
  const bots = botsList.map((b) => ({
    id: b.bot_id || b.id,
    name: b.name,
    tag: b.status === 'RUNNING' ? '活跃运行' : (b.status || '待命'),
    symbol: b.symbol,
    timeframe: b.timeframe || '15m',
    nav: Number(b.latest_nav || b.nav || 1.0),
    runtime: b.runtime || b.status || '待命',
    tokens: b.tokens !== undefined && b.tokens !== null ? String(b.tokens) : '0',
    exchange: b.exchange || 'BINANCE',
    marketType: b.market_type || 'USDM_FUTURES',
    executionMode: b.execution_mode || 'LOCAL_SIM',
    macroBias: b.macro_bias || null,
    currentEquity: Number(b.current_equity || b.allocated_capital || 0),
    cumulativeReturnPct: Number(b.cumulative_return_pct || 0),
    maxDrawdownPct: Number(b.max_drawdown_pct || 0),
  }));

  const currentBot = bots.find((b) => b.id === activeBotId) || bots[0];
  const ddPct = currentBot ? Math.min(100, (currentBot.maxDrawdownPct / 5.0) * 100) : 0;

  return (
    <aside className="w-64 bg-panel border-r border-line flex flex-col shrink-0 min-h-0 overflow-y-auto">
      {/* 分组 1: 机器人实例列表 */}
      <div className="p-3 border-b border-line">
        <div className="flex items-center justify-between text-xs font-semibold text-ink-3 uppercase tracking-wider mb-2">
          <div className="flex items-center space-x-1.5">
            <span>策略机器人</span>
            <span className="text-xs bg-muted px-1.5 py-0.5 rounded text-ink-2 font-mono">
              {bots.length} 个
            </span>
          </div>
          <div className="flex items-center space-x-1">
            <button
              onClick={onOpenCapitalModal}
              title="查看与修改各机器人资金占比"
              className="px-1.5 py-0.5 rounded bg-panel hover:bg-surface border border-line-strong text-ink-2 hover:text-brand transition text-xs font-medium flex items-center space-x-0.5"
            >
              <PieChart className="w-2.5 h-2.5 text-brand" />
              <span>配比</span>
            </button>
            <button
              onClick={onOpenCreateBotModal}
              title="新建策略交易机器人"
              className="px-1.5 py-0.5 rounded bg-brand hover:bg-brand-hover text-white transition text-xs font-bold flex items-center space-x-0.5 shadow-xs"
            >
              <Plus className="w-2.5 h-2.5" />
              <span>新建</span>
            </button>
          </div>
        </div>

        <div className="space-y-1.5 max-h-[340px] overflow-y-auto">
          {bots.length === 0 ? (
            <div className="p-3 text-center text-xs text-ink-3 bg-surface rounded-xl border border-line">
              正在加载机器人列表...
            </div>
          ) : (
            bots.map((bot) => (
              <div
                key={bot.id}
                onClick={() => onSelectBot(bot.id)}
                className={`p-2.5 rounded-xl border cursor-pointer transition ${
                  activeBotId === bot.id
                    ? 'bg-surface border-brand shadow-sm border-l-4'
                    : 'bg-panel border-line hover:bg-surface opacity-85 hover:opacity-100'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-bold text-ink truncate pr-1">{bot.name}</span>
                  <span
                    className={`text-xs px-1.5 py-0.2 rounded font-mono shrink-0 ${
                      activeBotId === bot.id
                        ? 'bg-brand text-white'
                        : 'bg-muted-strong text-ink-3'
                    }`}
                  >
                    {bot.tag}
                  </span>
                </div>
                <div className="flex items-center justify-between text-xs text-ink-3">
                  <span>{bot.symbol} · {bot.exchange}</span>
                  <span className="font-mono text-success font-semibold">NAV {bot.nav.toFixed(3)}</span>
                </div>
                <div className="mt-1.5 pt-1.5 border-t border-line-soft flex items-center justify-between text-xs text-ink-4">
                  <div className="flex items-center space-x-1.5">
                    <span className="font-mono px-1 rounded bg-subtle text-ink-3">{bot.marketType} · {bot.executionMode}</span>
                    {bot.macroBias && (
                      <span className="text-warning font-bold">🛡️ {bot.macroBias.action}</span>
                    )}
                  </div>
                  <div className="flex items-center space-x-1.5">
                    <span title="真实累计 LLM Token 消耗">🔥 {bot.tokens}</span>
                    {onDeleteBot && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteBot(bot);
                        }}
                        title={`删除机器人 ${bot.name}`}
                        className="p-1 rounded text-ink-4 hover:text-danger hover:bg-danger-solid/10 transition cursor-pointer"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 分组 2: 数据底座与信号感知源状态树 */}
      <div className="flex-1 min-h-[160px] overflow-y-auto p-3">
        <div className="flex items-center justify-between text-xs font-semibold text-ink-3 uppercase tracking-wider mb-2">
          <span>底座信号源 (Data Hub)</span>
          <span className="text-xs text-ink-4">零前视接入</span>
        </div>

        <div className="space-y-1 text-xs text-ink-2">
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-brand">📁</span>
            <span className="font-medium">multi_source.klines</span>
            <span className="ml-auto text-xs text-success font-mono">已封盘</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-info">⚡</span>
            <span>funding_rate_hub</span>
            <span className="ml-auto text-xs text-ink-3 font-mono">实时</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-success">🛡️</span>
            <span>oms_native_sl_tp</span>
            <span className="ml-auto text-xs text-success font-mono">启用</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-plum">📅</span>
            <span>macro_fomc_guard</span>
            <span className="ml-auto text-xs text-ink-3 font-mono">监听中</span>
          </div>
        </div>
      </div>

      {/* 分组 3: 底部当前 Bot 真实资产与回撤水位槽 + 钱包快捷入口 */}
      <div className="p-3 bg-subtle border-t border-line space-y-2">
        <div
          onClick={onOpenWalletModal}
          className="flex items-center justify-between p-1.5 rounded-xl hover:bg-muted cursor-pointer transition"
          title="点击管理 Web3 钱包"
        >
          <div className="flex items-center space-x-2">
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center font-bold text-xs shadow-xs ${
                walletState?.isConnected ? 'bg-brand text-white' : 'bg-muted-strong text-ink-2'
              }`}
            >
              {walletState?.isConnected ? '⚡' : '未'}
            </div>
            <div>
              <div className="text-xs font-bold text-ink flex items-center space-x-1">
                <span>{walletState?.isConnected ? (walletState.shortAddress || '已连接') : '未连接 Web3 钱包'}</span>
              </div>
              <div className="text-xs text-ink-3">
                {walletState?.isConnected ? `${walletState.network || 'Arbitrum'} Bearer 认证` : '点击签名登录'}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs font-mono font-bold text-ink">
              ${currentBot ? currentBot.currentEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '0.00'}
            </div>
            <div
              className={`text-xs font-mono font-semibold ${
                (currentBot?.cumulativeReturnPct || 0) >= 0 ? 'text-success' : 'text-danger'
              }`}
            >
              {(currentBot?.cumulativeReturnPct || 0) >= 0 ? '+' : ''}
              {(currentBot?.cumulativeReturnPct || 0).toFixed(2)}%
            </div>
          </div>
        </div>

        <div className="w-full bg-muted-strong h-1.5 rounded-full overflow-hidden">
          <div className="bg-success-solid h-full rounded-full transition-all" style={{ width: `${ddPct}%` }} />
        </div>
        <div className="flex justify-between text-xs text-ink-3 font-mono">
          <span>最大回撤: {(currentBot?.maxDrawdownPct || 0).toFixed(2)}%</span>
          <span>熔断线: 5.00%</span>
        </div>

        <button
          onClick={onOpenKeyVaultModal}
          className="w-full py-1.5 px-2 rounded-lg bg-panel hover:bg-surface border border-line-strong text-xs font-bold text-ink flex items-center justify-center space-x-1.5 transition shadow-xs"
        >
          <Key className="w-3.5 h-3.5 text-brand" />
          <span>🔑 凭证金库 (Hyperliquid / AI)</span>
        </button>
      </div>
    </aside>
  );
}
