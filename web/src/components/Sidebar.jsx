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
import { useTranslation } from 'react-i18next';
import { Key, Plus, PieChart, Trash2 } from 'lucide-react';
import { fmt } from '../i18n';

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
  const { t } = useTranslation('sidebar');
  const bots = botsList.map((b) => ({
    id: b.bot_id || b.id,
    name: b.name,
    tag: b.status === 'RUNNING' ? t('bots.running') : (b.status || t('bots.standby')),
    symbol: b.symbol,
    timeframe: b.timeframe || '15m',
    nav: Number(b.latest_nav || b.nav || 1.0),
    runtime: b.runtime || b.status || t('bots.standby'),
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
            <span>{t('bots.title')}</span>
            <span className="text-xs bg-muted px-1.5 py-0.5 rounded text-ink-2 font-mono">
              {t('bots.count', { n: bots.length })}
            </span>
          </div>
          <div className="flex items-center space-x-1">
            <button
              onClick={onOpenCapitalModal}
              title={t('bots.capitalTitle')}
              className="px-1.5 py-0.5 rounded bg-panel hover:bg-surface border border-line-strong text-ink-2 hover:text-brand transition text-xs font-medium flex items-center space-x-0.5"
            >
              <PieChart className="w-2.5 h-2.5 text-brand" />
              <span>{t('bots.capital')}</span>
            </button>
            <button
              onClick={onOpenCreateBotModal}
              title={t('bots.createTitle')}
              className="px-1.5 py-0.5 rounded bg-brand hover:bg-brand-hover text-white transition text-xs font-bold flex items-center space-x-0.5 shadow-xs"
            >
              <Plus className="w-2.5 h-2.5" />
              <span>{t('bots.create')}</span>
            </button>
          </div>
        </div>

        <div className="space-y-1.5 max-h-[340px] overflow-y-auto">
          {bots.length === 0 ? (
            <div className="p-3 text-center text-xs text-ink-3 bg-surface rounded-xl border border-line">
              {t('bots.loading')}
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
                  <span className="font-mono text-success font-semibold">NAV {fmt.number(bot.nav, 3)}</span>
                </div>
                <div className="mt-1.5 pt-1.5 border-t border-line-soft flex items-center justify-between text-xs text-ink-4">
                  <div className="flex items-center space-x-1.5">
                    <span className="font-mono px-1 rounded bg-subtle text-ink-3">{bot.marketType} · {bot.executionMode}</span>
                    {bot.macroBias && (
                      <span className="text-warning font-bold">🛡️ {bot.macroBias.action}</span>
                    )}
                  </div>
                  <div className="flex items-center space-x-1.5">
                    <span title={t('bots.tokensTitle')}>🔥 {bot.tokens}</span>
                    {onDeleteBot && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteBot(bot);
                        }}
                        title={t('bots.delete', { name: bot.name })}
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
          <span>{t('dataHub.title')}</span>
          <span className="text-xs text-ink-4">{t('dataHub.zeroLookahead')}</span>
        </div>

        <div className="space-y-1 text-xs text-ink-2">
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-brand">📁</span>
            <span className="font-medium">multi_source.klines</span>
            <span className="ml-auto text-xs text-success font-mono">{t('dataHub.sealed')}</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-info">⚡</span>
            <span>funding_rate_hub</span>
            <span className="ml-auto text-xs text-ink-3 font-mono">{t('dataHub.live')}</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-success">🛡️</span>
            <span>oms_native_sl_tp</span>
            <span className="ml-auto text-xs text-success font-mono">{t('dataHub.enabled')}</span>
          </div>
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-muted">
            <span className="text-plum">📅</span>
            <span>macro_fomc_guard</span>
            <span className="ml-auto text-xs text-ink-3 font-mono">{t('dataHub.listening')}</span>
          </div>
        </div>
      </div>

      {/* 分组 3: 底部当前 Bot 真实资产与回撤水位槽 + 钱包快捷入口 */}
      <div className="p-3 bg-subtle border-t border-line space-y-2">
        <div
          onClick={onOpenWalletModal}
          className="flex items-center justify-between p-1.5 rounded-xl hover:bg-muted cursor-pointer transition"
          title={t('wallet.manageTitle')}
        >
          <div className="flex items-center space-x-2">
            <div
              className={`w-7 h-7 rounded-full flex items-center justify-center font-bold text-xs shadow-xs ${
                walletState?.isConnected ? 'bg-brand text-white' : 'bg-muted-strong text-ink-2'
              }`}
            >
              {walletState?.isConnected ? '⚡' : t('wallet.offBadge')}
            </div>
            <div>
              <div className="text-xs font-bold text-ink flex items-center space-x-1">
                <span>{walletState?.isConnected ? (walletState.shortAddress || t('wallet.connected')) : t('wallet.disconnected')}</span>
              </div>
              <div className="text-xs text-ink-3">
                {walletState?.isConnected ? t('wallet.bearerAuth', { network: walletState.network || 'Arbitrum' }) : t('wallet.signIn')}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs font-mono font-bold text-ink">
              {fmt.usd(currentBot ? currentBot.currentEquity : 0)}
            </div>
            <div
              className={`text-xs font-mono font-semibold ${
                (currentBot?.cumulativeReturnPct || 0) >= 0 ? 'text-success' : 'text-danger'
              }`}
            >
              {(currentBot?.cumulativeReturnPct || 0) >= 0 ? '+' : ''}
              {fmt.pct((currentBot?.cumulativeReturnPct || 0) / 100)}
            </div>
          </div>
        </div>

        <div className="w-full bg-muted-strong h-1.5 rounded-full overflow-hidden">
          <div className="bg-success-solid h-full rounded-full transition-all" style={{ width: `${ddPct}%` }} />
        </div>
        <div className="flex justify-between text-xs text-ink-3 font-mono">
          <span>{t('wallet.maxDrawdown', { value: fmt.pct((currentBot?.maxDrawdownPct || 0) / 100) })}</span>
          <span>{t('wallet.breaker', { value: fmt.pct(0.05) })}</span>
        </div>

        <button
          onClick={onOpenKeyVaultModal}
          className="w-full py-1.5 px-2 rounded-lg bg-panel hover:bg-surface border border-line-strong text-xs font-bold text-ink flex items-center justify-center space-x-1.5 transition shadow-xs"
        >
          <Key className="w-3.5 h-3.5 text-brand" />
          <span>{t('wallet.keyVault')}</span>
        </button>
      </div>
    </aside>
  );
}
