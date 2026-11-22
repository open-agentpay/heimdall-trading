/**
 * ================================================================================
 * Heimdall 交易平台 - 零前视事件驱动回测实验室 (BacktestView)
 * ================================================================================
 *
 * 核心能力：
 * 1. 动态读取 `/api/v1/strategies/catalog`，支持纯算法三大策略（TREND_FOLLOW / GRID / FUNDING_ARB）超参微调；
 * 2. 支持「单段零前视回测」与「Walk-Forward 样本外滚动验证（OOS）」双模式；
 * 3. 展示 12 项核心量化绩效指标、NAV 单位净值曲线 + 水下回撤双轨 SVG 图表、以及逐窗口 OOS 对比表；
 * 4. 回测完成后支持「一键用此参数创建机器人」，形成“回测验证 -> 模拟盘 -> 实盘”完整闭环。
 */

import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { fmt } from '../i18n';
import {
  FlaskConical, Play, Sparkles, TrendingUp, Activity, ShieldCheck, Layers, ArrowRight
} from 'lucide-react';
import {
  getStrategiesCatalog,
  runZeroLookaheadBacktest,
  runWalkForwardBacktest,
} from '../api/endpoints';
import { MAINSTREAM_SYMBOLS } from '../data/symbols';

/** 已是百分数的值（1.23 表示 1.23%）：非负带 +，与原显示一致 */
const signedPct = (v, digits = 2) => `${Number(v) >= 0 ? '+' : ''}${fmt.pct(Number(v) / 100, digits)}`;

export default function BacktestView({ onApplyToCreateBot, onShowToast }) {
  const { t } = useTranslation('backtest');
  const [catalog, setCatalog] = useState([]);
  const [mode, setMode] = useState('single'); // single | walk_forward
  const [strategyType, setStrategyType] = useState('TREND_FOLLOW');
  const [symbol, setSymbol] = useState('ETH');
  const [timeframe, setTimeframe] = useState('15m');
  const [limitBars, setLimitBars] = useState(240);
  const [initialCapital, setInitialCapital] = useState(10000);
  const [warmupBars, setWarmupBars] = useState(30);
  const [trainBars, setTrainBars] = useState(60);
  const [testBars, setTestBars] = useState(60);
  const [stepBars, setStepBars] = useState(60);
  const [strategyConfig, setStrategyConfig] = useState({});

  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [singleResult, setSingleResult] = useState(null);
  const [wfResult, setWfResult] = useState(null);

  // 加载 StrategyRouter 策略目录
  useEffect(() => {
    getStrategiesCatalog()
      .then((res) => {
        const list = (res?.strategies || []).filter((s) => s.supports_backtest);
        setCatalog(list);
      })
      .catch(() => {});
  }, []);

  const activeMeta = catalog.find((s) => s.strategy_type === strategyType);

  // 切换策略时同步默认超参
  useEffect(() => {
    if (!activeMeta) return;
    const defaults = {};
    (activeMeta.params || []).forEach((p) => {
      defaults[p.key] = p.default;
    });
    setStrategyConfig(defaults);
  }, [strategyType, catalog.length]);

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    try {
      const payload = {
        strategy_type: strategyType,
        symbol,
        timeframe,
        limit_bars: Number(limitBars),
        initial_capital: Number(initialCapital),
        warmup_bars: Number(warmupBars),
        window_size: 60,
        strategy_config: strategyConfig,
        use_synthetic_fallback: true,
      };
      if (mode === 'single') {
        const res = await runZeroLookaheadBacktest(payload);
        setSingleResult(res);
        setWfResult(null);
        onShowToast?.(t('toast.singleDone', { source: res.data_source }), 'success');
      } else {
        const res = await runWalkForwardBacktest({
          ...payload,
          train_bars: Number(trainBars),
          test_bars: Number(testBars),
          step_bars: Number(stepBars),
        });
        setWfResult(res);
        setSingleResult(null);
        onShowToast?.(t('toast.wfDone', { count: res.walk_forward?.windows_count || 0 }), 'success');
      }
    } catch (err) {
      setError(err.message || t('toast.runError'));
      onShowToast?.(t('toast.runFailed', { message: err.message }), 'critical');
    } finally {
      setRunning(false);
    }
  };

  const report = singleResult?.report || null;
  const navCurve = report?.nav_curve || [];

  // 计算 NAV 曲线与水下回撤曲线 SVG 点集
  let navPoints = '';
  let ddPoints = '';
  let baselineY = 90;
  if (navCurve.length > 1) {
    const navs = navCurve.map((p) => Number(p.nav || 1.0));
    const minNav = Math.min(...navs, 0.995);
    const maxNav = Math.max(...navs, 1.005);
    const navRange = maxNav - minNav || 0.02;
    baselineY = 140 - ((1.0 - minNav) / navRange) * 120;
    navPoints = navCurve
      .map((p, i) => {
        const x = (i / (navCurve.length - 1)) * 600;
        const y = 140 - ((Number(p.nav || 1.0) - minNav) / navRange) * 120;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');

    const dds = navCurve.map((p) => Math.abs(Number(p.drawdown_pct || 0)));
    const maxDd = Math.max(...dds, 1.0);
    ddPoints = navCurve
      .map((p, i) => {
        const x = (i / (navCurve.length - 1)) * 600;
        const y = 10 + (Math.abs(Number(p.drawdown_pct || 0)) / maxDd) * 65;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      {/* 顶栏 */}
      <div className="h-14 bg-panel border-b border-line px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-muted border border-line-strong flex items-center justify-center text-brand">
            <FlaskConical className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-ink flex items-center space-x-2">
              <span>{t('header.title')}</span>
              <span className="text-xs font-mono bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full">
                {t('header.badge')}
              </span>
            </h1>
            <p className="text-xs text-ink-3">
              {t('header.subtitle')}
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {(singleResult || wfResult) && onApplyToCreateBot && (
            <button
              onClick={() =>
                onApplyToCreateBot({
                  strategy_type: strategyType,
                  symbol,
                  allocated_capital: Number(initialCapital),
                  strategy_config: strategyConfig,
                })
              }
              className="px-3 py-1.5 rounded-lg bg-success-solid hover:bg-success-solid-hover text-white text-xs font-bold flex items-center space-x-1.5 shadow-xs transition cursor-pointer"
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>{t('header.applyToBot')}</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            onClick={handleRun}
            disabled={running}
            className="px-4 py-1.5 rounded-lg bg-brand hover:bg-brand-hover text-white text-xs font-bold flex items-center space-x-1.5 shadow-xs transition cursor-pointer disabled:opacity-50"
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            <span>{running ? t('header.running') : t('header.run')}</span>
          </button>
        </div>
      </div>

      {/* 主体双栏 */}
      <div className="flex-1 flex overflow-hidden">
        {/* 左侧参数配置栏 */}
        <div className="w-80 bg-panel border-r border-line p-4 overflow-y-auto space-y-4 shrink-0 text-xs">
          {/* 验证模式 */}
          <div>
            <label className="block text-xs font-bold text-ink-2 mb-1.5">{t('form.mode')}</label>
            <div className="grid grid-cols-2 gap-1.5 bg-muted p-1 rounded-xl border border-line-strong">
              <button
                onClick={() => setMode('single')}
                className={`py-1.5 rounded-lg text-xs font-bold transition ${
                  mode === 'single' ? 'bg-surface text-brand shadow-xs' : 'text-ink-3'
                }`}
              >
                {t('form.modeSingle')}
              </button>
              <button
                onClick={() => setMode('walk_forward')}
                className={`py-1.5 rounded-lg text-xs font-bold transition ${
                  mode === 'walk_forward' ? 'bg-surface text-brand shadow-xs' : 'text-ink-3'
                }`}
              >
                Walk-Forward OOS
              </button>
            </div>
          </div>

          {/* 策略选择 */}
          <div>
            <label className="block text-xs font-bold text-ink-2 mb-1.5">{t('form.strategy')}</label>
            <div className="space-y-1.5">
              {[
                { id: 'TREND_FOLLOW', labelKey: 'strategies.trendFollow' },
                { id: 'GRID', labelKey: 'strategies.grid' },
                { id: 'FUNDING_ARB', labelKey: 'strategies.fundingArb' },
              ].map((s) => (
                <button
                  key={s.id}
                  onClick={() => setStrategyType(s.id)}
                  className={`w-full text-left px-3 py-2 rounded-xl border font-semibold transition ${
                    strategyType === s.id
                      ? 'bg-surface border-brand text-brand shadow-xs'
                      : 'bg-subtle border-transparent text-ink-2 hover:bg-surface'
                  }`}
                >
                  {t(s.labelKey)}
                </button>
              ))}
            </div>
            {activeMeta?.summary && (
              <p className="text-xs text-ink-3 mt-1.5 leading-relaxed">{activeMeta.summary}</p>
            )}
          </div>

          {/* 标的与周期 */}
          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className="block text-xs font-bold text-ink-2 mb-1">{t('form.symbol')}</label>
              <select
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-surface border border-line-strong rounded-lg font-mono font-bold"
              >
                {MAINSTREAM_SYMBOLS.map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol} · {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-bold text-ink-2 mb-1">{t('form.timeframe')}</label>
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-surface border border-line-strong rounded-lg font-mono font-bold"
              >
                <option value="15m">15m</option>
                <option value="1h">1h</option>
                <option value="4h">4h</option>
              </select>
            </div>
          </div>

          {/* K 线根数与资金 */}
          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className="block text-xs font-bold text-ink-2 mb-1">{t('form.limitBars')}</label>
              <input
                type="number"
                min="80"
                max="1000"
                step="20"
                value={limitBars}
                onChange={(e) => setLimitBars(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-surface border border-line-strong rounded-lg font-mono"
              />
            </div>
            <div>
              <label className="block text-xs font-bold text-ink-2 mb-1">{t('form.initialCapital')}</label>
              <input
                type="number"
                min="1000"
                step="1000"
                value={initialCapital}
                onChange={(e) => setInitialCapital(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-surface border border-line-strong rounded-lg font-mono"
              />
            </div>
          </div>

          {mode === 'walk_forward' && (
            <div className="p-3 rounded-xl bg-surface border border-line space-y-2">
              <div className="text-xs font-bold text-ink">{t('form.wfWindows')}</div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-xs text-ink-3">{t('form.trainBars')}</label>
                  <input
                    type="number"
                    min="30"
                    value={trainBars}
                    onChange={(e) => setTrainBars(e.target.value)}
                    className="w-full px-2 py-1 border border-line-strong rounded font-mono text-xs"
                  />
                </div>
                <div>
                  <label className="block text-xs text-ink-3">{t('form.testBars')}</label>
                  <input
                    type="number"
                    min="30"
                    value={testBars}
                    onChange={(e) => setTestBars(e.target.value)}
                    className="w-full px-2 py-1 border border-line-strong rounded font-mono text-xs"
                  />
                </div>
                <div>
                  <label className="block text-xs text-ink-3">{t('form.stepBars')}</label>
                  <input
                    type="number"
                    min="20"
                    value={stepBars}
                    onChange={(e) => setStepBars(e.target.value)}
                    className="w-full px-2 py-1 border border-line-strong rounded font-mono text-xs"
                  />
                </div>
              </div>
            </div>
          )}

          {/* 策略专属超参动态表单 */}
          {activeMeta?.params && activeMeta.params.length > 0 && (
            <div className="p-3 rounded-xl bg-surface border border-line space-y-2.5">
              <div className="text-xs font-bold text-ink">{t('form.params')}</div>
              {activeMeta.params.map((p) => (
                <div key={p.key} className="flex items-center justify-between gap-2">
                  <label className="text-xs text-ink-2">{p.label}</label>
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    step={p.step}
                    value={strategyConfig[p.key] ?? p.default}
                    onChange={(e) =>
                      setStrategyConfig((prev) => ({ ...prev, [p.key]: Number(e.target.value) }))
                    }
                    className="w-24 px-2 py-1 border border-line-strong rounded font-mono text-right text-xs"
                  />
                </div>
              ))}
            </div>
          )}

          <div className="p-3 rounded-xl bg-muted border border-line-strong text-xs text-ink-3 space-y-1">
            <div className="font-bold text-ink flex items-center space-x-1">
              <ShieldCheck className="w-3.5 h-3.5 text-success" />
              <span>{t('guard.title')}</span>
            </div>
            <p>{t('guard.body')}</p>
          </div>
        </div>

        {/* 右侧回测结果看板 */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {error && (
            <div className="p-3.5 rounded-xl bg-danger-soft border border-danger-line text-xs font-semibold text-danger">
              ⚠️ {error}
            </div>
          )}

          {!report && !wfResult && (
            <div className="h-full flex flex-col items-center justify-center text-center p-10 bg-surface rounded-2xl border border-dashed border-line-strong space-y-3">
              <FlaskConical className="w-10 h-10 text-brand" />
              <div className="text-sm font-bold text-ink">{t('empty.title')}</div>
              <p className="text-xs text-ink-3 max-w-md">
                {t('empty.body')}
              </p>
            </div>
          )}

          {/* 单段回测完整报告 */}
          {report && (
            <>
              <div className="flex items-center justify-between">
                <div className="text-xs font-bold text-ink flex items-center space-x-2">
                  <TrendingUp className="w-4 h-4 text-brand" />
                  <span>
                    {t('report.summary', { strategy: report.strategy_type, symbol: report.symbol, timeframe: report.timeframe, bars: report.bars_evaluated })}
                  </span>
                </div>
                <span className="text-xs font-mono px-2.5 py-0.5 rounded-full bg-surface border border-line-strong text-ink-2">
                  {t('report.dataSource', { source: singleResult.data_source })}
                </span>
              </div>

              {/* 12 项核心绩效指标矩阵 */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                  { id: 'cumReturn', label: t('metrics.cumReturn'), val: signedPct(report.cumulative_return_pct), color: report.cumulative_return_pct >= 0 ? 'text-success' : 'text-danger' },
                  { id: 'netPnl', label: t('metrics.netPnl'), val: fmt.usd(report.net_pnl_usd, 2, { signed: true }), color: report.net_pnl_usd >= 0 ? 'text-success' : 'text-danger' },
                  { id: 'maxDrawdown', label: t('metrics.maxDrawdown'), val: `-${fmt.pct(report.max_drawdown_pct / 100)}`, color: 'text-danger' },
                  { id: 'sharpe', label: t('metrics.sharpe'), val: fmt.number(report.annualized_sharpe_ratio), color: 'text-ink' },
                  { id: 'sortino', label: t('metrics.sortino'), val: fmt.number(report.annualized_sortino_ratio), color: 'text-ink' },
                  { id: 'calmar', label: t('metrics.calmar'), val: fmt.number(report.calmar_ratio), color: 'text-ink' },
                  { id: 'winRate', label: t('metrics.winRate'), val: `${fmt.pct(report.win_rate_pct / 100, 1)} (${report.closing_trades}/${report.total_trades})`, color: 'text-ink' },
                  { id: 'profitFactor', label: t('metrics.profitFactor'), val: fmt.number(report.profit_factor), color: 'text-ink' },
                  { id: 'expectancy', label: t('metrics.expectancy'), val: fmt.usd(report.expectancy_usd), color: report.expectancy_usd >= 0 ? 'text-success' : 'text-danger' },
                  { id: 'maxLosses', label: t('metrics.maxLosses'), val: t('metrics.timesValue', { count: report.max_consecutive_losses }), color: 'text-warning' },
                  { id: 'fees', label: t('metrics.fees'), val: fmt.usd(report.total_fees_usd), color: 'text-ink-3' },
                  { id: 'fundingPnl', label: t('metrics.fundingPnl'), val: fmt.usd(report.total_funding_pnl_usd, 2, { signed: true }), color: report.total_funding_pnl_usd >= 0 ? 'text-success' : 'text-danger' },
                ].map((m) => (
                  <div key={m.id} className="bg-surface p-3.5 rounded-xl border border-line shadow-xs">
                    <div className="text-xs text-ink-3 font-semibold">{m.label}</div>
                    <div className={`text-base font-bold font-mono mt-1 ${m.color}`}>{m.val}</div>
                  </div>
                ))}
              </div>

              {/* 双轨 SVG 图表：净值曲线 + 水下回撤 */}
              <div className="bg-surface p-5 rounded-2xl border border-line shadow-xs space-y-4">
                <div>
                  <div className="text-xs font-bold text-ink mb-2 flex items-center space-x-1.5">
                    <Activity className="w-4 h-4 text-brand" />
                    <span>{t('chart.nav')}</span>
                  </div>
                  <div className="w-full h-40 bg-panel rounded-xl border border-line-soft p-2">
                    <svg viewBox="0 0 600 150" className="w-full h-full overflow-visible">
                      <line x1="0" y1={baselineY} x2="600" y2={baselineY} style={{ stroke: 'rgb(var(--c-line-strong))' }} strokeDasharray="4 4" strokeWidth="1.2" />
                      {navPoints && (
                        <polyline fill="none" style={{ stroke: 'rgb(var(--c-brand))' }} strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" points={navPoints} />
                      )}
                    </svg>
                  </div>
                </div>

                <div>
                  <div className="text-xs font-bold text-ink mb-2">{t('chart.drawdown')}</div>
                  <div className="w-full h-24 bg-panel rounded-xl border border-line-soft p-2">
                    <svg viewBox="0 0 600 85" className="w-full h-full overflow-visible">
                      <line x1="0" y1="10" x2="600" y2="10" style={{ stroke: 'rgb(var(--c-line-strong))' }} strokeWidth="1" />
                      {ddPoints && (
                        <polyline fill="none" style={{ stroke: 'rgb(var(--c-danger))' }} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" points={ddPoints} />
                      )}
                    </svg>
                  </div>
                </div>
              </div>
            </>
          )}

          {/* Walk-Forward 样本外滚动验证结果 */}
          {wfResult?.walk_forward && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                <div className="bg-surface p-4 rounded-xl border border-line">
                  <div className="text-xs text-ink-3">{t('wf.windowsCount')}</div>
                  <div className="text-xl font-bold font-mono text-ink mt-1">{t('wf.windowsValue', { count: wfResult.walk_forward.windows_count })}</div>
                </div>
                <div className="bg-surface p-4 rounded-xl border border-line">
                  <div className="text-xs text-ink-3">{t('wf.avgReturn')}</div>
                  <div className={`text-xl font-bold font-mono mt-1 ${(wfResult.walk_forward.avg_oos_return_pct || 0) >= 0 ? 'text-success' : 'text-danger'}`}>
                    {signedPct(wfResult.walk_forward.avg_oos_return_pct || 0)}
                  </div>
                </div>
                <div className="bg-surface p-4 rounded-xl border border-line">
                  <div className="text-xs text-ink-3">{t('wf.avgSharpe')}</div>
                  <div className="text-xl font-bold font-mono text-ink mt-1">{fmt.number(wfResult.walk_forward.avg_oos_sharpe || 0)}</div>
                </div>
                <div className="bg-surface p-4 rounded-xl border border-line">
                  <div className="text-xs text-ink-3">{t('wf.worstDrawdown')}</div>
                  <div className="text-xl font-bold font-mono text-danger mt-1">-{fmt.pct((wfResult.walk_forward.worst_oos_drawdown_pct || 0) / 100)}</div>
                </div>
              </div>

              <div className="bg-surface rounded-2xl border border-line overflow-hidden">
                <div className="px-5 py-3 bg-panel border-b border-line flex items-center space-x-2">
                  <Layers className="w-4 h-4 text-brand" />
                  <h3 className="text-xs font-bold text-ink">{t('wf.tableTitle')}</h3>
                </div>
                <table className="w-full text-left text-xs">
                  <thead className="bg-panel text-ink-3 font-semibold border-b border-line-soft">
                    <tr>
                      <th className="py-2.5 px-4">{t('wf.colWindow')}</th>
                      <th className="py-2.5 px-4">{t('wf.colBars')}</th>
                      <th className="py-2.5 px-4">{t('wf.colReturn')}</th>
                      <th className="py-2.5 px-4">{t('wf.colSharpe')}</th>
                      <th className="py-2.5 px-4">{t('wf.colDrawdown')}</th>
                      <th className="py-2.5 px-4">{t('wf.colTrades')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line-soft font-mono text-xs">
                    {(wfResult.walk_forward.oos_reports || []).map((r, i) => (
                      <tr key={i} className="hover:bg-panel">
                        <td className="py-2.5 px-4 font-bold">{t('wf.windowLabel', { index: i + 1 })}</td>
                        <td className="py-2.5 px-4">{t('wf.barsValue', { count: r.bars_evaluated })}</td>
                        <td className={`py-2.5 px-4 font-bold ${r.cumulative_return_pct >= 0 ? 'text-success' : 'text-danger'}`}>
                          {signedPct(r.cumulative_return_pct)}
                        </td>
                        <td className="py-2.5 px-4">{fmt.number(r.annualized_sharpe_ratio)}</td>
                        <td className="py-2.5 px-4 text-danger">-{fmt.pct(Number(r.max_drawdown_pct) / 100)}</td>
                        <td className="py-2.5 px-4">{t('wf.tradesValue', { count: r.total_trades, winRate: fmt.pct(Number(r.win_rate_pct) / 100, 1) })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
