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
import { useTranslation } from 'react-i18next';
import { showAlert } from '../utils/dialog';
import AccountSummary from './nav/AccountSummary';
import NavMetricCards from './nav/NavMetricCards';
import NavChart from './nav/NavChart';
import PaperOrderPanel from './nav/PaperOrderPanel';
import LedgerPanel from './nav/LedgerPanel';
import { TrendingUp, RefreshCw } from 'lucide-react';
import { getBotNav, getBotTrades, listBotOmsOrders, recoverOmsOrder } from '../api/endpoints';

export default function NavView({ activeBotId = "bot_eth_debate_01" }) {
  const { t } = useTranslation('nav');
  const [timeRange, setTimeRange] = useState('1M'); // 1D | 1W | 1M | ALL
  const [navData, setNavData] = useState(null);
  const [trades, setTrades] = useState([]);
  const [omsData, setOmsData] = useState(null);
  const [recoveringCid, setRecoveringCid] = useState(null);
  const [loading, setLoading] = useState(false);

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
        setNavError(navRes.status === 'rejected' ? (navRes.reason?.message || t('loadFailed')) : t('noData'));
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
      showAlert(err.message || t('common:unknownError'), { title: t('recoverFailed'), tone: 'error' });
    } finally {
      setRecoveringCid(null);
    }
  };

  useEffect(() => {
    fetchData();
  }, [activeBotId]);

  const rawMetrics = (navData && navData.metrics) || {};
  const balance = (navData && navData.balance) || {};
  const positions = (navData && navData.positions) || [];
  const activePosition = positions.find(p => p.quantity > 0);
  const accountKnown = Boolean(navData && navData.balance);
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
              <span>{t('title')}</span>
              <span className="text-xs font-mono bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold">
                {t('badge')}
              </span>
            </h1>
            <p className="text-xs text-ink-3">
              {t('subtitle')}
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
            <span>{t('common:refresh')}</span>
          </button>
        </div>
      </div>

      {/* 2. 核心主内容区 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        
        {navError && (
          <div className="p-3 rounded-xl bg-warning-soft border border-warning-line text-xs font-semibold text-warning">
            {t('dataUnavailable', { error: navError })}
          </div>
        )}

        {/* 2.0 账户资金状态与当前持仓总控看板 */}
        <AccountSummary accountKnown={accountKnown} balance={balance} activePosition={activePosition} />

        {/* 2.1 六大核心量化指标卡片 */}
        <NavMetricCards rawMetrics={rawMetrics} />

        {/* 2.2 单位净值走势图 */}
        <NavChart navData={navData} navError={navError} />

        {/* 2.3 快捷沙盒模拟交易下单舱 */}
        <PaperOrderPanel activeBotId={activeBotId} accountKnown={accountKnown} balance={balance} onOrderPlaced={fetchData} />

        {/* 2.4 成交流水簿 & OMS 订单状态机/原生保护单 双视图 */}
        <LedgerPanel trades={trades} omsData={omsData} recoveringCid={recoveringCid} onRecoverUnknown={handleRecoverUnknown} />

      </div>

    </div>
  );
}
