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
    executionMode: b.execution_mode || 'PAPER',
    macroBias: b.macro_bias || null,
    currentEquity: Number(b.current_equity || b.allocated_capital || 0),
    cumulativeReturnPct: Number(b.cumulative_return_pct || 0),
    maxDrawdownPct: Number(b.max_drawdown_pct || 0),
  }));

  const currentBot = bots.find((b) => b.id === activeBotId) || bots[0];
  const ddPct = currentBot ? Math.min(100, (currentBot.maxDrawdownPct / 5.0) * 100) : 0;

  return (
    <aside className="w-64 bg-[#FAF7F2] border-r border-[#E6DFD5] flex flex-col shrink-0 select-none">
      {/* 分组 1: 机器人实例列表 */}
      <div className="p-3 border-b border-[#EBE4DA]">
        <div className="flex items-center justify-between text-xs font-semibold text-[#8C8276] uppercase tracking-wider mb-2">
          <div className="flex items-center space-x-1.5">
            <span>策略机器人</span>
            <span className="text-[10px] bg-[#EFE9DF] px-1.5 py-0.5 rounded text-[#5E554B] font-mono">
              {bots.length} 个
            </span>
          </div>
          <div className="flex items-center space-x-1">
            <button
              onClick={onOpenCapitalModal}
              title="查看与修改各机器人资金占比"
              className="px-1.5 py-0.5 rounded bg-[#FAF7F2] hover:bg-white border border-[#DDD5C7] text-[#5E554B] hover:text-[#DE6438] transition text-[10px] font-medium flex items-center space-x-0.5"
            >
              <PieChart className="w-2.5 h-2.5 text-[#DE6438]" />
              <span>配比</span>
            </button>
            <button
              onClick={onOpenCreateBotModal}
              title="新建策略交易机器人"
              className="px-1.5 py-0.5 rounded bg-[#DE6438] hover:bg-[#C5532A] text-white transition text-[10px] font-bold flex items-center space-x-0.5 shadow-xs"
            >
              <Plus className="w-2.5 h-2.5" />
              <span>新建</span>
            </button>
          </div>
        </div>

        <div className="space-y-1.5 max-h-[340px] overflow-y-auto">
          {bots.length === 0 ? (
            <div className="p-3 text-center text-[11px] text-[#8C8276] bg-white rounded-xl border border-[#E6DFD5]">
              正在加载机器人列表...
            </div>
          ) : (
            bots.map((bot) => (
              <div
                key={bot.id}
                onClick={() => onSelectBot(bot.id)}
                className={`p-2.5 rounded-xl border cursor-pointer transition ${
                  activeBotId === bot.id
                    ? 'bg-white border-[#DE6438] shadow-sm border-l-4'
                    : 'bg-[#FAF7F2] border-[#E6DFD5] hover:bg-white opacity-85 hover:opacity-100'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-bold text-[#2E2823] truncate pr-1">{bot.name}</span>
                  <span
                    className={`text-[10px] px-1.5 py-0.2 rounded font-mono shrink-0 ${
                      activeBotId === bot.id
                        ? 'bg-[#DE6438] text-white'
                        : 'bg-[#E2DACF] text-[#6E6458]'
                    }`}
                  >
                    {bot.tag}
                  </span>
                </div>
                <div className="flex items-center justify-between text-[11px] text-[#7E7469]">
                  <span>{bot.symbol} · {bot.exchange}</span>
                  <span className="font-mono text-[#4D835C] font-semibold">NAV {bot.nav.toFixed(3)}</span>
                </div>
                <div className="mt-1.5 pt-1.5 border-t border-[#F2EDE4] flex items-center justify-between text-[10px] text-[#9E9486]">
                  <div className="flex items-center space-x-1.5">
                    <span className="font-mono px-1 rounded bg-[#F5F0E6] text-[#6E6458]">{bot.executionMode}</span>
                    {bot.macroBias && (
                      <span className="text-[#C28B38] font-bold">🛡️ {bot.macroBias.action}</span>
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
                        className="p-1 rounded text-[#9E9486] hover:text-[#C24D4D] hover:bg-[#C24D4D]/10 transition cursor-pointer"
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
      <div className="flex-1 overflow-y-auto p-3">
        <div className="flex items-center justify-between text-xs font-semibold text-[#8C8276] uppercase tracking-wider mb-2">
          <span>底座信号源 (Data Hub)</span>
          <span className="text-[10px] text-[#A89F93]">零前视接入</span>
        </div>

        <div className="space-y-1 text-xs text-[#5E554B]">
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF]">
            <span className="text-[#DE6438]">📁</span>
            <span className="font-medium">multi_source.klines</span>
            <span className="ml-auto text-[10px] text-[#4D835C] font-mono">已封盘</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF]">
            <span className="text-[#3E6B89]">⚡</span>
            <span>funding_rate_hub</span>
            <span className="ml-auto text-[10px] text-[#7E7469] font-mono">实时</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF]">
            <span className="text-[#4D835C]">🛡️</span>
            <span>oms_native_sl_tp</span>
            <span className="ml-auto text-[10px] text-[#4D835C] font-mono">启用</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF]">
            <span className="text-[#A25D8F]">📅</span>
            <span>macro_fomc_guard</span>
            <span className="ml-auto text-[10px] text-[#8C8276] font-mono">监听中</span>
          </div>
        </div>
      </div>

      {/* 分组 3: 底部当前 Bot 真实资产与回撤水位槽 + 钱包快捷入口 */}
      <div className="p-3 bg-[#F2EDE4] border-t border-[#E6DFD5] space-y-2">
        <div
          onClick={onOpenWalletModal}
          className="flex items-center justify-between p-1.5 rounded-xl hover:bg-[#EAE4D9] cursor-pointer transition"
          title="点击管理 Web3 钱包"
        >
          <div className="flex items-center space-x-2">
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center font-bold text-xs shadow-xs ${
                walletState?.isConnected ? 'bg-[#DE6438] text-white' : 'bg-[#DDD5C7] text-[#5A5046]'
              }`}
            >
              {walletState?.isConnected ? '⚡' : '未'}
            </div>
            <div>
              <div className="text-xs font-bold text-[#2E2823] flex items-center space-x-1">
                <span>{walletState?.isConnected ? (walletState.shortAddress || '已连接') : '未连接 Web3 钱包'}</span>
              </div>
              <div className="text-[10px] text-[#7E7469]">
                {walletState?.isConnected ? `${walletState.network || 'Arbitrum'} Bearer 认证` : '点击签名登录'}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs font-mono font-bold text-[#2E2823]">
              ${currentBot ? currentBot.currentEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '0.00'}
            </div>
            <div
              className={`text-[10px] font-mono font-semibold ${
                (currentBot?.cumulativeReturnPct || 0) >= 0 ? 'text-[#4D835C]' : 'text-[#C24D4D]'
              }`}
            >
              {(currentBot?.cumulativeReturnPct || 0) >= 0 ? '+' : ''}
              {(currentBot?.cumulativeReturnPct || 0).toFixed(2)}%
            </div>
          </div>
        </div>

        <div className="w-full bg-[#E5DACB] h-1.5 rounded-full overflow-hidden">
          <div className="bg-[#4D835C] h-full rounded-full transition-all" style={{ width: `${ddPct}%` }} />
        </div>
        <div className="flex justify-between text-[10px] text-[#8C8276] font-mono">
          <span>最大回撤: {(currentBot?.maxDrawdownPct || 0).toFixed(2)}%</span>
          <span>熔断线: 5.00%</span>
        </div>

        <button
          onClick={onOpenKeyVaultModal}
          className="w-full py-1.5 px-2 rounded-lg bg-[#FAF7F2] hover:bg-white border border-[#DDD5C7] text-[11px] font-bold text-[#2E2823] flex items-center justify-center space-x-1.5 transition shadow-xs"
        >
          <Key className="w-3.5 h-3.5 text-[#DE6438]" />
          <span>🔑 凭证金库 (Hyperliquid / AI)</span>
        </button>
      </div>
    </aside>
  );
}
