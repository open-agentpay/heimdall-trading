/**
 * ================================================================================
 * Heimdall 交易平台 - 新建策略机器人弹窗 (CreateBotModal)
 * ================================================================================
 *
 * 统一三元组路由契约：
 * - 交易所：BINANCE / HYPERLIQUID / OKX
 * - 市场类型：SPOT (现货) / USDM_FUTURES (U 本位合约)
 * - 运行模式：LOCAL_SIM (本地模拟) / TESTNET (测试网) / MAINNET (实盘)
 * 不再展示历史 PAPER 或 DEMO 选项。
 */

import React, { useState, useEffect } from 'react';
import { X, Bot, ShieldCheck, ArrowRight, CheckCircle2, XCircle } from 'lucide-react';
import { createBot, getStrategiesCatalog, checkLiveReadiness } from '../api/endpoints';
import { MAINSTREAM_SYMBOLS } from '../data/symbols';

export default function CreateBotModal({ isOpen, onClose, onCreated, prefill = null }) {
  const [catalog, setCatalog] = useState([]);
  const [formData, setFormData] = useState({
    bot_id: `bot_${Date.now().toString().slice(-4)}`,
    name: 'ETH 趋势突破跟踪 02',
    symbol: 'ETH',
    strategy_type: 'BULL_BEAR_DEBATE',
    exchange: 'BINANCE',
    market_type: 'USDM_FUTURES',
    execution_mode: 'LOCAL_SIM',
    allocated_capital: 10000.0,
    max_drawdown_limit: 0.05,
  });
  const [strategyConfig, setStrategyConfig] = useState({});
  const [readiness, setReadiness] = useState(null);
  const [checkingReadiness, setCheckingReadiness] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    getStrategiesCatalog()
      .then((res) => setCatalog(res?.strategies || []))
      .catch(() => {});
  }, [isOpen]);

  // 接收来自回测实验室的“一键应用参数”预填
  useEffect(() => {
    if (!isOpen || !prefill) return;
    setFormData((prev) => ({
      ...prev,
      bot_id: `bot_${(prefill.symbol || 'eth').toLowerCase()}_${Date.now().toString().slice(-4)}`,
      name: `${prefill.symbol || 'ETH'} ${prefill.strategy_type} (回测优选)`,
      symbol: prefill.symbol || prev.symbol,
      strategy_type: prefill.strategy_type || prev.strategy_type,
      allocated_capital: prefill.allocated_capital || prev.allocated_capital,
    }));
    if (prefill.strategy_config) {
      setStrategyConfig(prefill.strategy_config);
    }
  }, [isOpen, prefill]);

  // 当选择 TESTNET / MAINNET 时实时触发按市场与环境精确匹配的实盘就绪预检
  useEffect(() => {
    if (!isOpen || formData.execution_mode === 'LOCAL_SIM') {
      setReadiness(null);
      return;
    }
    setCheckingReadiness(true);
    checkLiveReadiness({
      exchange: formData.exchange,
      marketType: formData.market_type,
      executionMode: formData.execution_mode,
      symbol: formData.symbol,
    })
      .then((res) => setReadiness(res))
      .catch((err) => setReadiness({ error: err.message, checks: [] }))
      .finally(() => setCheckingReadiness(false));
  }, [isOpen, formData.execution_mode, formData.exchange, formData.market_type, formData.symbol]);

  if (!isOpen) return null;

  const activeMeta = catalog.find((s) => s.strategy_type === formData.strategy_type);
  const blockedByReadiness =
    (formData.execution_mode === 'TESTNET' && readiness && !readiness.ready_for_testnet) ||
    (formData.execution_mode === 'MAINNET' && readiness && !readiness.ready_for_mainnet);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await createBot({
        bot_id: formData.bot_id.trim(),
        name: formData.name.trim(),
        symbol: formData.symbol.toUpperCase().trim(),
        strategy_type: formData.strategy_type,
        exchange: formData.exchange,
        market_type: formData.market_type,
        execution_mode: formData.execution_mode,
        strategy_config: strategyConfig,
        allocated_capital: Number(formData.allocated_capital),
        max_drawdown_limit: Number(formData.max_drawdown_limit),
      });
      onCreated && onCreated(formData.bot_id);
      onClose();
    } catch (err) {
      setError(err.message || '创建机器人失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4">
      <div className="w-full max-w-xl bg-[#FAF7F2] rounded-2xl border border-[#D8CEBE] shadow-2xl overflow-hidden flex flex-col max-h-[92vh]">
        <div className="p-4 border-b border-[#E6DFD5] flex items-center justify-between bg-[#F4EFE6]">
          <div className="flex items-center space-x-2.5">
            <div className="p-2 bg-[#DE6438]/10 text-[#DE6438] rounded-xl border border-[#DE6438]/20">
              <Bot className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-[#2E2823]">新建交易机器人 (StrategyRouter)</h3>
              <p className="text-xs text-[#8C8276]">交易所、市场类型与运行模式独立建模，严禁跨市场误路由</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-[#8C8276] hover:bg-[#EBE4DA] hover:text-[#2E2823] transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 overflow-y-auto space-y-4 text-xs">
          {error && (
            <div className="p-2.5 rounded-xl bg-red-50 border border-red-200 text-red-700 text-xs">
              ⚠️ {error}
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-[#665E54] mb-1">机器人唯一 ID</label>
              <input
                type="text"
                required
                value={formData.bot_id}
                onChange={(e) => setFormData({ ...formData, bot_id: e.target.value })}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl font-mono text-xs"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-[#665E54] mb-1">展示策略名称</label>
              <input
                type="text"
                required
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl text-xs"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="block text-xs font-semibold text-[#665E54]">交易标的</label>
                <span className="text-xs text-[#DE6438] font-mono">{MAINSTREAM_SYMBOLS.length} 主流资产</span>
              </div>
              <select
                value={formData.symbol}
                onChange={(e) => {
                  const newSym = e.target.value;
                  setFormData((prev) => ({
                    ...prev,
                    symbol: newSym,
                    name: prev.name.replace(/^(ETH|BTC|SOL|[A-Z]{2,10})/, newSym),
                  }));
                }}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl text-xs font-mono font-bold"
              >
                {MAINSTREAM_SYMBOLS.map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol} · {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-[#665E54] mb-1">执行交易所</label>
              <select
                value={formData.exchange}
                onChange={(e) => setFormData({ ...formData, exchange: e.target.value })}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl text-xs font-mono font-bold"
              >
                <option value="BINANCE">BINANCE (币安)</option>
                <option value="HYPERLIQUID">HYPERLIQUID</option>
                <option value="OKX">OKX</option>
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-[#665E54] mb-1">交易市场</label>
              <select
                value={formData.market_type}
                onChange={(e) => setFormData({ ...formData, market_type: e.target.value })}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl text-xs font-mono font-bold"
              >
                <option value="SPOT">SPOT 现货</option>
                <option value="USDM_FUTURES">USDM_FUTURES U 本位合约</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-[#665E54] mb-1">运行模式</label>
              <select
                value={formData.execution_mode}
                onChange={(e) => setFormData({ ...formData, execution_mode: e.target.value })}
                className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl text-xs font-mono font-bold"
              >
                <option value="LOCAL_SIM">LOCAL_SIM 本地模拟</option>
                <option value="TESTNET">TESTNET 测试网</option>
                <option value="MAINNET">MAINNET 实盘</option>
              </select>
            </div>
          </div>

          {/* 市场规则提示 */}
          {formData.market_type === 'SPOT' ? (
            <div className="px-3 py-2 rounded-xl bg-amber-50 border border-amber-200 text-amber-800 text-xs">
              ℹ️ 现货市场提示：<strong>不可开空</strong>，不支持杠杆与 reduceOnly；仅允许买入现货及卖出已有持仓。
            </div>
          ) : (
            <div className="px-3 py-2 rounded-xl bg-[#F4EFE6] border border-[#E6DFD5] text-[#5A5046] text-xs">
              ℹ️ U 本位合约说明：支持多空双向开平仓与杠杆配置；系统将自动探测账户单向/双向持仓模式（Hedge / One-way），并仅按真实成交数量挂载原生条件止损止盈。
            </div>
          )}

          {/* StrategyRouter 4 大核心策略 */}
          <div>
            <label className="block text-xs font-semibold text-[#665E54] mb-1.5">核心决策策略模板</label>
            <div className="grid grid-cols-2 gap-2">
              {[
                { id: 'BULL_BEAR_DEBATE', label: '⚔️ 多空博弈激辩', badge: 'LLM Multi-Agent' },
                { id: 'TREND_FOLLOW', label: '📈 趋势突破跟踪', badge: '0 Token · 纯算法' },
                { id: 'GRID', label: '🔲 动态区间网格', badge: '0 Token · 纯算法' },
                { id: 'FUNDING_ARB', label: '⚖️ 资金费率套利', badge: '0 Token · 仅合约' },
              ].map((strat) => (
                <div
                  key={strat.id}
                  onClick={() => setFormData({ ...formData, strategy_type: strat.id })}
                  className={`p-2.5 rounded-xl border cursor-pointer transition ${
                    formData.strategy_type === strat.id
                      ? 'bg-white border-[#DE6438] text-[#DE6438] shadow-xs'
                      : 'bg-[#F4EFE6] border-transparent text-[#5A5046] hover:bg-white'
                  }`}
                >
                  <div className="font-bold text-xs">{strat.label}</div>
                  <div className="text-xs text-[#8C8276] mt-0.5 font-mono">{strat.badge}</div>
                </div>
              ))}
            </div>
          </div>

          {/* 策略专属超参 */}
          {activeMeta?.params && activeMeta.params.length > 0 && (
            <div className="p-3 rounded-xl bg-white border border-[#E6DFD5] space-y-2">
              <div className="text-xs font-bold text-[#2E2823]">策略专属超参 (透传至 StrategyRouter)</div>
              <div className="grid grid-cols-3 gap-2">
                {activeMeta.params.map((p) => (
                  <div key={p.key}>
                    <label className="block text-xs text-[#7E7469] mb-0.5">{p.label}</label>
                    <input
                      type="number"
                      min={p.min}
                      max={p.max}
                      step={p.step}
                      value={strategyConfig[p.key] ?? p.default}
                      onChange={(e) =>
                        setStrategyConfig((prev) => ({ ...prev, [p.key]: Number(e.target.value) }))
                      }
                      className="w-full px-2 py-1 border border-[#D8CEBE] rounded font-mono text-xs"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 实盘就绪 5 项预检清单（TESTNET / MAINNET 模式自动展示） */}
          {formData.execution_mode !== 'LOCAL_SIM' && (
            <div className="p-3.5 rounded-xl bg-white border border-[#DFD6C7] space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-[#2E2823] flex items-center space-x-1.5">
                  <ShieldCheck className="w-4 h-4 text-[#DE6438]" />
                  <span>实盘就绪预检清单 (Live Readiness Preflight)</span>
                </span>
                <span className="text-xs font-mono text-[#7E7469]">
                  {checkingReadiness ? '检测中...' : `${formData.market_type} · ${formData.execution_mode}`}
                </span>
              </div>
              {readiness?.error ? (
                <div className="text-xs text-[#C24D4D]">⚠️ 预检接口返回异常（请先完成钱包签名登录）: {readiness.error}</div>
              ) : (
                <div className="space-y-1.5">
                  {(readiness?.checks || []).map((c) => (
                    <div key={c.id} className="flex items-center justify-between text-xs bg-[#FAF7F2] px-2.5 py-1.5 rounded-lg">
                      <span className="flex items-center space-x-1.5">
                        {c.passed ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-[#3D784E]" />
                        ) : (
                          <XCircle className="w-3.5 h-3.5 text-[#C24D4D]" />
                        )}
                        <span className="font-medium text-[#2E2823]">{c.name}</span>
                      </span>
                      <span className="text-xs text-[#7E7469] font-mono">{c.detail}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 初始分配资金 */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-[#665E54]">初始分配资金 (USD)</label>
              <span className="font-mono text-xs font-bold text-[#DE6438]">
                ${Number(formData.allocated_capital).toLocaleString()} USDT
              </span>
            </div>
            <div className="flex items-center space-x-1.5 mb-2 overflow-x-auto pb-1">
              {[1000, 5000, 10000, 20000, 50000, 100000].map((amt) => (
                <button
                  key={amt}
                  type="button"
                  onClick={() => setFormData({ ...formData, allocated_capital: amt })}
                  className={`px-2 py-1 rounded-lg text-xs font-mono font-bold transition shrink-0 ${
                    Number(formData.allocated_capital) === amt
                      ? 'bg-[#DE6438] text-white shadow-xs'
                      : 'bg-[#F2EDE4] text-[#6E6458] hover:bg-white hover:text-[#DE6438] border border-[#E6DFD5]'
                  }`}
                >
                  ${amt >= 1000 ? `${amt / 1000}k` : amt}
                </button>
              ))}
            </div>
            <input
              type="number"
              min="100"
              step="100"
              value={formData.allocated_capital}
              onChange={(e) => setFormData({ ...formData, allocated_capital: Number(e.target.value) })}
              className="w-full px-3 py-2 bg-white border border-[#D8CEBE] rounded-xl font-mono text-xs"
              placeholder="自定义初始资金"
            />
          </div>

          <div className="pt-2 flex items-center justify-end space-x-2.5">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-xl border border-[#D8CEBE] bg-white text-[#5E554B] hover:bg-[#F2EDE4] font-medium transition"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={loading || blockedByReadiness}
              className="px-5 py-2 rounded-xl bg-[#DE6438] hover:bg-[#C5532A] text-white font-bold transition flex items-center space-x-1.5 shadow-sm disabled:opacity-50"
            >
              <span>{blockedByReadiness ? '实盘预检未通过' : loading ? '初始化实例中...' : '立即创建并装载席位'}</span>
              {!blockedByReadiness && <ArrowRight className="w-4 h-4" />}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
