/**
 * ================================================================================
 * Heimdall 交易平台 - 组合资金配比调整面板 (CapitalDistributionModal)
 * ================================================================================
 */

import React, { useState, useEffect } from 'react';
import { X, PieChart, DollarSign, TrendingUp, RefreshCw, CheckCircle2, AlertCircle } from 'lucide-react';
import { getPortfolioCapitalDistribution, updateBotCapital } from '../api/endpoints';

export default function CapitalDistributionModal({ isOpen, onClose, onUpdated }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [editingCapital, setEditingCapital] = useState({});
  const [savingBotId, setSavingBotId] = useState(null);
  const [toastMsg, setToastMsg] = useState(null);

  const fetchDistribution = async () => {
    setLoading(true);
    try {
      const res = await getPortfolioCapitalDistribution();
      setData(res);
      // 初始化输入态
      const initialMap = {};
      (res.bots || []).forEach(b => {
        initialMap[b.bot_id] = b.allocated_capital;
      });
      setEditingCapital(initialMap);
    } catch (err) {
      console.error('获取资金分布失败:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchDistribution();
    }
  }, [isOpen]);

  if (!isOpen) return null;

  // 临时计算编辑后的总资金与各比例
  const currentTotal = Object.values(editingCapital).reduce((acc, val) => acc + (Number(val) || 0), 0);

  const handleSaveCapital = async (botId) => {
    const newCap = Number(editingCapital[botId]);
    if (!newCap || newCap <= 0) {
      alert('请输入有效的资金数额');
      return;
    }
    setSavingBotId(botId);
    try {
      const res = await updateBotCapital(botId, newCap);
      setToastMsg(`✅ 机器人 [${botId}] 资金成功调整为 $${newCap.toLocaleString()} USDT (份额平滑重算)`);
      setTimeout(() => setToastMsg(null), 3000);
      await fetchDistribution();
      onUpdated && onUpdated();
    } catch (err) {
      alert('调整资金失败: ' + (err.message || '未知错误'));
    } finally {
      setSavingBotId(null);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4 animate-fade-in">
      <div className="w-full max-w-2xl bg-[#FAF7F2] rounded-2xl border border-[#D8CEBE] shadow-2xl overflow-hidden flex flex-col max-h-[90vh]">
        
        {/* 顶部标题栏 */}
        <div className="p-4 border-b border-[#E6DFD5] flex items-center justify-between bg-[#F4EFE6]">
          <div className="flex items-center space-x-2.5">
            <div className="p-2 bg-[#DE6438]/10 text-[#DE6438] rounded-xl border border-[#DE6438]/20">
              <PieChart className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-[#2E2823]">多策略组合资金配比看板</h3>
              <p className="text-[11px] text-[#8C8276]">动态调整每个机器人的头寸分配与平滑重算净值份额 (NAV Continuous)</p>
            </div>
          </div>
          <button 
            onClick={onClose}
            className="p-1.5 rounded-lg text-[#8C8276] hover:bg-[#EBE4DA] hover:text-[#2E2823] transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* 组合总览卡片 */}
        <div className="p-4 bg-white border-b border-[#EBE4DA] flex items-center justify-between">
          <div>
            <div className="text-[11px] text-[#7E7469]">组合总池资金池 (Total Capital)</div>
            <div className="text-xl font-bold font-mono text-[#2E2823]">
              ${(currentTotal || (data?.total_portfolio_capital || 0)).toLocaleString(undefined, { minimumFractionDigits: 2 })} <span className="text-xs text-[#7E7469]">USDT</span>
            </div>
          </div>
          <div className="flex items-center space-x-3">
            <div className="text-right">
              <div className="text-[11px] text-[#7E7469]">活跃策略实例</div>
              <div className="text-sm font-bold font-mono text-[#4D835C]">{data?.bot_count || 0} 个机器人</div>
            </div>
            <button
              onClick={fetchDistribution}
              disabled={loading}
              className="p-2 rounded-xl bg-[#FAF7F2] border border-[#DDD5C7] hover:bg-[#EBE4DA] text-[#5E554B] transition"
              title="刷新数据"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {/* 提示消息 */}
        {toastMsg && (
          <div className="mx-4 mt-3 p-2.5 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs flex items-center space-x-2">
            <CheckCircle2 className="w-4 h-4 shrink-0 text-emerald-600" />
            <span>{toastMsg}</span>
          </div>
        )}

        {/* 机器人列表与资金调整 */}
        <div className="p-4 overflow-y-auto space-y-3.5 flex-1">
          {(!data?.bots || data.bots.length === 0) ? (
            <div className="text-center py-8 text-[#9E9486] text-xs">
              暂无已装载的策略机器人实例
            </div>
          ) : (
            data.bots.map((bot) => {
              const currentVal = Number(editingCapital[bot.bot_id] ?? bot.allocated_capital);
              const pct = currentTotal > 0 ? ((currentVal / currentTotal) * 100).toFixed(1) : '0.0';
              const isModified = currentVal !== bot.allocated_capital;
              const delta = currentVal - bot.allocated_capital;

              return (
                <div 
                  key={bot.bot_id}
                  className="p-3.5 rounded-xl border border-[#E6DFD5] bg-white hover:border-[#D0C4B4] transition shadow-xs"
                >
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-xs text-[#2E2823]">{bot.name}</span>
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-[#EFE9DF] text-[#6E6458]">
                        {bot.symbol}
                      </span>
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-yellow-50 text-yellow-700 border border-yellow-200">
                        {bot.exchange || 'BINANCE'}
                      </span>
                    </div>

                    <div className="flex items-center space-x-2">
                      <span className="text-[11px] text-[#7E7469]">占比:</span>
                      <span className="font-mono text-xs font-bold text-[#DE6438]">{pct}%</span>
                    </div>
                  </div>

                  {/* 进度条可视化 */}
                  <div className="w-full bg-[#EBE4DA] h-2 rounded-full overflow-hidden mb-3">
                    <div 
                      className="bg-[#DE6438] h-full rounded-full transition-all duration-300"
                      style={{ width: `${Math.min(100, Math.max(2, Number(pct)))}%` }}
                    />
                  </div>

                  {/* 核心指标与输入编辑 */}
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 items-center text-xs">
                    <div className="text-[11px] text-[#7E7469]">
                      当前净值 (NAV): <strong className="font-mono text-[#4D835C]">{Number(bot.latest_nav || 1.0).toFixed(4)}</strong>
                      <div className="text-[10px] text-[#A89F93]">总净资产: ${(bot.current_equity || bot.allocated_capital).toFixed(2)}</div>
                    </div>

                    <div className="relative">
                      <input
                        type="number"
                        min="100"
                        step="100"
                        value={editingCapital[bot.bot_id] ?? bot.allocated_capital}
                        onChange={(e) => setEditingCapital({
                          ...editingCapital,
                          [bot.bot_id]: e.target.value,
                        })}
                        className="w-full pl-6 pr-14 py-1.5 bg-[#FAF7F2] border border-[#D8CEBE] rounded-lg font-mono text-xs focus:outline-hidden focus:border-[#DE6438] focus:bg-white"
                      />
                      <span className="absolute left-2 top-1.5 text-[#9E9486] font-bold">$</span>
                      <span className="absolute right-2 top-1.5 text-[#9E9486] text-[10px]">USDT</span>
                    </div>

                    <div className="flex items-center justify-end space-x-2">
                      {isModified && (
                        <span className={`text-[10px] font-mono font-semibold ${delta > 0 ? 'text-[#4D835C]' : 'text-red-500'}`}>
                          {delta > 0 ? `+${delta}` : delta}
                        </span>
                      )}
                      <button
                        onClick={() => handleSaveCapital(bot.bot_id)}
                        disabled={savingBotId === bot.bot_id || !isModified}
                        className="px-3 py-1.5 rounded-lg bg-[#DE6438] hover:bg-[#C5532A] text-white font-semibold text-xs transition disabled:opacity-40 disabled:hover:bg-[#DE6438] shadow-xs"
                      >
                        {savingBotId === bot.bot_id ? '重算中...' : '确认调资'}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* 底部原理说明 */}
        <div className="p-3.5 bg-[#F2EDE4] border-t border-[#E6DFD5] text-[11px] text-[#6E6458] space-y-1">
          <div className="font-semibold text-[#2E2823] flex items-center space-x-1">
            <TrendingUp className="w-3.5 h-3.5 text-[#DE6438]" />
            <span>平滑动态调资与币安模拟对接说明:</span>
          </div>
          <p>
            1. <strong>NAV 曲线平滑连续性</strong>: 追加或减少资金时，系统自动执行 <code>ΔShares = ΔCapital / NAV</code>，即刻重算可用现金与份额，NAV 单位净值保持数学严格连续，绝不产生虚假收益折线。<br/>
            2. <strong>币安模拟交易</strong>: 默认启用高仿真盘口模拟撮合 (Binance Paper Engine)，严格遵守币安标准费率 (Maker 0.02% / Taker 0.04%)；若配置了币安 API Key，则直连币安期货合约沙盒 (Futures Testnet)。
          </p>
        </div>

      </div>
    </div>
  );
}