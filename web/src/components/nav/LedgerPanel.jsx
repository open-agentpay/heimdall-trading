import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { fmt } from '../../i18n';

/** 成交流水簿与 OMS 订单状态机（含原生保护单、UNKNOWN 订单查单恢复） */
export default function LedgerPanel({ trades, omsData, recoveringCid, onRecoverUnknown }) {
  const { t } = useTranslation('nav');
  const [ledgerTab, setLedgerTab] = useState('trades'); // trades | oms

  return (
    <div className="bg-surface rounded-2xl border border-line shadow-xs overflow-hidden">
      <div className="px-5 py-3 bg-panel border-b border-line flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setLedgerTab('trades')}
            className={`px-3 py-1 rounded-lg text-xs font-bold transition ${ledgerTab === 'trades' ? 'bg-surface text-brand shadow-xs border border-line' : 'text-ink-3 hover:text-ink'}`}
          >
            {t('ledger.tradesTab', { count: trades.length })}
          </button>
          <button
            onClick={() => setLedgerTab('oms')}
            className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${ledgerTab === 'oms' ? 'bg-surface text-brand shadow-xs border border-line' : 'text-ink-3 hover:text-ink'}`}
          >
            <span>{t('ledger.omsTab', { count: omsData?.summary?.total || 0 })}</span>
            {(omsData?.summary?.unknown_count || 0) > 0 && (
              <span className="px-1.5 py-0.2 rounded-full bg-danger-solid text-white text-xs">
                {omsData.summary.unknown_count} UNKNOWN
              </span>
            )}
          </button>
        </div>
        {omsData?.instrument_spec && (
          <span className="text-xs font-mono text-ink-3 bg-surface px-2.5 py-1 rounded-lg border border-line">
            {t('ledger.spec', {
                  symbol: omsData.instrument_spec.symbol,
                  tick: omsData.instrument_spec.tick_size,
                  step: omsData.instrument_spec.step_size,
                  min: omsData.instrument_spec.min_notional_usd,
                })}
                {omsData.instrument_spec.source && (
                  <span className={omsData.instrument_spec.source === 'default' ? 'text-danger' : ''}>
                    {' · '}
                    {{
                      exchange: t('ledger.specSource.exchange'),
                      builtin: t('ledger.specSource.builtin'),
                      default: t('ledger.specSource.default'),
                    }[omsData.instrument_spec.source] || omsData.instrument_spec.source}
                  </span>
                )}
          </span>
        )}
      </div>

      {ledgerTab === 'trades' ? (
        <div className="overflow-x-auto">
          {trades.length === 0 ? (
            <div className="p-8 text-center text-xs text-ink-3">
              {t('ledger.noTrades')}
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="bg-panel text-ink-3 font-semibold border-b border-line-soft">
                <tr>
                  <th className="py-2.5 px-4">{t('ledger.cols.tradeId')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.time')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.symbol')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.side')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.avgPrice')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.notional')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.fee')}</th>
                  <th className="py-2.5 px-4 text-right">{t('ledger.cols.pnl')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-soft font-mono text-xs">
                {trades.map((tr, idx) => (
                  <tr key={idx} className="hover:bg-panel transition">
                    <td className="py-2.5 px-4 text-ink-3">{tr.tradeId || tr.trade_id}</td>
                    <td className="py-2.5 px-4 text-ink-2" title={tr.timestamp || ''}>{fmt.dateTime(tr.timestamp)}</td>
                    <td className="py-2.5 px-4 font-bold text-ink">{tr.symbol}</td>
                    <td className="py-2.5 px-4">
                      <span className={`px-2 py-0.5 rounded text-xs font-bold ${(tr.side === 'BUY' || tr.posSide === 'LONG') ? 'bg-success-soft text-success' : 'bg-brand-soft text-brand'}`}>
                        {tr.posSide || tr.side}
                      </span>
                    </td>
                    <td className="py-2.5 px-4">{fmt.price(tr.price || tr.executed_price || 0)}</td>
                    <td className="py-2.5 px-4">{fmt.usd(tr.sizeUsd || tr.size_usd || 0)}</td>
                    <td className="py-2.5 px-4 text-ink-3">{fmt.usd(tr.feeUsd || tr.fee_usd || 0, 4)}</td>
                    <td className={`py-2.5 px-4 text-right font-bold ${tr.pnlColor || 'text-success'}`}>
                      {tr.realizedPnl || tr.realized_pnl || '$0.00'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="overflow-x-auto">
          {(!omsData?.orders || omsData.orders.length === 0) ? (
            <div className="p-8 text-center text-xs text-ink-3">
              {t('ledger.noOrders')}
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="bg-panel text-ink-3 font-semibold border-b border-line-soft">
                <tr>
                  <th className="py-2.5 px-4">{t('ledger.cols.clientOrderId')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.type')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.side')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.qty')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.price')}</th>
                  <th className="py-2.5 px-4">{t('ledger.cols.state')}</th>
                  <th className="py-2.5 px-4 text-right">{t('ledger.cols.action')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-soft font-mono text-xs">
                {omsData.orders.map((o) => (
                  <tr key={o.client_order_id} className="hover:bg-panel transition">
                    <td className="py-2.5 px-4 text-ink-2">{o.client_order_id}</td>
                    <td className="py-2.5 px-4">
                      <span className="font-bold text-ink">{o.order_type}</span>
                      {Boolean(o.reduce_only) && (
                        <span className="ml-1.5 px-1.5 py-0.2 rounded bg-success-soft text-success text-xs font-sans font-bold">
                          {t('ledger.reduceOnly')}
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 px-4">{o.side} ({o.position_side})</td>
                    <td className="py-2.5 px-4">{o.filled_qty || '0'} / {o.requested_qty}</td>
                    <td className="py-2.5 px-4">
                      {o.trigger_price ? t('ledger.trigger', { price: o.trigger_price }) : (o.avg_fill_price ? t('ledger.avg', { price: o.avg_fill_price }) : (o.limit_price ? t('ledger.limit', { price: o.limit_price }) : '-'))}
                    </td>
                    <td className="py-2.5 px-4">
                      <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${
                        o.state === 'FILLED' ? 'bg-success-soft text-success' :
                        o.state === 'UNKNOWN' ? 'bg-danger-soft text-danger' :
                        o.state === 'REJECTED' ? 'bg-danger-soft text-danger' :
                        'bg-muted text-ink-2'
                      }`}>
                        {o.state}
                      </span>
                    </td>
                    <td className="py-2.5 px-4 text-right">
                      {o.state === 'UNKNOWN' ? (
                        <button
                          onClick={() => onRecoverUnknown(o.client_order_id)}
                          disabled={recoveringCid === o.client_order_id}
                          className="px-2.5 py-1 rounded bg-brand hover:bg-brand-hover text-white text-xs font-sans font-bold"
                        >
                          {recoveringCid === o.client_order_id ? t('ledger.recovering') : t('ledger.recover')}
                        </button>
                      ) : (
                        <span className="text-ink-4 text-xs">{o.exchange_order_id || '-'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
