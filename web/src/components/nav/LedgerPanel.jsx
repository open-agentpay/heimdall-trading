import React, { useState } from 'react';

/** 成交流水簿与 OMS 订单状态机（含原生保护单、UNKNOWN 订单查单恢复） */
export default function LedgerPanel({ trades, omsData, recoveringCid, onRecoverUnknown }) {
  const [ledgerTab, setLedgerTab] = useState('trades'); // trades | oms

  return (
    <div className="bg-surface rounded-2xl border border-line shadow-xs overflow-hidden">
      <div className="px-5 py-3 bg-panel border-b border-line flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setLedgerTab('trades')}
            className={`px-3 py-1 rounded-lg text-xs font-bold transition ${ledgerTab === 'trades' ? 'bg-surface text-brand shadow-xs border border-line' : 'text-ink-3 hover:text-ink'}`}
          >
            成交流水簿 ({trades.length})
          </button>
          <button
            onClick={() => setLedgerTab('oms')}
            className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${ledgerTab === 'oms' ? 'bg-surface text-brand shadow-xs border border-line' : 'text-ink-3 hover:text-ink'}`}
          >
            <span>OMS 订单状态机 &amp; 原生保护单 ({omsData?.summary?.total || 0})</span>
            {(omsData?.summary?.unknown_count || 0) > 0 && (
              <span className="px-1.5 py-0.2 rounded-full bg-danger-solid text-white text-xs">
                {omsData.summary.unknown_count} UNKNOWN
              </span>
            )}
          </button>
        </div>
        {omsData?.instrument_spec && (
          <span className="text-xs font-mono text-ink-3 bg-surface px-2.5 py-1 rounded-lg border border-line">
            精度规格 {omsData.instrument_spec.symbol}: tick={omsData.instrument_spec.tick_size} · step={omsData.instrument_spec.step_size} · min=${omsData.instrument_spec.min_notional_usd}
          </span>
        )}
      </div>

      {ledgerTab === 'trades' ? (
        <div className="overflow-x-auto">
          {trades.length === 0 ? (
            <div className="p-8 text-center text-xs text-ink-3">
              当前机器人暂无真实成交流水（已禁用历史假盈利兜底）。
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="bg-panel text-ink-3 font-semibold border-b border-line-soft">
                <tr>
                  <th className="py-2.5 px-4">流水单号</th>
                  <th className="py-2.5 px-4">成交时间</th>
                  <th className="py-2.5 px-4">标的</th>
                  <th className="py-2.5 px-4">方向</th>
                  <th className="py-2.5 px-4">成交均价</th>
                  <th className="py-2.5 px-4">名义价值</th>
                  <th className="py-2.5 px-4">手续费</th>
                  <th className="py-2.5 px-4 text-right">实现盈亏</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-soft font-mono text-xs">
                {trades.map((t, idx) => (
                  <tr key={idx} className="hover:bg-panel transition">
                    <td className="py-2.5 px-4 text-ink-3">{t.tradeId || t.trade_id}</td>
                    <td className="py-2.5 px-4 text-ink-2">{t.timestamp}</td>
                    <td className="py-2.5 px-4 font-bold text-ink">{t.symbol}</td>
                    <td className="py-2.5 px-4">
                      <span className={`px-2 py-0.5 rounded text-xs font-bold ${(t.side === 'BUY' || t.posSide === 'LONG') ? 'bg-success-soft text-success' : 'bg-brand-soft text-brand'}`}>
                        {t.posSide || t.side}
                      </span>
                    </td>
                    <td className="py-2.5 px-4">${Number(t.price || t.executed_price || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
                    <td className="py-2.5 px-4">${Number(t.sizeUsd || t.size_usd || 0).toFixed(2)}</td>
                    <td className="py-2.5 px-4 text-ink-3">${Number(t.feeUsd || t.fee_usd || 0).toFixed(4)}</td>
                    <td className={`py-2.5 px-4 text-right font-bold ${t.pnlColor || 'text-success'}`}>
                      {t.realizedPnl || t.realized_pnl || '$0.00'}
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
              暂无 OMS 托管订单与原生条件止损/止盈单记录。
            </div>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="bg-panel text-ink-3 font-semibold border-b border-line-soft">
                <tr>
                  <th className="py-2.5 px-4">幂等键 (clientOrderId)</th>
                  <th className="py-2.5 px-4">类型 / 保护属性</th>
                  <th className="py-2.5 px-4">方向</th>
                  <th className="py-2.5 px-4">数量 (成交/委托)</th>
                  <th className="py-2.5 px-4">委托 / 触发价</th>
                  <th className="py-2.5 px-4">状态机</th>
                  <th className="py-2.5 px-4 text-right">人工干预</th>
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
                          reduceOnly 原生保护
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 px-4">{o.side} ({o.position_side})</td>
                    <td className="py-2.5 px-4">{o.filled_qty || '0'} / {o.requested_qty}</td>
                    <td className="py-2.5 px-4">
                      {o.trigger_price ? `触发 $${o.trigger_price}` : (o.avg_fill_price ? `均价 $${o.avg_fill_price}` : (o.limit_price ? `限价 $${o.limit_price}` : '-'))}
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
                          {recoveringCid === o.client_order_id ? '查单中...' : '立即查单恢复'}
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
