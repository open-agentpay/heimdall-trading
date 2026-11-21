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
import { 
  TrendingUp, BarChart3, ArrowUpRight, ArrowDownRight, DollarSign, 
  ShieldCheck, RefreshCw, Calendar, Download, Layers, Activity 
} from 'lucide-react';
import { getBotNav, getBotTrades, placePaperOrder, listBotOmsOrders, recoverOmsOrder } from '../api/endpoints';

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

  // 零数据时的真实基准指标（彻底清除历史写死的假盈利 Mock 指标、假曲线与假流水）
  const emptyMetrics = {
    current_nav: 1.0,
    cumulative_return_pct: 0.0,
    annualized_return_pct: 0.0,
    sharpe_ratio: 0.0,
    sortino_ratio: 0.0,
    max_drawdown_pct: 0.0,
    win_rate_pct: 0.0,
    profit_loss_ratio: 0.0,
    total_trades: 0,
    total_equity_usd: 10000.0,
  };

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
      } else {
        setNavData({
          metrics: { current_nav: 1.0, cumulative_return_pct: 0, sharpe_ratio: 0, max_drawdown_pct: 0, win_rate_pct: 0, profit_loss_ratio: 0, total_trades: 0, total_equity_usd: 10000.0 },
          nav_series: [],
        });
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
      alert(`查单恢复失败: ${err.message}`);
    } finally {
      setRecoveringCid(null);
    }
  };

  const handlePlaceOrder = async () => {
    setOrderLoading(true);
    setOrderStatus(null);
    try {
      const res = await placePaperOrder(activeBotId, {
        side: orderSide,
        size_usd: Number(orderAmount),
        leverage: Number(orderLeverage),
      });
      if (res && res.success) {
        setOrderStatus({
          success: true,
          msg: res.message || `模拟下单撮合成交成功！最新 NAV: ${Number(res.nav || 1.0).toFixed(4)}`
        });
        await fetchData();
      } else {
        setOrderStatus({
          success: false,
          msg: res?.detail || res?.message || '下单未能完成'
        });
      }
    } catch (err) {
      setOrderStatus({
        success: false,
        msg: err.message || '模拟下单请求异常'
      });
    } finally {
      setOrderLoading(false);
      setTimeout(() => setOrderStatus(null), 6000);
    }
  };

  useEffect(() => {
    fetchData();
  }, [activeBotId]);

  const rawMetrics = (navData && navData.metrics) || emptyMetrics;
  const balance = (navData && navData.balance) || {
    total_equity: rawMetrics.total_equity_usd ?? 10000.0,
    cash_balance: rawMetrics.total_equity_usd ?? 10000.0,
    margin_used: 0.0,
    unrealized_pnl: 0.0,
    realized_pnl_cum: 0.0,
  };
  const positions = (navData && navData.positions) || [];
  const activePosition = positions.find(p => p.quantity > 0);
  const freeCash = Math.max(0, Number(balance.cash_balance ?? 10000.0));
  const maxPurchasingPower = Math.floor(freeCash * Number(orderLeverage || 1));

  const metrics = {
    current_nav: rawMetrics.current_nav ?? rawMetrics.latest_nav ?? 1.0,
    cumulative_return_pct: rawMetrics.cumulative_return_pct ?? 0.0,
    sharpe_ratio: rawMetrics.sharpe_ratio ?? rawMetrics.annualized_sharpe_ratio ?? 0.0,
    max_drawdown_pct: rawMetrics.max_drawdown_pct ?? 0.0,
    win_rate_pct: rawMetrics.win_rate_pct ?? 0.0,
    profit_loss_ratio: rawMetrics.profit_loss_ratio ?? 0.0,
    total_trades: rawMetrics.total_trades ?? trades.length,
    total_equity_usd: balance.total_equity || rawMetrics.total_equity_usd || 10000.0,
  };

  let series = (navData && navData.nav_series && navData.nav_series.length > 0)
    ? navData.nav_series
    : [
        { date: '初始', nav: 1.0000, total_equity: balance.total_equity || 10000.0 },
        { date: '当前', nav: Number(metrics.current_nav || 1.0), total_equity: balance.total_equity || 10000.0 },
      ];

  // 确保至少有两个点，防止 series.length === 1 时产生除以零 NaN
  if (series.length === 1) {
    const single = series[0];
    series = [
      { date: "初始", timestamp: single.timestamp, nav: 1.0000, total_equity: single.total_equity || 10000.0 },
      single,
    ];
  }

  // 简易 SVG 净值曲线计算
  const navValues = series.map(s => Number(s.nav || 1.0));
  const minNav = Math.min(...navValues, 0.99) * 0.99;
  const maxNav = Math.max(...navValues, 1.01) * 1.01;
  const range = maxNav - minNav || 0.1;

  const points = series.map((s, idx) => {
    const x = series.length > 1 ? (idx / (series.length - 1)) * 600 : 300;
    const y = 180 - ((Number(s.nav || 1.0) - minNav) / range) * 160;
    return `${x},${y}`;
  }).join(' ');

  return (
    <div className="flex-1 flex flex-col bg-[#F6F2EB] overflow-hidden select-none">
      
      {/* 1. 顶部控制栏 */}
      <div className="h-14 bg-[#FAF7F2] border-b border-[#E6DFD5] px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-[#EFE9DF] border border-[#DFD6C7] flex items-center justify-center text-[#3D784E]">
            <TrendingUp className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-[#2E2823] flex items-center space-x-2">
              <span>单位净值绩效与对账中心 (NAV & Ledger)</span>
              <span className="text-[10px] font-mono bg-[#E8F2EA] text-[#3D784E] border border-[#CDE3D2] px-2 py-0.5 rounded-full font-semibold">
                公募基金级折算
              </span>
            </h1>
            <p className="text-[11px] text-[#7E7469]">
              消除实盘追加本金与提现干扰，真实核算夏普比率、最大回撤与累计超额 Alpha
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {/* 时间区间选择 */}
          <div className="flex bg-[#EFE9DF] p-0.5 rounded-lg border border-[#DFD6C7] text-xs">
            {['1D', '1W', '1M', 'ALL'].map((r) => (
              <button
                key={r}
                onClick={() => setTimeRange(r)}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  timeRange === r
                    ? "bg-white text-[#2E2823] shadow-xs"
                    : "text-[#7E7469] hover:text-[#2E2823]"
                }`}
              >
                {r}
              </button>
            ))}
          </div>

          <button 
            onClick={fetchData}
            disabled={loading}
            className="p-1.5 bg-white border border-[#D8CEBE] hover:border-[#DE6438] rounded-lg text-xs font-semibold text-[#5A5046] flex items-center space-x-1.5 shadow-xs transition"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {/* 2. 核心主内容区 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        
        {/* 2.0 账户资金状态与当前持仓总控看板 */}
        <div className="bg-white p-4 rounded-2xl border border-[#E6DFD5] shadow-xs flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-wrap items-center gap-6">
            <div>
              <span className="text-[11px] font-semibold text-[#7E7469] flex items-center space-x-1">
                <span>💼</span>
                <span>账户总资产 (Equity)</span>
              </span>
              <div className="text-base font-bold font-mono text-[#2E2823] mt-0.5">
                ${Number(balance.total_equity || 10000).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </div>
            </div>

            <div className="h-8 w-px bg-[#EFE9DF] hidden sm:block" />

            <div>
              <span className="text-[11px] font-semibold text-[#3D784E] flex items-center space-x-1">
                <span>🟢</span>
                <span>空闲可用现金 (Free Cash)</span>
              </span>
              <div className="text-base font-bold font-mono text-[#3D784E] mt-0.5">
                ${Number(balance.cash_balance ?? 10000).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </div>
            </div>

            <div className="h-8 w-px bg-[#EFE9DF] hidden sm:block" />

            <div>
              <span className="text-[11px] font-semibold text-[#8C5E2E] flex items-center space-x-1">
                <span>🔒</span>
                <span>占用保证金 (Margin Used)</span>
              </span>
              <div className="text-base font-bold font-mono text-[#8C5E2E] mt-0.5">
                ${Number(balance.margin_used || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </div>
            </div>

            <div className="h-8 w-px bg-[#EFE9DF] hidden sm:block" />

            <div>
              <span className="text-[11px] font-semibold text-[#7E7469] flex items-center space-x-1">
                <span>📈</span>
                <span>持仓浮动盈亏 (Unrealized)</span>
              </span>
              <div className={`text-base font-bold font-mono mt-0.5 ${Number(balance.unrealized_pnl || 0) >= 0 ? 'text-[#3D784E]' : 'text-[#C24D4D]'}`}>
                {Number(balance.unrealized_pnl || 0) >= 0 ? '+' : ''}${Number(balance.unrealized_pnl || 0).toFixed(2)}
              </div>
            </div>
          </div>

          {/* 右侧当前持仓头寸徽标 */}
          <div className="flex items-center space-x-2 bg-[#FAF8F5] px-3.5 py-2 rounded-xl border border-[#EFE9DF]">
            <span className="text-xs text-[#7E7469] font-medium">当前持仓头寸:</span>
            {activePosition ? (
              <span className="text-xs font-bold font-mono flex items-center space-x-1.5 text-[#2E2823]">
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${activePosition.side === 'LONG' ? 'bg-[#EBF5EE] text-[#3D784E]' : 'bg-[#FBEFEA] text-[#C24D4D]'}`}>
                  {activePosition.side} {activePosition.leverage}x
                </span>
                <span>{Number(activePosition.quantity).toFixed(4)} {activePosition.symbol}</span>
                <span className="text-[11px] text-[#7E7469]">@ ${Number(activePosition.entry_price).toFixed(2)}</span>
              </span>
            ) : (
              <span className="text-xs font-bold font-mono text-[#7E7469] flex items-center space-x-1.5">
                <span className="w-2 h-2 rounded-full bg-[#3D784E]" />
                <span>当前空仓 (FLAT) · 100% 现金就绪</span>
              </span>
            )}
          </div>
        </div>

        {/* 2.1 六大核心量化指标卡片 */}
        <div className="grid grid-cols-6 gap-3">
          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">单位净值 (NAV)</span>
            <div className="text-xl font-bold font-mono text-[#2E2823] mt-1 flex items-baseline space-x-1">
              <span>{Number(metrics.current_nav || 1.0).toFixed(4)}</span>
              <span className="text-xs text-[#3D784E] font-sans">USD</span>
            </div>
            <span className="text-[10px] text-[#9E9486] font-mono">基准面值 1.0000</span>
          </div>

          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">累计真实收益率</span>
            <div className="text-xl font-bold font-mono text-[#3D784E] mt-1 flex items-center">
              <ArrowUpRight className="w-4 h-4 mr-0.5" />
              <span>+{Number(metrics.cumulative_return_pct || 0).toFixed(2)}%</span>
            </div>
            <span className="text-[10px] text-[#9E9486]">消除出入金扰动</span>
          </div>

          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">夏普比率 (Sharpe)</span>
            <div className="text-xl font-bold font-mono text-[#2E2823] mt-1">
              <span>{Number(metrics.sharpe_ratio || 0).toFixed(2)}</span>
            </div>
            <span className="text-[10px] text-[#3D784E] font-semibold">优秀 (&gt;2.0)</span>
          </div>

          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">历史最大回撤 (MDD)</span>
            <div className="text-xl font-bold font-mono text-[#C24D4D] mt-1 flex items-center">
              <ArrowDownRight className="w-4 h-4 mr-0.5" />
              <span>-{Number(metrics.max_drawdown_pct || 0).toFixed(2)}%</span>
            </div>
            <span className="text-[10px] text-[#9E9486]">单日熔断线 5.0%</span>
          </div>

          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">交易胜率 (Win Rate)</span>
            <div className="text-xl font-bold font-mono text-[#2E2823] mt-1">
              <span>{Number(metrics.win_rate_pct || 0).toFixed(1)}%</span>
            </div>
            <span className="text-[10px] text-[#7E7469]">共 {metrics.total_trades ?? 0} 笔成交流水</span>
          </div>

          <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] shadow-xs">
            <span className="text-[11px] font-semibold text-[#7E7469]">盈亏比 (Profit Factor)</span>
            <div className="text-xl font-bold font-mono text-[#2E2823] mt-1">
              <span>{Number(metrics.profit_loss_ratio || 0).toFixed(2)}:1</span>
            </div>
            <span className="text-[10px] text-[#3D784E] font-semibold">正期望盈利模型</span>
          </div>
        </div>

        {/* 2.2 单位净值走势图 */}
        <div className="bg-white p-5 rounded-2xl border border-[#E6DFD5] shadow-xs space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Activity className="w-4 h-4 text-[#DE6438]" />
              <h3 className="text-xs font-bold text-[#2E2823]">单位净值历史走势曲线 (NAV Curve)</h3>
            </div>
            <div className="flex items-center space-x-3 text-xs text-[#7E7469]">
              <span className="flex items-center space-x-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-[#DE6438]"></span>
                <span className="font-semibold text-[#2E2823]">Heimdall Alpha 策略</span>
              </span>
              <span className="flex items-center space-x-1.5">
                <span className="w-2.5 h-2.5 rounded-full bg-[#C2B7A8]"></span>
                <span>基准基线 (1.0000)</span>
              </span>
            </div>
          </div>

          {/* SVG 矢量走势图 */}
          <div className="w-full h-48 bg-[#FAF8F5] rounded-xl border border-[#F0EBE1] p-3 relative flex flex-col justify-between">
            <svg viewBox="0 0 600 180" className="w-full h-full overflow-visible">
              {/* 基准虚线 1.000 */}
              <line 
                x1="0" 
                y1={180 - ((1.000 - minNav) / range) * 160} 
                x2="600" 
                y2={180 - ((1.000 - minNav) / range) * 160} 
                stroke="#D8CEBE" 
                strokeDasharray="4 4" 
                strokeWidth="1.5"
              />

              {/* 曲线下方渐变填充 */}
              <defs>
                <linearGradient id="navGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#DE6438" stopOpacity="0.25" />
                  <stop offset="100%" stopColor="#DE6438" stopOpacity="0.0" />
                </linearGradient>
              </defs>
              <polygon 
                points={`0,180 ${points} 600,180`} 
                fill="url(#navGradient)" 
              />

              {/* 主走势曲线 */}
              <polyline
                fill="none"
                stroke="#DE6438"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                points={points}
              />
            </svg>

            <div className="flex justify-between text-[10px] text-[#9E9486] font-mono pt-1 border-t border-[#EFE9DF]">
              {series.map((s, i) => (
                <span key={i}>{s.date || (s.timestamp ? s.timestamp.slice(5, 10) : '')}</span>
              ))}
            </div>
          </div>
        </div>

        {/* 2.3 快捷沙盒模拟交易下单舱 */}
        <div className="bg-white p-5 rounded-2xl border border-[#E6DFD5] shadow-xs space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <span className="text-base">⚡</span>
              <h3 className="text-xs font-bold text-[#2E2823]">
                沙盒模拟交易下单 (Paper Order Simulator)
              </h3>
              <span className="text-[10px] font-mono bg-[#EBF5EE] text-[#3D784E] border border-[#CDE3D2] px-2 py-0.5 rounded-full font-semibold">
                Paper Execution Engine
              </span>
            </div>
            <span className="text-[11px] text-[#7E7469]">
              撮合成交将即时更新持仓资产并沉淀成交流水与 NAV 净值走势
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-3 bg-[#FAF8F5] p-3 rounded-xl border border-[#EFE9DF]">
            {/* 方向切换 */}
            <div className="flex bg-[#EFE9DF] p-1 rounded-lg border border-[#DFD6C7] text-xs font-bold space-x-1">
              <button
                type="button"
                onClick={() => setOrderSide('BUY')}
                className={`px-3 py-1.5 rounded-md transition flex items-center space-x-1 ${
                  orderSide === 'BUY'
                    ? 'bg-[#3D784E] text-white shadow-xs'
                    : 'text-[#5A5046] hover:text-[#2E2823]'
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
                    ? 'bg-[#C24D4D] text-white shadow-xs'
                    : 'text-[#5A5046] hover:text-[#2E2823]'
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
                    ? 'bg-[#2E2823] text-white shadow-xs'
                    : 'text-[#5A5046] hover:text-[#2E2823]'
                }`}
              >
                <span>⚪</span>
                <span>平仓 (CLOSE)</span>
              </button>
            </div>

            {/* 名义金额输入与购买力控制 */}
            {orderSide !== 'CLOSE' && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-[#7E7469] font-medium">委托规模:</span>
                <div className="relative">
                  <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-xs text-[#7E7469] font-mono">$</span>
                  <input
                    type="number"
                    min="10"
                    step="100"
                    value={orderAmount}
                    onChange={(e) => setOrderAmount(e.target.value)}
                    className={`w-28 pl-6 pr-2 py-1.5 text-xs font-mono font-bold bg-white border rounded-lg focus:outline-none ${
                      Number(orderAmount) > maxPurchasingPower
                        ? 'border-[#C24D4D] text-[#C24D4D] focus:border-[#C24D4D]'
                        : 'border-[#DFD6C7] text-[#2E2823] focus:border-[#DE6438]'
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
                        className={`text-[10px] px-2 py-1 rounded border font-mono transition ${
                          isActive
                            ? 'bg-[#DE6438] text-white border-[#DE6438]'
                            : 'bg-white text-[#7E7469] border-[#DFD6C7] hover:border-[#DE6438]'
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
                <span className="text-xs text-[#7E7469] font-medium">杠杆:</span>
                <select
                  value={orderLeverage}
                  onChange={(e) => setOrderLeverage(e.target.value)}
                  className="bg-white border border-[#DFD6C7] rounded-lg px-2.5 py-1.5 text-xs font-mono font-bold focus:outline-none focus:border-[#DE6438]"
                >
                  <option value="1">1.0x (无杠杆)</option>
                  <option value="1.5">1.5x (对冲稳健)</option>
                  <option value="2">2.0x (双倍收益)</option>
                  <option value="3">3.0x (HardRiskGuard 风控上限)</option>
                </select>
              </div>
            )}

            {/* 提交按钮 */}
            <button
              type="button"
              onClick={handlePlaceOrder}
              disabled={orderLoading || (orderSide !== 'CLOSE' && Number(orderAmount) > maxPurchasingPower)}
              className={`px-4 py-2 rounded-xl text-xs font-bold text-white shadow-xs transition flex items-center space-x-1.5 ${
                orderSide === 'BUY'
                  ? 'bg-[#3D784E] hover:bg-[#346642]'
                  : orderSide === 'SELL'
                  ? 'bg-[#C24D4D] hover:bg-[#A93F3F]'
                  : 'bg-[#2E2823] hover:bg-[#1A1614]'
              } ${(orderLoading || (orderSide !== 'CLOSE' && Number(orderAmount) > maxPurchasingPower)) ? 'opacity-50 cursor-not-allowed' : ''}`}
            >
              <RefreshCw className={`w-3.5 h-3.5 ${orderLoading ? 'animate-spin' : ''}`} />
              <span>{orderLoading ? '撮合中...' : '🚀 提交模拟撮合成交'}</span>
            </button>
          </div>

          {/* 实时购买力限额说明与超额告警 */}
          {orderSide !== 'CLOSE' && (
            <div className="flex flex-wrap items-center justify-between text-[11px] px-1 pt-0.5 text-[#7E7469]">
              <div className="flex items-center space-x-2 font-mono">
                <span>空闲可用现金: <strong className="text-[#3D784E]">${freeCash.toLocaleString(undefined, { minimumFractionDigits: 2 })}</strong></span>
                <span>·</span>
                <span>当前杠杆购买力上限: <strong className="text-[#2E2823]">${maxPurchasingPower.toLocaleString()}</strong> USD</span>
              </div>
              {Number(orderAmount) > maxPurchasingPower && (
                <span className="text-[#C24D4D] font-semibold flex items-center space-x-1">
                  <span>⚠️</span>
                  <span>委托规模超出可用资金上限，请降低金额或提高杠杆</span>
                </span>
              )}
            </div>
          )}

          {/* 状态提示 */}
          {orderStatus && (
            <div
              className={`p-2.5 rounded-lg text-xs font-medium border flex items-center space-x-2 animate-fade-in ${
                orderStatus.success
                  ? 'bg-[#EBF5EE] text-[#3D784E] border-[#CDE3D2]'
                  : 'bg-[#FBEFEA] text-[#C24D4D] border-[#F2D6CA]'
              }`}
            >
              <span>{orderStatus.success ? '✓' : '⚠️'}</span>
              <span>{orderStatus.msg}</span>
            </div>
          )}
        </div>

        {/* 2.4 成交流水簿 & OMS 订单状态机/原生保护单 双视图 */}
        <div className="bg-white rounded-2xl border border-[#E6DFD5] shadow-xs overflow-hidden">
          <div className="px-5 py-3 bg-[#FAF7F2] border-b border-[#E6DFD5] flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center space-x-2">
              <button
                onClick={() => setLedgerTab('trades')}
                className={`px-3 py-1 rounded-lg text-xs font-bold transition ${ledgerTab === 'trades' ? 'bg-white text-[#DE6438] shadow-xs border border-[#E6DFD5]' : 'text-[#7E7469] hover:text-[#2E2823]'}`}
              >
                成交流水簿 ({trades.length})
              </button>
              <button
                onClick={() => setLedgerTab('oms')}
                className={`px-3 py-1 rounded-lg text-xs font-bold transition flex items-center space-x-1.5 ${ledgerTab === 'oms' ? 'bg-white text-[#DE6438] shadow-xs border border-[#E6DFD5]' : 'text-[#7E7469] hover:text-[#2E2823]'}`}
              >
                <span>OMS 订单状态机 &amp; 原生保护单 ({omsData?.summary?.total || 0})</span>
                {(omsData?.summary?.unknown_count || 0) > 0 && (
                  <span className="px-1.5 py-0.2 rounded-full bg-[#C24D4D] text-white text-[10px]">
                    {omsData.summary.unknown_count} UNKNOWN
                  </span>
                )}
              </button>
            </div>
            {omsData?.instrument_spec && (
              <span className="text-[10px] font-mono text-[#7E7469] bg-white px-2.5 py-1 rounded-lg border border-[#E6DFD5]">
                精度规格 {omsData.instrument_spec.symbol}: tick={omsData.instrument_spec.tick_size} · step={omsData.instrument_spec.step_size} · min=${omsData.instrument_spec.min_notional_usd}
              </span>
            )}
          </div>

          {ledgerTab === 'trades' ? (
            <div className="overflow-x-auto">
              {trades.length === 0 ? (
                <div className="p-8 text-center text-xs text-[#8C8275]">
                  当前机器人暂无真实成交流水（已禁用历史假盈利兜底）。
                </div>
              ) : (
                <table className="w-full text-left text-xs">
                  <thead className="bg-[#FAF8F5] text-[#7E7469] font-semibold border-b border-[#EFE9DF]">
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
                  <tbody className="divide-y divide-[#EFE9DF] font-mono text-[11px]">
                    {trades.map((t, idx) => (
                      <tr key={idx} className="hover:bg-[#FAF7F2] transition">
                        <td className="py-2.5 px-4 text-[#7E7469]">{t.tradeId || t.trade_id}</td>
                        <td className="py-2.5 px-4 text-[#5A5046]">{t.timestamp}</td>
                        <td className="py-2.5 px-4 font-bold text-[#2E2823]">{t.symbol}</td>
                        <td className="py-2.5 px-4">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${(t.side === 'BUY' || t.posSide === 'LONG') ? 'bg-[#EBF5EE] text-[#3D784E]' : 'bg-[#FBEFEA] text-[#DE6438]'}`}>
                            {t.posSide || t.side}
                          </span>
                        </td>
                        <td className="py-2.5 px-4">${Number(t.price || t.executed_price || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
                        <td className="py-2.5 px-4">${Number(t.sizeUsd || t.size_usd || 0).toFixed(2)}</td>
                        <td className="py-2.5 px-4 text-[#7E7469]">${Number(t.feeUsd || t.fee_usd || 0).toFixed(4)}</td>
                        <td className={`py-2.5 px-4 text-right font-bold ${t.pnlColor || 'text-[#3D784E]'}`}>
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
                <div className="p-8 text-center text-xs text-[#8C8275]">
                  暂无 OMS 托管订单与原生条件止损/止盈单记录。
                </div>
              ) : (
                <table className="w-full text-left text-xs">
                  <thead className="bg-[#FAF8F5] text-[#7E7469] font-semibold border-b border-[#EFE9DF]">
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
                  <tbody className="divide-y divide-[#EFE9DF] font-mono text-[11px]">
                    {omsData.orders.map((o) => (
                      <tr key={o.client_order_id} className="hover:bg-[#FAF7F2] transition">
                        <td className="py-2.5 px-4 text-[#5A5046]">{o.client_order_id}</td>
                        <td className="py-2.5 px-4">
                          <span className="font-bold text-[#2E2823]">{o.order_type}</span>
                          {Boolean(o.reduce_only) && (
                            <span className="ml-1.5 px-1.5 py-0.2 rounded bg-[#EBF5EE] text-[#3D784E] text-[9px] font-sans font-bold">
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
                          <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
                            o.state === 'FILLED' ? 'bg-[#EBF5EE] text-[#3D784E]' :
                            o.state === 'UNKNOWN' ? 'bg-[#FDECEC] text-[#C24D4D]' :
                            o.state === 'REJECTED' ? 'bg-[#FDECEC] text-[#C24D4D]' :
                            'bg-[#EFE9DF] text-[#5A5046]'
                          }`}>
                            {o.state}
                          </span>
                        </td>
                        <td className="py-2.5 px-4 text-right">
                          {o.state === 'UNKNOWN' ? (
                            <button
                              onClick={() => handleRecoverUnknown(o.client_order_id)}
                              disabled={recoveringCid === o.client_order_id}
                              className="px-2.5 py-1 rounded bg-[#DE6438] hover:bg-[#C5532A] text-white text-[10px] font-sans font-bold"
                            >
                              {recoveringCid === o.client_order_id ? '查单中...' : '立即查单恢复'}
                            </button>
                          ) : (
                            <span className="text-[#9E9486] text-[10px]">{o.exchange_order_id || '-'}</span>
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
