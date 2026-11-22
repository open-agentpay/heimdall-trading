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
import {
  FlaskConical, Play, Sparkles, TrendingUp, Activity, ShieldCheck, Layers, ArrowRight
} from 'lucide-react';
import {
  getStrategiesCatalog,
  runZeroLookaheadBacktest,
  runWalkForwardBacktest,
} from '../api/endpoints';
import { MAINSTREAM_SYMBOLS } from '../data/symbols';

export default function BacktestView({ onApplyToCreateBot, onShowToast }) {
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
        onShowToast?.(`🧪 零前视回测完成 (${res.data_source})`, 'success');
      } else {
        const res = await runWalkForwardBacktest({
          ...payload,
          train_bars: Number(trainBars),
          test_bars: Number(testBars),
          step_bars: Number(stepBars),
        });
        setWfResult(res);
        setSingleResult(null);
        onShowToast?.(`🧪 Walk-Forward 样本外滚动验证完成 (${res.walk_forward?.windows_count || 0} 个窗口)`, 'success');
      }
    } catch (err) {
      setError(err.message || '回测执行失败');
      onShowToast?.(`回测失败: ${err.message}`, 'critical');
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
    <div className="flex-1 flex flex-col bg-[#F6F2EB] overflow-hidden">
      {/* 顶栏 */}
      <div className="h-14 bg-[#FAF7F2] border-b border-[#E6DFD5] px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-[#EFE9DF] border border-[#DFD6C7] flex items-center justify-center text-[#DE6438]">
            <FlaskConical className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-[#2E2823] flex items-center space-x-2">
              <span>零前视事件驱动回测实验室 (Zero-Lookahead Backtest)</span>
              <span className="text-xs font-mono bg-[#EBF5EE] text-[#3D784E] border border-[#CDE3D2] px-2 py-0.5 rounded-full">
                t 决策 · t+1 撮合
              </span>
            </h1>
            <p className="text-xs text-[#7E7469]">
              100% 复用实盘 FactorEngine、StrategyRouter、HardRiskGuard 与 NAVEngine，杜绝未来函数
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
              className="px-3 py-1.5 rounded-lg bg-[#3D784E] hover:bg-[#316340] text-white text-xs font-bold flex items-center space-x-1.5 shadow-xs transition cursor-pointer"
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>一键用此参数创建 Bot</span>
              <ArrowRight className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            onClick={handleRun}
            disabled={running}
            className="px-4 py-1.5 rounded-lg bg-[#DE6438] hover:bg-[#C5532A] text-white text-xs font-bold flex items-center space-x-1.5 shadow-xs transition cursor-pointer disabled:opacity-50"
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            <span>{running ? '回测计算中...' : '运行零前视回测'}</span>
          </button>
        </div>
      </div>

      {/* 主体双栏 */}
      <div className="flex-1 flex overflow-hidden">
        {/* 左侧参数配置栏 */}
        <div className="w-80 bg-[#FAF7F2] border-r border-[#E6DFD5] p-4 overflow-y-auto space-y-4 shrink-0 text-xs">
          {/* 验证模式 */}
          <div>
            <label className="block text-xs font-bold text-[#665E54] mb-1.5">回测验证模式</label>
            <div className="grid grid-cols-2 gap-1.5 bg-[#EFE9DF] p-1 rounded-xl border border-[#DFD6C7]">
              <button
                onClick={() => setMode('single')}
                className={`py-1.5 rounded-lg text-xs font-bold transition ${
                  mode === 'single' ? 'bg-white text-[#DE6438] shadow-xs' : 'text-[#7E7469]'
                }`}
              >
                单段回测
              </button>
              <button
                onClick={() => setMode('walk_forward')}
                className={`py-1.5 rounded-lg text-xs font-bold transition ${
                  mode === 'walk_forward' ? 'bg-white text-[#DE6438] shadow-xs' : 'text-[#7E7469]'
                }`}
              >
                Walk-Forward OOS
              </button>
            </div>
          </div>

          {/* 策略选择 */}
          <div>
            <label className="block text-xs font-bold text-[#665E54] mb-1.5">纯算法策略 (StrategyRouter)</label>
            <div className="space-y-1.5">
              {[
                { id: 'TREND_FOLLOW', label: '📈 趋势突破 (TREND_FOLLOW)' },
                { id: 'GRID', label: '🔲 动态网格 (GRID)' },
                { id: 'FUNDING_ARB', label: '⚖️ 资金费率套利 (FUNDING_ARB)' },
              ].map((s) => (
                <button
                  key={s.id}
                  onClick={() => setStrategyType(s.id)}
                  className={`w-full text-left px-3 py-2 rounded-xl border font-semibold transition ${
                    strategyType === s.id
                      ? 'bg-white border-[#DE6438] text-[#DE6438] shadow-xs'
                      : 'bg-[#F4EFE6] border-transparent text-[#5A5046] hover:bg-white'
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
            {activeMeta?.summary && (
              <p className="text-[10.5px] text-[#7E7469] mt-1.5 leading-relaxed">{activeMeta.summary}</p>
            )}
          </div>

          {/* 标的与周期 */}
          <div className="grid grid-cols-2 gap-2.5">
            <div>
              <label className="block text-xs font-bold text-[#665E54] mb-1">标的</label>
              <select
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-white border border-[#D8CEBE] rounded-lg font-mono font-bold"
              >
                {MAINSTREAM_SYMBOLS.map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol} · {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-bold text-[#665E54] mb-1">周期</label>
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-white border border-[#D8CEBE] rounded-lg font-mono font-bold"
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
              <label className="block text-xs font-bold text-[#665E54] mb-1">K 线根数</label>
              <input
                type="number"
                min="80"
                max="1000"
                step="20"
                value={limitBars}
                onChange={(e) => setLimitBars(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-white border border-[#D8CEBE] rounded-lg font-mono"
              />
            </div>
            <div>
              <label className="block text-xs font-bold text-[#665E54] mb-1">初始资金 ($)</label>
              <input
                type="number"
                min="1000"
                step="1000"
                value={initialCapital}
                onChange={(e) => setInitialCapital(e.target.value)}
                className="w-full px-2.5 py-1.5 bg-white border border-[#D8CEBE] rounded-lg font-mono"
              />
            </div>
          </div>

          {mode === 'walk_forward' && (
            <div className="p-3 rounded-xl bg-white border border-[#E6DFD5] space-y-2">
              <div className="text-xs font-bold text-[#2E2823]">Walk-Forward 窗口切分</div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="block text-xs text-[#7E7469]">观察段</label>
                  <input
                    type="number"
                    min="30"
                    value={trainBars}
                    onChange={(e) => setTrainBars(e.target.value)}
                    className="w-full px-2 py-1 border border-[#D8CEBE] rounded font-mono text-xs"
                  />
                </div>
                <div>
                  <label className="block text-xs text-[#7E7469]">OOS测试</label>
                  <input
                    type="number"
                    min="30"
                    value={testBars}
                    onChange={(e) => setTestBars(e.target.value)}
                    className="w-full px-2 py-1 border border-[#D8CEBE] rounded font-mono text-xs"
                  />
                </div>
                <div>
                  <label className="block text-xs text-[#7E7469]">步长</label>
                  <input
                    type="number"
                    min="20"
                    value={stepBars}
                    onChange={(e) => setStepBars(e.target.value)}
                    className="w-full px-2 py-1 border border-[#D8CEBE] rounded font-mono text-xs"
                  />
                </div>
              </div>
            </div>
          )}

          {/* 策略专属超参动态表单 */}
          {activeMeta?.params && activeMeta.params.length > 0 && (
            <div className="p-3 rounded-xl bg-white border border-[#E6DFD5] space-y-2.5">
              <div className="text-xs font-bold text-[#2E2823]">策略超参微调</div>
              {activeMeta.params.map((p) => (
                <div key={p.key} className="flex items-center justify-between gap-2">
                  <label className="text-xs text-[#5A5046]">{p.label}</label>
                  <input
                    type="number"
                    min={p.min}
                    max={p.max}
                    step={p.step}
                    value={strategyConfig[p.key] ?? p.default}
                    onChange={(e) =>
                      setStrategyConfig((prev) => ({ ...prev, [p.key]: Number(e.target.value) }))
                    }
                    className="w-24 px-2 py-1 border border-[#D8CEBE] rounded font-mono text-right text-xs"
                  />
                </div>
              ))}
            </div>
          )}

          <div className="p-3 rounded-xl bg-[#EFE9DF] border border-[#DDD5C7] text-[10.5px] text-[#6E6458] space-y-1">
            <div className="font-bold text-[#2E2823] flex items-center space-x-1">
              <ShieldCheck className="w-3.5 h-3.5 text-[#3D784E]" />
              <span>零前视红线保障</span>
            </div>
            <p>禁止 LLM 参与历史回测；每根 K 线严格在闭合后评估，撮合与盘中触损全部延后至下一根 bar 结算。</p>
          </div>
        </div>

        {/* 右侧回测结果看板 */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {error && (
            <div className="p-3.5 rounded-xl bg-[#FDECEC] border border-[#F7CDCD] text-xs font-semibold text-[#C24D4D]">
              ⚠️ {error}
            </div>
          )}

          {!report && !wfResult && (
            <div className="h-full flex flex-col items-center justify-center text-center p-10 bg-white rounded-2xl border border-dashed border-[#D8CEBE] space-y-3">
              <FlaskConical className="w-10 h-10 text-[#DE6438]" />
              <div className="text-sm font-bold text-[#2E2823]">选择策略与周期，点击右上角「运行零前视回测」</div>
              <p className="text-xs text-[#7E7469] max-w-md">
                系统将自动拉取最新闭合历史 K 线（离线时自动降级确定性合成数据），跑完后可一键带参部署为实盘或模拟盘机器人。
              </p>
            </div>
          )}

          {/* 单段回测完整报告 */}
          {report && (
            <>
              <div className="flex items-center justify-between">
                <div className="text-xs font-bold text-[#2E2823] flex items-center space-x-2">
                  <TrendingUp className="w-4 h-4 text-[#DE6438]" />
                  <span>
                    {report.strategy_type} · {report.symbol} ({report.timeframe}) · 评估 {report.bars_evaluated} 根 K 线
                  </span>
                </div>
                <span className="text-xs font-mono px-2.5 py-0.5 rounded-full bg-white border border-[#DFD6C7] text-[#5A5046]">
                  数据源: {singleResult.data_source}
                </span>
              </div>

              {/* 12 项核心绩效指标矩阵 */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {[
                  { label: '累计收益率', val: `${report.cumulative_return_pct >= 0 ? '+' : ''}${report.cumulative_return_pct.toFixed(2)}%`, color: report.cumulative_return_pct >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]' },
                  { label: '净盈亏 (USD)', val: `${report.net_pnl_usd >= 0 ? '+' : ''}$${report.net_pnl_usd.toFixed(2)}`, color: report.net_pnl_usd >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]' },
                  { label: '最大回撤 (MDD)', val: `-${report.max_drawdown_pct.toFixed(2)}%`, color: 'text-[#C24D4D]' },
                  { label: '年化夏普 (Sharpe)', val: report.annualized_sharpe_ratio.toFixed(2), color: 'text-[#2E2823]' },
                  { label: '索提诺比率 (Sortino)', val: report.annualized_sortino_ratio.toFixed(2), color: 'text-[#2E2823]' },
                  { label: '卡玛比率 (Calmar)', val: report.calmar_ratio.toFixed(2), color: 'text-[#2E2823]' },
                  { label: '平仓胜率', val: `${report.win_rate_pct.toFixed(1)}% (${report.closing_trades}/${report.total_trades})`, color: 'text-[#2E2823]' },
                  { label: '盈亏比 (PF)', val: report.profit_factor.toFixed(2), color: 'text-[#2E2823]' },
                  { label: '单笔期望值', val: `$${report.expectancy_usd.toFixed(2)}`, color: report.expectancy_usd >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]' },
                  { label: '最大连亏次数', val: `${report.max_consecutive_losses} 次`, color: 'text-[#C28B38]' },
                  { label: '累计手续费', val: `$${report.total_fees_usd.toFixed(2)}`, color: 'text-[#7E7469]' },
                  { label: '累计资金费损益', val: `${report.total_funding_pnl_usd >= 0 ? '+' : ''}$${report.total_funding_pnl_usd.toFixed(2)}`, color: report.total_funding_pnl_usd >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]' },
                ].map((m) => (
                  <div key={m.label} className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
                    <div className="text-[10.5px] text-[#7E7469] font-semibold">{m.label}</div>
                    <div className={`text-base font-bold font-mono mt-1 ${m.color}`}>{m.val}</div>
                  </div>
                ))}
              </div>

              {/* 双轨 SVG 图表：净值曲线 + 水下回撤 */}
              <div className="bg-white p-5 rounded-2xl border border-[#E6DFD5] shadow-xs space-y-4">
                <div>
                  <div className="text-xs font-bold text-[#2E2823] mb-2 flex items-center space-x-1.5">
                    <Activity className="w-4 h-4 text-[#DE6438]" />
                    <span>单位净值曲线 (NAV Curve)</span>
                  </div>
                  <div className="w-full h-40 bg-[#FAF8F5] rounded-xl border border-[#F0EBE1] p-2">
                    <svg viewBox="0 0 600 150" className="w-full h-full overflow-visible">
                      <line x1="0" y1={baselineY} x2="600" y2={baselineY} stroke="#D8CEBE" strokeDasharray="4 4" strokeWidth="1.2" />
                      {navPoints && (
                        <polyline fill="none" stroke="#DE6438" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" points={navPoints} />
                      )}
                    </svg>
                  </div>
                </div>

                <div>
                  <div className="text-xs font-bold text-[#2E2823] mb-2">水下回撤曲线 (Underwater Drawdown %)</div>
                  <div className="w-full h-24 bg-[#FAF8F5] rounded-xl border border-[#F0EBE1] p-2">
                    <svg viewBox="0 0 600 85" className="w-full h-full overflow-visible">
                      <line x1="0" y1="10" x2="600" y2="10" stroke="#D8CEBE" strokeWidth="1" />
                      {ddPoints && (
                        <polyline fill="none" stroke="#C24D4D" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" points={ddPoints} />
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
                <div className="bg-white p-4 rounded-xl border border-[#E6DFD5]">
                  <div className="text-xs text-[#7E7469]">OOS 样本外窗口数</div>
                  <div className="text-xl font-bold font-mono text-[#2E2823] mt-1">{wfResult.walk_forward.windows_count} 段</div>
                </div>
                <div className="bg-white p-4 rounded-xl border border-[#E6DFD5]">
                  <div className="text-xs text-[#7E7469]">样本外平均收益率</div>
                  <div className={`text-xl font-bold font-mono mt-1 ${(wfResult.walk_forward.avg_oos_return_pct || 0) >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]'}`}>
                    {(wfResult.walk_forward.avg_oos_return_pct || 0) >= 0 ? '+' : ''}{(wfResult.walk_forward.avg_oos_return_pct || 0).toFixed(2)}%
                  </div>
                </div>
                <div className="bg-white p-4 rounded-xl border border-[#E6DFD5]">
                  <div className="text-xs text-[#7E7469]">样本外平均夏普</div>
                  <div className="text-xl font-bold font-mono text-[#2E2823] mt-1">{(wfResult.walk_forward.avg_oos_sharpe || 0).toFixed(2)}</div>
                </div>
                <div className="bg-white p-4 rounded-xl border border-[#E6DFD5]">
                  <div className="text-xs text-[#7E7469]">最差单窗口回撤</div>
                  <div className="text-xl font-bold font-mono text-[#C24D4D] mt-1">-{(wfResult.walk_forward.worst_oos_drawdown_pct || 0).toFixed(2)}%</div>
                </div>
              </div>

              <div className="bg-white rounded-2xl border border-[#E6DFD5] overflow-hidden">
                <div className="px-5 py-3 bg-[#FAF7F2] border-b border-[#E6DFD5] flex items-center space-x-2">
                  <Layers className="w-4 h-4 text-[#DE6438]" />
                  <h3 className="text-xs font-bold text-[#2E2823]">Walk-Forward 逐窗口样本外明细</h3>
                </div>
                <table className="w-full text-left text-xs">
                  <thead className="bg-[#FAF8F5] text-[#7E7469] font-semibold border-b border-[#EFE9DF]">
                    <tr>
                      <th className="py-2.5 px-4">窗口</th>
                      <th className="py-2.5 px-4">评估 K 线</th>
                      <th className="py-2.5 px-4">OOS 收益率</th>
                      <th className="py-2.5 px-4">夏普比率</th>
                      <th className="py-2.5 px-4">最大回撤</th>
                      <th className="py-2.5 px-4">交易数 / 胜率</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[#EFE9DF] font-mono text-xs">
                    {(wfResult.walk_forward.oos_reports || []).map((r, i) => (
                      <tr key={i} className="hover:bg-[#FAF7F2]">
                        <td className="py-2.5 px-4 font-bold">Window #{i + 1}</td>
                        <td className="py-2.5 px-4">{r.bars_evaluated} bars</td>
                        <td className={`py-2.5 px-4 font-bold ${r.cumulative_return_pct >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]'}`}>
                          {r.cumulative_return_pct >= 0 ? '+' : ''}{Number(r.cumulative_return_pct).toFixed(2)}%
                        </td>
                        <td className="py-2.5 px-4">{Number(r.annualized_sharpe_ratio).toFixed(2)}</td>
                        <td className="py-2.5 px-4 text-[#C24D4D]">-{Number(r.max_drawdown_pct).toFixed(2)}%</td>
                        <td className="py-2.5 px-4">{r.total_trades} 笔 / {Number(r.win_rate_pct).toFixed(1)}%</td>
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
