import React from 'react';
import { ArrowUpRight, ArrowDownRight } from 'lucide-react';
import { numOrNull, fmtNum } from '../../data/agentRoles';

/** 六项核心量化指标卡片（数据缺失时显示 “--”） */
export default function NavMetricCards({ rawMetrics }) {
  const metrics = {
    current_nav: numOrNull(rawMetrics.current_nav ?? rawMetrics.latest_nav),
    cumulative_return_pct: numOrNull(rawMetrics.cumulative_return_pct),
    sharpe_ratio: numOrNull(rawMetrics.sharpe_ratio ?? rawMetrics.annualized_sharpe_ratio),
    max_drawdown_pct: numOrNull(rawMetrics.max_drawdown_pct),
    win_rate_pct: numOrNull(rawMetrics.win_rate_pct),
    profit_loss_ratio: numOrNull(rawMetrics.profit_loss_ratio),
    total_trades: numOrNull(rawMetrics.total_trades),
  };
  const cumRet = metrics.cumulative_return_pct;
  const sharpe = metrics.sharpe_ratio;
  const sharpeLabel = sharpe === null ? '--' : sharpe >= 2 ? '优秀 (≥2.0)' : sharpe >= 1 ? '良好 (1.0–2.0)' : sharpe > 0 ? '一般 (0–1.0)' : '偏弱 (≤0)';
  const sharpeLabelColor = sharpe === null ? 'text-ink-4' : sharpe >= 1 ? 'text-success' : sharpe > 0 ? 'text-warning' : 'text-danger';
  const pf = metrics.profit_loss_ratio;
  const pfLabel = pf === null
    ? ((metrics.total_trades ?? 0) > 0 ? '暂无亏损交易，无法计算' : '暂无已平仓交易')
    : pf > 1 ? '盈利交易总额大于亏损' : pf < 1 ? '亏损交易总额大于盈利' : '盈亏持平';
  const pfLabelColor = pf === null ? 'text-ink-4' : pf > 1 ? 'text-success' : pf < 1 ? 'text-danger' : 'text-ink-3';


  return (
    <div className="grid grid-cols-6 gap-3">
      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">单位净值 (NAV)</span>
        <div className="text-xl font-bold font-mono text-ink mt-1 flex items-baseline space-x-1">
          <span>{fmtNum(metrics.current_nav, 4)}</span>
          <span className="text-xs text-success font-sans">USD</span>
        </div>
        <span className="text-xs text-ink-4 font-mono">基准面值 1.0000</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">累计真实收益率</span>
        <div className={`text-xl font-bold font-mono mt-1 flex items-center ${cumRet === null ? 'text-ink-4' : cumRet >= 0 ? 'text-success' : 'text-danger'}`}>
          {cumRet !== null && (cumRet >= 0 ? <ArrowUpRight className="w-4 h-4 mr-0.5" /> : <ArrowDownRight className="w-4 h-4 mr-0.5" />)}
          <span>{cumRet === null ? '--' : `${cumRet >= 0 ? '+' : ''}${cumRet.toFixed(2)}%`}</span>
        </div>
        <span className="text-xs text-ink-4">消除出入金扰动</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">夏普比率 (Sharpe)</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{fmtNum(sharpe, 2)}</span>
        </div>
        <span className={`text-xs font-semibold ${sharpeLabelColor}`}>{sharpeLabel}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">历史最大回撤 (MDD)</span>
        <div className="text-xl font-bold font-mono text-danger mt-1 flex items-center">
          <ArrowDownRight className="w-4 h-4 mr-0.5" />
          <span>{metrics.max_drawdown_pct === null ? '--' : `-${metrics.max_drawdown_pct.toFixed(2)}%`}</span>
        </div>
        <span className="text-xs text-ink-4">相对历史最高净值</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">交易胜率 (Win Rate)</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{metrics.win_rate_pct === null ? '--' : `${metrics.win_rate_pct.toFixed(1)}%`}</span>
        </div>
        <span className="text-xs text-ink-3">共 {metrics.total_trades ?? '--'} 笔成交流水</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">盈亏比 (Profit Factor)</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{pf === null ? '--' : `${pf.toFixed(2)}:1`}</span>
        </div>
        <span className={`text-xs font-semibold ${pfLabelColor}`}>{pfLabel}</span>
      </div>
    </div>
  );
}
