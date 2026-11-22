/**
 * ================================================================================
 * Heimdall 交易平台 - NAV 单位净值与量化绩效看板 (NavView)
 * ================================================================================
 * 
 * 核心功能：
 * 1. 采用消除出入金干扰的【单位净值法 (NAV)】，真实评估投资收益率；
 * 2. 核心量化对冲指标：单位净值、累计收益、夏普比率、最大回撤、胜率与盈亏比；
 * 3. 动态绘制净值曲线走势；
 * 4. 成交流水明细对账表（方向、均价、名义价值、手续费、滑点与实现盈亏）。
 */

import React, { useState, useEffect } from 'react';
import { showAlert } from '../utils/dialog';
import { 
  TrendingUp, BarChart3, ArrowUpRight, ArrowDownRight, DollarSign, 
  ShieldCheck, RefreshCw, Calendar, Download, Layers, Activity 
} from 'lucide-react';
import { getBotNav, getBotTrades, placePaperOrder, getPaperOrderQuote, listBotOmsOrders, recoverOmsOrder } from '../api/endpoints';
import { numOrNull, fmtNum, fmtUsd } from '../data/agentRoles';

// 成交价来源标签（对应后端 place_paper_order 返回的 price_source / reference_price_source）
const PRICE_SOURCE_META = {
  LIVE: { label: '跨所实时中位价', className: 'bg-success-soft text-success border-success-line' },
  USER: { label: '手动指定价', className: 'bg-subtle text-ink-2 border-line-strong' },
  SIMULATED: { label: '离线仿真价', className: 'bg-warning-soft text-warning border-warning-line' },
};

function priceSourceMeta(source) {
  return PRICE_SOURCE_META[String(source || '').toUpperCase()] || {
    label: source || '未知来源',
    className: 'bg-subtle text-ink-3 border-line-strong',
  };
}

function formatUsd(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '--';
  return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatDeviation(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return `${(num * 100).toFixed(2)}%`;
}

// 偏差着色：≤0.3% 绿 / ≤0.7% 橙 / 其余红（后端默认容差 1%）
function deviationClass(value) {
  const num = Math.abs(Number(value) || 0);
  if (num <= 0.003) return 'text-success';
  if (num <= 0.007) return 'text-warning';
  return 'text-danger';
}

export default function NavView({ activeBotId = "bot_eth_debate_01" }) {
  const [timeRange, setTimeRange] = useState('1M'); // 1D | 1W | 1M | ALL
  const [ledgerTab, setLedgerTab] = useState('trades'); // trades | oms
  const [navData, setNavData] = useState(null);
  const [trades, setTrades] = useState([]);
  const [omsData, setOmsData] = useState(null);
  const [recoveringCid, setRecoveringCid] = useState(null);
  const [loading, setLoading] = useState(false);

  // 模拟下单表单状态
  const [orderSide, setOrderSide] = useState('BUY'); // BUY | SELL | CLOSE
  const [orderAmount, setOrderAmount] = useState(1000);
  const [orderLeverage, setOrderLeverage] = useState(2);
  const [orderLoading, setOrderLoading] = useState(false);
  const [orderStatus, setOrderStatus] = useState(null);

  // 指定成交价（可选）：开启后按输入价格撮合，后端校验其与跨所实时中位价的偏差
  const [useCustomPrice, setUseCustomPrice] = useState(false);
  const [customPrice, setCustomPrice] = useState('');
  const [quote, setQuote] = useState(null);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState(null);

  // 接口失败时记录错误并显示 “--”，不再用 $10,000 / 净值 1.0 之类的默认值冒充真实账户
  const [navError, setNavError] = useState(null);

  const fetchData = async () => {
    setLoading(true);
    try {
      const [navRes, tradesRes, omsRes] = await Promise.allSettled([
        getBotNav(activeBotId),
        getBotTrades(activeBotId, 30),
        listBotOmsOrders(activeBotId, 50),
      ]);

      if (navRes.status === 'fulfilled' && navRes.value && navRes.value.metrics) {
        setNavData(navRes.value);
        setNavError(null);
      } else {
        setNavData(null);
        setNavError(navRes.status === 'rejected' ? (navRes.reason?.message || '净值接口请求失败') : '净值接口未返回数据');
      }

      if (tradesRes.status === 'fulfilled' && Array.isArray(tradesRes.value)) {
        setTrades(tradesRes.value);
      } else {
        setTrades([]);
      }

      if (omsRes.status === 'fulfilled' && omsRes.value) {
        setOmsData(omsRes.value);
      }
    } catch (err) {
      console.warn('[NavView] 读取净值与 OMS 数据异常:', err);
      setTrades([]);
    } finally {
      setLoading(false);
    }
  };

  const handleRecoverUnknown = async (cid) => {
    setRecoveringCid(cid);
    try {
      await recoverOmsOrder(activeBotId, cid);
      await fetchData();
    } catch (err) {
      showAlert(err.message || '未知错误', { title: '查单恢复失败', tone: 'error' });
    } finally {
      setRecoveringCid(null);
    }
  };

  // 读取跨所实时参考价与偏差容差（与后端下单接口同一取价规则）
  const fetchQuote = async () => {
    setQuoteLoading(true);
    try {
      const q = await getPaperOrderQuote(activeBotId);
      setQuote(q);
      setQuoteError(null);
    } catch (err) {
      setQuote(null);
      setQuoteError(err?.message || '参考价读取失败');
    } finally {
      setQuoteLoading(false);
    }
  };

  const handlePlaceOrder = async () => {
    setOrderLoading(true);
    setOrderStatus(null);
    let dismissMs = 8000;
    try {
      const payload = {
        side: orderSide,
        size_usd: Number(orderAmount),
        leverage: Number(orderLeverage),
      };
      if (useCustomPrice && customPriceNum > 0) {
        payload.price = customPriceNum;
      }
      const res = await placePaperOrder(activeBotId, payload);
      if (res && res.success) {
        setOrderStatus({
          success: true,
          title: '撮合成交成功',
          msg: res.message || `模拟下单撮合成交成功！最新 NAV: ${Number(res.nav || 1.0).toFixed(4)}`,
          // M2：成交价锚定跨所实时中位价，回显参考价、价格来源与偏差，便于核对成交是否可信
          pricing: {
            fillPrice: res.trade?.price,
            priceSource: res.price_source,
            referencePrice: res.reference_price,
            referenceSource: res.reference_price_source,
            deviationPct: res.price_deviation_pct,
          },
          nav: res.nav,
        });
        await fetchData();
        fetchQuote();
      } else {
        dismissMs = 12000;
        setOrderStatus({
          success: false,
          title: '下单未能完成',
          msg: res?.detail || res?.message || '下单未能完成'
        });
      }
    } catch (err) {
      // 按后端拒单原因分类展示：503 行情不可用 / 400 价格偏差 / 400 风控拦截 / 400 资金不足 / 其他
      dismissMs = 12000;
      const statusCode = err?.status;
      const detail = err?.message || '模拟下单请求异常';
      let title = '下单被拒绝';
      let kind = 'generic';
      if (statusCode === 503) {
        title = '行情不可用，已拒单';
        kind = 'market_unavailable';
      } else if (statusCode === 400 && detail.includes('偏离')) {
        title = '价格偏离实时行情，已拒单';
        kind = 'price_deviation';
      } else if (statusCode === 400 && detail.includes('风控拦截')) {
        title = '风控拦截';
        kind = 'risk_guard';
      } else if (statusCode === 400 && detail.includes('资金不足')) {
        title = '可用资金不足';
        kind = 'insufficient_funds';
      } else if (statusCode === 401 || statusCode === 403) {
        title = '无权限下单';
        kind = 'auth';
      } else if (statusCode === 0 || statusCode === 408) {
        title = '网络异常';
        kind = 'network';
      }
      setOrderStatus({
        success: false,
        title,
        kind,
        statusCode,
        msg: detail,
      });
    } finally {
      setOrderLoading(false);
      setTimeout(() => setOrderStatus(null), dismissMs);
    }
  };

  useEffect(() => {
    fetchData();
  }, [activeBotId]);

  useEffect(() => {
    fetchQuote();
    if (!useCustomPrice) return undefined;
    const timer = setInterval(fetchQuote, 15000);
    return () => clearInterval(timer);
  }, [activeBotId, useCustomPrice]);

  const rawMetrics = (navData && navData.metrics) || {};
  const balance = (navData && navData.balance) || {};
  const positions = (navData && navData.positions) || [];
  const activePosition = positions.find(p => p.quantity > 0);
  const accountKnown = Boolean(navData && navData.balance);
  // 账户数据未知时购买力为 0，禁止在未知资金状态下提交开仓
  const freeCash = accountKnown ? Math.max(0, Number(balance.cash_balance) || 0) : 0;
  const maxPurchasingPower = Math.floor(freeCash * Number(orderLeverage || 1));

  // 指定成交价偏差（前端预检；后端会用下单时刻的最新实时价再校验一次）
  const referencePrice = quote?.tradable ? Number(quote.reference_price) : null;
  const maxDeviationPct = Number(quote?.max_deviation_pct ?? 0.01);
  const customPriceNum = Number(customPrice);
  const customPriceValid = useCustomPrice && Number.isFinite(customPriceNum) && customPriceNum > 0;
  const customDeviationPct = customPriceValid && referencePrice > 0
    ? Math.abs(customPriceNum - referencePrice) / referencePrice
    : null;
  const customPriceExceeds = customDeviationPct !== null && customDeviationPct > maxDeviationPct;
  const overBuyingPower = accountKnown && orderSide !== 'CLOSE' && Number(orderAmount) > maxPurchasingPower;
  const submitBlocked = orderLoading
    || (orderSide !== 'CLOSE' && !accountKnown)
    || overBuyingPower
    || (useCustomPrice && (!customPriceValid || customPriceExceeds));

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

  // 净值曲线只画后端真实快照；不足两个点时显示空状态，不再补“初始 1.0000”这类虚拟点
  const series = (navData && Array.isArray(navData.nav_series))
    ? navData.nav_series.filter((p) => numOrNull(p.nav) !== null)
    : [];
  const hasCurve = series.length >= 2;

  // 简易 SVG 净值曲线计算
  const navValues = hasCurve ? series.map(s => Number(s.nav)) : [1.0];
  const minNav = Math.min(...navValues, 0.99) * 0.99;
  const maxNav = Math.max(...navValues, 1.01) * 1.01;
  const range = maxNav - minNav || 0.1;

  const points = series.map((s, idx) => {
    const x = series.length > 1 ? (idx / (series.length - 1)) * 600 : 300;
    const y = 180 - ((Number(s.nav) - minNav) / range) * 160;
    return `${x},${y}`;
  }).join(' ');

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      
      {/* 1. 顶部控制栏 */}
      <div className="h-14 bg-panel border-b border-line px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-muted border border-line-strong flex items-center justify-center text-success">
            <TrendingUp className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-ink flex items-center space-x-2">
              <span>单位净值绩效与对账中心 (NAV & Ledger)</span>
              <span className="text-xs font-mono bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold">
                公募基金级折算
              </span>
            </h1>
            <p className="text-xs text-ink-3">
              消除实盘追加本金与提现干扰，真实核算夏普比率、最大回撤与累计超额 Alpha
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {/* 时间区间选择 */}
          <div className="flex bg-muted p-0.5 rounded-lg border border-line-strong text-xs">
            {['1D', '1W', '1M', 'ALL'].map((r) => (
              <button
                key={r}
                onClick={() => setTimeRange(r)}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  timeRange === r
                    ? "bg-surface text-ink shadow-xs"
                    : "text-ink-3 hover:text-ink"
                }`}
              >
                {r}
              </button>
            ))}
          </div>

          <button 
            onClick={fetchData}
            disabled={loading}
            className="p-1.5 bg-surface border border-line-strong hover:border-brand rounded-lg text-xs font-semibold text-ink-2 flex items-center space-x-1.5 shadow-xs transition"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {/* 2. 核心主内容区 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        
        {navError && (
          <div className="p-3 rounded-xl bg-warning-soft border border-warning-line text-xs font-semibold text-warning">
            📡 净值与账户数据不可用：{navError}。以下指标显示为 “--”，请检查后端服务后点击刷新。
          </div>
        )}

        {/* 2.0 账户资金状态与当前持仓总控看板 */}
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

        {/* 2.1 六大核心量化指标卡片 */}
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

        {/* 2.2 单位净值走势图 */}
        <div className="bg-surface p-5 rounded-2xl border border-line shadow-xs space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Activity className="w-4 h-4 text-brand" />
              <h3 className="text-xs font-bold text-ink">单位净值历史走势曲线 (NAV Curve)</h3>
            </div>
            <div className="flex items-center space-x-3 text-xs text-ink-3">
              <span className="flex items-center space-x-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-brand"></span>
                <span className="font-semibold text-ink">Heimdall Alpha 策略</span>
              </span>
              <span className="flex items-center space-x-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-muted-strong"></span>
                <span>基准基线 (1.0000)</span>
              </span>
            </div>
          </div>

          {/* SVG 矢量走势图 */}
          {!hasCurve ? (
            <div className="w-full h-48 bg-panel rounded-xl border border-dashed border-line flex items-center justify-center text-xs text-ink-4">
              {navError ? `净值数据不可用：${navError}` : '净值快照不足两个，暂无走势曲线'}
            </div>
          ) : (
          <div className="w-full h-48 bg-panel rounded-xl border border-line-soft p-3 relative flex flex-col justify-between">
            <svg viewBox="0 0 600 180" className="w-full h-full overflow-visible">
              {/* 基准虚线 1.000 */}
              <line 
                x1="0" 
                y1={180 - ((1.000 - minNav) / range) * 160} 
                x2="600" 
                y2={180 - ((1.000 - minNav) / range) * 160} 
                style={{ stroke: 'rgb(var(--c-line-strong))' }} 
                strokeDasharray="4 4" 
                strokeWidth="1.5"
              />

              {/* 曲线下方渐变填充 */}
              <defs>
                <linearGradient id="navGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" style={{ stopColor: 'rgb(var(--c-brand))', stopOpacity: 0.25 }} />
                  <stop offset="100%" style={{ stopColor: 'rgb(var(--c-brand))', stopOpacity: 0.0 }} />
                </linearGradient>
              </defs>
              <polygon 
                points={`0,180 ${points} 600,180`} 
                fill="url(#navGradient)" 
              />

              {/* 主走势曲线 */}
              <polyline
                fill="none"
                style={{ stroke: 'rgb(var(--c-brand))' }}
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                points={points}
              />
            </svg>

            <div className="flex justify-between text-xs text-ink-4 font-mono pt-1 border-t border-line-soft">
              {series.map((s, i) => (
                <span key={i}>{s.date || (s.timestamp ? s.timestamp.slice(5, 10) : '')}</span>
              ))}
            </div>
          </div>
          )}
        </div>

        {/* 2.3 快捷沙盒模拟交易下单舱 */}
        <div className="bg-surface p-5 rounded-2xl border border-line shadow-xs space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <span className="text-base">⚡</span>
              <h3 className="text-xs font-bold text-ink">
                沙盒模拟交易下单 (Paper Order Simulator)
              </h3>
              <span className="text-xs font-mono bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold">
                Paper Execution Engine
              </span>
            </div>
            <span className="text-xs text-ink-3">
              撮合成交将即时更新持仓资产并沉淀成交流水与 NAV 净值走势
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-3 bg-panel p-3 rounded-xl border border-line-soft">
            {/* 方向切换 */}
            <div className="flex bg-muted p-1 rounded-lg border border-line-strong text-xs font-bold space-x-1">
              <button
                type="button"
                onClick={() => setOrderSide('BUY')}
                className={`px-3 py-1.5 rounded-md transition flex items-center space-x-1 ${
                  orderSide === 'BUY'
                    ? 'bg-success-solid text-white shadow-xs'
                    : 'text-ink-2 hover:text-ink'
                }`}
              >
                <span>🟢</span>
                <span>做多 (BUY)</span>
              </button>
              <button
                type="button"
                onClick={() => setOrderSide('SELL')}
                className={`px-3 py-1.5 rounded-md transition flex items-center space-x-1 ${
                  orderSide === 'SELL'
                    ? 'bg-danger-solid text-white shadow-xs'
                    : 'text-ink-2 hover:text-ink'
                }`}
              >
                <span>🔴</span>
                <span>做空 (SELL)</span>
              </button>
              <button
                type="button"
                onClick={() => setOrderSide('CLOSE')}
                className={`px-3 py-1.5 rounded-md transition flex items-center space-x-1 ${
                  orderSide === 'CLOSE'
                    ? 'bg-ink text-canvas shadow-xs'
                    : 'text-ink-2 hover:text-ink'
                }`}
              >
                <span>⚪</span>
                <span>平仓 (CLOSE)</span>
              </button>
            </div>

            {/* 名义金额输入与购买力控制 */}
            {orderSide !== 'CLOSE' && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-ink-3 font-medium">委托规模:</span>
                <div className="relative">
                  <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-ink-3 font-mono">$</span>
                  <input
                    type="number"
                    min="10"
                    step="100"
                    value={orderAmount}
                    onChange={(e) => setOrderAmount(e.target.value)}
                    className={`w-28 pl-6 pr-2 py-1.5 text-xs font-mono font-bold bg-surface border rounded-lg focus:outline-none ${
                      overBuyingPower
                        ? 'border-danger text-danger focus:border-danger'
                        : 'border-line-strong text-ink focus:border-brand'
                    }`}
                    placeholder="1000"
                  />
                </div>
                {/* 仓位比例快捷按键 */}
                <div className="flex space-x-1">
                  {[
                    { label: '25%', ratio: 0.25 },
                    { label: '50%', ratio: 0.50 },
                    { label: '75%', ratio: 0.75 },
                    { label: '100% 最大', ratio: 1.0 },
                  ].map((btn) => {
                    const targetAmt = Math.max(10, Math.floor(maxPurchasingPower * btn.ratio));
                    const isActive = Number(orderAmount) === targetAmt;
                    return (
                      <button
                        key={btn.label}
                        type="button"
                        onClick={() => setOrderAmount(targetAmt)}
                        className={`text-xs px-2 py-1 rounded border font-mono transition ${
                          isActive
                            ? 'bg-brand text-white border-brand'
                            : 'bg-surface text-ink-3 border-line-strong hover:border-brand'
                        }`}
                      >
                        {btn.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* 杠杆选择 */}
            {orderSide !== 'CLOSE' && (
              <div className="flex items-center space-x-2">
                <span className="text-xs text-ink-3 font-medium">杠杆:</span>
                <select
                  value={orderLeverage}
                  onChange={(e) => setOrderLeverage(e.target.value)}
                  className="bg-surface border border-line-strong rounded-lg px-2.5 py-1.5 text-xs font-mono font-bold focus:outline-none focus:border-brand"
                >
                  <option value="1">1.0x (无杠杆)</option>
                  <option value="1.5">1.5x (对冲稳健)</option>
                  <option value="2">2.0x (双倍收益)</option>
                  <option value="3">3.0x (HardRiskGuard 风控上限)</option>
                </select>
              </div>
            )}

            {/* 指定成交价（可选） */}
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center space-x-1.5 text-xs text-ink-3 font-medium cursor-pointer">
                <input
                  type="checkbox"
                  checked={useCustomPrice}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setUseCustomPrice(on);
                    if (on && !customPrice && referencePrice > 0) {
                      setCustomPrice(String(referencePrice));
                    }
                  }}
                  className="accent-brand"
                />
                <span>指定成交价</span>
              </label>
              {useCustomPrice && (
                <>
                  <div className="relative">
                    <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-ink-3 font-mono">$</span>
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={customPrice}
                      onChange={(e) => setCustomPrice(e.target.value)}
                      placeholder={referencePrice > 0 ? String(referencePrice) : '价格'}
                      className={`w-28 pl-6 pr-2 py-1.5 text-xs font-mono font-bold bg-surface border rounded-lg focus:outline-none ${
                        customPriceExceeds || (customPrice !== '' && !customPriceValid)
                          ? 'border-danger text-danger focus:border-danger'
                          : 'border-line-strong text-ink focus:border-brand'
                      }`}
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => referencePrice > 0 && setCustomPrice(String(referencePrice))}
                    disabled={!(referencePrice > 0)}
                    className="text-xs px-2 py-1 rounded border font-mono bg-surface text-ink-3 border-line-strong hover:border-brand disabled:opacity-40"
                  >
                    用参考价
                  </button>
                  {customDeviationPct !== null && (
                    <span className={`text-xs font-mono font-bold ${customPriceExceeds ? 'text-danger' : deviationClass(customDeviationPct)}`}>
                      偏差 {formatDeviation(customDeviationPct)}
                    </span>
                  )}
                </>
              )}
            </div>

            {/* 提交按钮 */}
            <button
              type="button"
              onClick={handlePlaceOrder}
              disabled={submitBlocked}
              className={`px-4 py-2 rounded-xl text-xs font-bold shadow-xs transition flex items-center space-x-1.5 ${
                orderSide === 'BUY'
                  ? 'bg-success-solid hover:bg-success-solid-hover text-white'
                  : orderSide === 'SELL'
                  ? 'bg-danger-solid hover:bg-danger-solid-hover text-white'
                  : 'bg-ink hover:bg-ink-2 text-canvas'
              } ${submitBlocked ? 'opacity-50 cursor-not-allowed' : ''}`}
            >
              <RefreshCw className={`w-3.5 h-3.5 ${orderLoading ? 'animate-spin' : ''}`} />
              <span>{orderLoading ? '撮合中...' : '🚀 提交模拟撮合成交'}</span>
            </button>
          </div>

          {/* 实时购买力限额说明与超额告警 */}
          {orderSide !== 'CLOSE' && (
            <div className="flex flex-wrap items-center justify-between text-xs px-1 pt-0.5 text-ink-3">
              <div className="flex items-center space-x-2 font-mono">
                <span>空闲可用现金: <strong className="text-success">{accountKnown ? `$${freeCash.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : '--'}</strong></span>
                <span>·</span>
                <span>当前杠杆购买力上限: <strong className="text-ink">{accountKnown ? `$${maxPurchasingPower.toLocaleString()}` : '--'}</strong> USD</span>
              </div>
              {!accountKnown && (
                <span className="text-warning font-semibold">📡 账户数据不可用，暂不能开仓</span>
              )}
              {overBuyingPower && (
                <span className="text-danger font-semibold flex items-center space-x-1">
                  <span>⚠️</span>
                  <span>委托规模超出可用资金上限，请降低金额或提高杠杆</span>
                </span>
              )}
            </div>
          )}

          {/* 跨所实时参考价 & 指定价偏差预检 */}
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs px-1 text-ink-3">
            <div className="flex flex-wrap items-center gap-2 font-mono">
              <span>
                跨所参考价:{' '}
                <strong className={quote?.tradable ? 'text-ink' : 'text-warning'}>
                  {quote?.tradable ? formatUsd(quote.reference_price) : (quoteLoading && !quote ? '读取中…' : '不可用')}
                </strong>
              </span>
              {quote?.tradable && (
                <span className={`px-1.5 py-0.5 rounded border font-sans font-semibold ${priceSourceMeta(quote.reference_price_source).className}`}>
                  {priceSourceMeta(quote.reference_price_source).label}
                </span>
              )}
              <span>·</span>
              <span>指定价容差 ±{formatDeviation(maxDeviationPct)}</span>
              <button
                type="button"
                onClick={fetchQuote}
                disabled={quoteLoading}
                className="flex items-center space-x-1 text-ink-3 hover:text-brand disabled:opacity-40"
                title="刷新参考价"
              >
                <RefreshCw className={`w-3 h-3 ${quoteLoading ? 'animate-spin' : ''}`} />
                <span className="font-sans">刷新</span>
              </button>
            </div>
            {useCustomPrice && customPriceExceeds && (
              <span className="text-danger font-semibold">
                ⚠️ 指定价偏离参考价 {formatDeviation(customDeviationPct)}，超过 ±{formatDeviation(maxDeviationPct)} 容差，提交将被拒绝
              </span>
            )}
            {useCustomPrice && customPrice !== '' && !customPriceValid && (
              <span className="text-danger font-semibold">⚠️ 请输入大于 0 的价格</span>
            )}
            {!quote?.tradable && (quote?.message || quoteError) && (
              <span className="text-warning font-semibold">📡 {quote?.message || quoteError}</span>
            )}
          </div>

          {/* 状态提示 */}
          {orderStatus && (
            <div
              className={`p-3 rounded-lg text-xs border animate-fade-in ${
                orderStatus.success
                  ? 'bg-success-soft text-success border-success-line'
                  : orderStatus.kind === 'market_unavailable'
                  ? 'bg-warning-soft text-warning border-warning-line'
                  : 'bg-brand-soft text-danger border-brand-line'
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-start space-x-2 min-w-0">
                  <span className="shrink-0">
                    {orderStatus.success ? '✓' : orderStatus.kind === 'market_unavailable' ? '📡' : '⚠️'}
                  </span>
                  <div className="min-w-0">
                    <div className="font-bold">
                      {orderStatus.title || (orderStatus.success ? '撮合成交成功' : '下单被拒绝')}
                      {!orderStatus.success && orderStatus.statusCode ? (
                        <span className="ml-1.5 font-mono font-normal opacity-70">HTTP {orderStatus.statusCode}</span>
                      ) : null}
                    </div>
                    <div className="mt-0.5 font-medium break-words">{orderStatus.msg}</div>
                    {orderStatus.kind === 'market_unavailable' && (
                      <div className="mt-1 text-xs opacity-80">
                        跨所行情源暂不可用或价格严重背离，为避免按失真价格成交已暂停下单，请稍后重试。
                      </div>
                    )}
                    {orderStatus.kind === 'price_deviation' && (
                      <div className="mt-1 text-xs opacity-80">
                        成交价须贴近跨所实时中位价（默认容差 1%），不填价格将直接按实时价成交。
                      </div>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setOrderStatus(null)}
                  className="shrink-0 opacity-60 hover:opacity-100 font-mono"
                  aria-label="关闭提示"
                >
                  ✕
                </button>
              </div>

              {/* M2：成交价格核对明细（成交价 / 价格来源 / 跨所参考价 / 偏差） */}
              {orderStatus.success && orderStatus.pricing && (
                <div className="mt-2 pt-2 border-t border-success-line grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-ink-2">
                  <div>
                    <div className="text-ink-3">成交价</div>
                    <div className="font-mono font-bold text-ink">{formatUsd(orderStatus.pricing.fillPrice)}</div>
                  </div>
                  <div>
                    <div className="text-ink-3">价格来源</div>
                    <span className={`inline-block mt-0.5 px-1.5 py-0.5 rounded border font-semibold ${priceSourceMeta(orderStatus.pricing.priceSource).className}`}>
                      {priceSourceMeta(orderStatus.pricing.priceSource).label}
                    </span>
                  </div>
                  <div>
                    <div className="text-ink-3">跨所参考价</div>
                    <div className="font-mono font-bold text-ink">
                      {formatUsd(orderStatus.pricing.referencePrice)}
                    </div>
                    {orderStatus.pricing.referenceSource && orderStatus.pricing.referenceSource !== 'LIVE' && (
                      <div className="text-xs text-warning">{priceSourceMeta(orderStatus.pricing.referenceSource).label}</div>
                    )}
                  </div>
                  <div>
                    <div className="text-ink-3">偏差</div>
                    <div className={`font-mono font-bold ${deviationClass(orderStatus.pricing.deviationPct)}`}>
                      {formatDeviation(orderStatus.pricing.deviationPct)}
                    </div>
                  </div>
                </div>
              )}
              {orderStatus.success && orderStatus.pricing?.priceSource === 'SIMULATED' && (
                <div className="mt-2 text-xs text-warning font-semibold">
                  ⚠️ 本笔按离线仿真价成交（已显式开启离线兜底），净值与盈亏不代表真实行情。
                </div>
              )}
            </div>
          )}
        </div>

        {/* 2.4 成交流水簿 & OMS 订单状态机/原生保护单 双视图 */}
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
                              onClick={() => handleRecoverUnknown(o.client_order_id)}
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

      </div>

    </div>
  );
}
