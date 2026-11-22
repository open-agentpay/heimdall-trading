import React from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight, ArrowDownRight } from 'lucide-react';
import { numOrNull, fmtNum } from '../../data/agentRoles';

/** 六项核心量化指标卡片（数据缺失时显示 “--”） */
export default function NavMetricCards({ rawMetrics }) {
  const { t } = useTranslation('nav');
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
  const sharpeLabel = sharpe === null ? '--' : sharpe >= 2 ? t('metrics.sharpeLevel.excellent') : sharpe >= 1 ? t('metrics.sharpeLevel.good') : sharpe > 0 ? t('metrics.sharpeLevel.fair') : t('metrics.sharpeLevel.weak');
  const sharpeLabelColor = sharpe === null ? 'text-ink-4' : sharpe >= 1 ? 'text-success' : sharpe > 0 ? 'text-warning' : 'text-danger';
  const pf = metrics.profit_loss_ratio;
  const pfLabel = pf === null
    ? ((metrics.total_trades ?? 0) > 0 ? t('metrics.pfLevel.noLoss') : t('metrics.pfLevel.noClosed'))
    : pf > 1 ? t('metrics.pfLevel.gt') : pf < 1 ? t('metrics.pfLevel.lt') : t('metrics.pfLevel.eq');
  const pfLabelColor = pf === null ? 'text-ink-4' : pf > 1 ? 'text-success' : pf < 1 ? 'text-danger' : 'text-ink-3';


  return (
    <div className="grid grid-cols-6 gap-3">
      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.nav')}</span>
        <div className="text-xl font-bold font-mono text-ink mt-1 flex items-baseline space-x-1">
          <span>{fmtNum(metrics.current_nav, 4)}</span>
          <span className="text-xs text-success font-sans">USD</span>
        </div>
        <span className="text-xs text-ink-4 font-mono">{t('metrics.navBase')}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.cumReturn')}</span>
        <div className={`text-xl font-bold font-mono mt-1 flex items-center ${cumRet === null ? 'text-ink-4' : cumRet >= 0 ? 'text-success' : 'text-danger'}`}>
          {cumRet !== null && (cumRet >= 0 ? <ArrowUpRight className="w-4 h-4 mr-0.5" /> : <ArrowDownRight className="w-4 h-4 mr-0.5" />)}
          <span>{cumRet === null ? '--' : `${cumRet >= 0 ? '+' : ''}${cumRet.toFixed(2)}%`}</span>
        </div>
        <span className="text-xs text-ink-4">{t('metrics.cumReturnHint')}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.sharpe')}</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{fmtNum(sharpe, 2)}</span>
        </div>
        <span className={`text-xs font-semibold ${sharpeLabelColor}`}>{sharpeLabel}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.maxDrawdown')}</span>
        <div className="text-xl font-bold font-mono text-danger mt-1 flex items-center">
          <ArrowDownRight className="w-4 h-4 mr-0.5" />
          <span>{metrics.max_drawdown_pct === null ? '--' : `-${metrics.max_drawdown_pct.toFixed(2)}%`}</span>
        </div>
        <span className="text-xs text-ink-4">{t('metrics.maxDrawdownHint')}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.winRate')}</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{metrics.win_rate_pct === null ? '--' : `${metrics.win_rate_pct.toFixed(1)}%`}</span>
        </div>
        <span className="text-xs text-ink-3">{t('metrics.tradeCount', { count: metrics.total_trades ?? '--' })}</span>
      </div>

      <div className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
        <span className="text-xs font-semibold text-ink-3">{t('metrics.profitFactor')}</span>
        <div className="text-xl font-bold font-mono text-ink mt-1">
          <span>{pf === null ? '--' : `${pf.toFixed(2)}:1`}</span>
        </div>
        <span className={`text-xs font-semibold ${pfLabelColor}`}>{pfLabel}</span>
      </div>
    </div>
  );
}
