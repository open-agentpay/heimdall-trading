import React from 'react';
import { useTranslation } from 'react-i18next';
import { fmt } from '../../i18n';
import { fmtUsd } from '../../data/agentRoles';

/** 账户资金状态与当前持仓 */
export default function AccountSummary({ accountKnown, balance, activePosition }) {
  const { t } = useTranslation('nav');
  return (
    <div className="bg-surface p-4 rounded-2xl border border-line shadow-xs flex flex-wrap items-center justify-between gap-4">
      <div className="flex flex-wrap items-center gap-6">
        <div>
          <span className="text-xs font-semibold text-ink-3 flex items-center space-x-1">
            <span>💼</span>
            <span>{t('account.equity')}</span>
          </span>
          <div className="text-base font-bold font-mono text-ink mt-0.5">
            {fmtUsd(accountKnown ? balance.total_equity : null)}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-success flex items-center space-x-1">
            <span>🟢</span>
            <span>{t('account.freeCash')}</span>
          </span>
          <div className="text-base font-bold font-mono text-success mt-0.5">
            {fmtUsd(accountKnown ? balance.cash_balance : null)}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-warning-strong flex items-center space-x-1">
            <span>🔒</span>
            <span>{t('account.marginUsed')}</span>
          </span>
          <div className="text-base font-bold font-mono text-warning-strong mt-0.5">
            {accountKnown ? fmt.usd(balance.margin_used || 0) : '--'}
          </div>
        </div>

        <div className="h-8 w-px bg-muted hidden sm:block" />

        <div>
          <span className="text-xs font-semibold text-ink-3 flex items-center space-x-1">
            <span>📈</span>
            <span>{t('account.unrealized')}</span>
          </span>
          <div className={`text-base font-bold font-mono mt-0.5 ${!accountKnown ? 'text-ink-4' : Number(balance.unrealized_pnl || 0) >= 0 ? 'text-success' : 'text-danger'}`}>
            {!accountKnown ? '--' : fmt.usd(balance.unrealized_pnl || 0, 2, { signed: true })}
          </div>
        </div>
      </div>

      {/* 右侧当前持仓头寸徽标 */}
      <div className="flex items-center space-x-2 bg-panel px-3.5 py-2 rounded-xl border border-line-soft">
        <span className="text-xs text-ink-3 font-medium">{t('account.position')}</span>
        {activePosition ? (
          <span className="text-xs font-bold font-mono flex items-center space-x-1.5 text-ink">
            <span className={`px-2 py-0.5 rounded text-xs font-bold ${activePosition.side === 'LONG' ? 'bg-success-soft text-success' : 'bg-brand-soft text-danger'}`}>
              {activePosition.side} {activePosition.leverage}x
            </span>
            <span>{fmt.number(activePosition.quantity, 4)} {activePosition.symbol}</span>
            <span className="text-xs text-ink-3">@ {fmt.price(activePosition.entry_price)}</span>
          </span>
        ) : accountKnown ? (
          <span className="text-xs font-bold font-mono text-ink-3 flex items-center space-x-1.5">
            <span className="w-2 h-2 rounded-full bg-success-solid" />
            <span>{t('account.flat')}</span>
          </span>
        ) : (
          <span className="text-xs font-bold font-mono text-ink-4">{t('account.unavailable')}</span>
        )}
      </div>
    </div>
  );
}
