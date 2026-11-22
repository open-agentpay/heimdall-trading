import React from 'react';
import { fmtUsd } from '../../data/agentRoles';

/** 账户资金状态与当前持仓 */
export default function AccountSummary({ accountKnown, balance, activePosition }) {
  return (
    <div className="bg-surface p-4 rounded-2xl border border-line shadow-xs flex flex-wrap items-center justify-between gap-4">
      <div className="flex flex-wrap items-center gap-6">
        <div>
          <span className="text-xs font-semibold text-ink-3 flex items-center space-x-1">
            <span>💼</span>
            <span>账户总资产 (Equity)</span>
          </span>
          <div className="text-base font-bold font-mono text-ink mt-0.5">
            {fmtUsd(accountKnown ? balance.total_equity : null)}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-success flex items-center space-x-1">
            <span>🟢</span>
            <span>空闲可用现金 (Free Cash)</span>
          </span>
          <div className="text-base font-bold font-mono text-success mt-0.5">
            {fmtUsd(accountKnown ? balance.cash_balance : null)}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-warning-strong flex items-center space-x-1">
            <span>🔒</span>
            <span>占用保证金 (Margin Used)</span>
          </span>
          <div className="text-base font-bold font-mono text-warning-strong mt-0.5">
            {accountKnown ? `$${Number(balance.margin_used || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '--'}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-ink-3 flex items-center space-x-1">
            <span>📈</span>
            <span>持仓浮动盈亏 (Unrealized)</span>
          </span>
          <div className={`text-base font-bold font-mono mt-0.5 ${!accountKnown ? 'text-ink-4' : Number(balance.unrealized_pnl || 0) >= 0 ? 'text-success' : 'text-danger'}`}>
            {!accountKnown ? '--' : `${Number(balance.unrealized_pnl || 0) >= 0 ? '+' : '-'}$${Math.abs(Number(balance.unrealized_pnl || 0)).toFixed(2)}`}
          </div>
        </div>
      </div>

      {/* 右侧当前持仓头寸徽标 */}
      <div className="flex items-center space-x-2 bg-panel px-3.5 py-2 rounded-xl border border-line-soft">
        <span className="text-xs text-ink-3 font-medium">当前持仓头寸:</span>
        {activePosition ? (
          <span className="text-xs font-bold font-mono flex items-center space-x-1.5 text-ink">
            <span className={`px-2 py-0.5 rounded text-xs font-bold ${activePosition.side === 'LONG' ? 'bg-success-soft text-success' : 'bg-brand-soft text-danger'}`}>
              {activePosition.side} {activePosition.leverage}x
            </span>
            <span>{Number(activePosition.quantity).toFixed(4)} {activePosition.symbol}</span>
            <span className="text-xs text-ink-3">@ ${Number(activePosition.entry_price).toFixed(2)}</span>
          </span>
        ) : accountKnown ? (
          <span className="text-xs font-bold font-mono text-ink-3 flex items-center space-x-1.5">
            <span className="w-2 h-2 rounded-full bg-success-solid" />
            <span>当前空仓 (FLAT)</span>
          </span>
        ) : (
          <span className="text-xs font-bold font-mono text-ink-4">--（账户数据不可用）</span>
        )}
      </div>
    </div>
  );
}
