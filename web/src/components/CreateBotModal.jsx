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
 *
 * 保证金模式（2026-11-24）：仅合约显示；只有支持设置保证金模式的交易所（目前为币安）在测试网 / 实盘可选，
 * 默认"沿用账户设置"（不传 margin_mode）。不可选时下拉框禁用并说明原因，提交时也不发送该字段。
 * 交易所是否支持目前在前端写死（MARGIN_MODE_EXCHANGES），接入交易所注册表接口后改为由后端返回。
 */

import React, { useState, useEffect } from 'react';
import { useTranslation, Trans } from 'react-i18next';
import { fmt } from '../i18n';
import { X, Bot, ShieldCheck, ArrowRight, CheckCircle2, XCircle } from 'lucide-react';
import { createBot, getStrategiesCatalog, checkLiveReadiness } from '../api/endpoints';
import { MAINSTREAM_SYMBOLS } from '../data/symbols';

// 适配器会在开仓前设置保证金模式的交易所（与后端 supports_margin_settings 一致）
const MARGIN_MODE_EXCHANGES = ['BINANCE'];

export default function CreateBotModal({ isOpen, onClose, onCreated, prefill = null }) {
  const { t } = useTranslation('createBot');
  const [catalog, setCatalog] = useState([]);
  const [formData, setFormData] = useState({
    bot_id: `bot_${Date.now().toString().slice(-4)}`,
    name: t('defaultName', { symbol: 'ETH' }),
    symbol: 'ETH',
    strategy_type: 'BULL_BEAR_DEBATE',
    exchange: 'BINANCE',
    market_type: 'USDM_FUTURES',
    execution_mode: 'LOCAL_SIM',
    allocated_capital: 10000.0,
    max_drawdown_limit: 0.05,
    // 空字符串 = 沿用交易所账户设置（不发送 margin_mode）
    margin_mode: '',
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
      name: t('prefillName', { symbol: prefill.symbol || 'ETH', strategy: prefill.strategy_type }),
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

  // 保证金模式：只对合约显示；本地模拟或交易所不支持时禁用
  const isFutures = formData.market_type === 'USDM_FUTURES';
  const marginDisabledReason = !isFutures
    ? null
    : formData.execution_mode === 'LOCAL_SIM'
    ? t('hints.marginLocalSim')
    : !MARGIN_MODE_EXCHANGES.includes(formData.exchange)
    ? t('hints.marginUnsupported', { exchange: formData.exchange })
    : null;
  const marginEnabled = isFutures && !marginDisabledReason;
  const effectiveMarginMode = marginEnabled ? formData.margin_mode : '';
  const marginModeName = effectiveMarginMode === 'ISOLATED' ? t('hints.isolatedName') : t('hints.crossedName');

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
        // 只在可选且明确选择时发送；不发送即沿用交易所账户设置
        ...(effectiveMarginMode ? { margin_mode: effectiveMarginMode } : {}),
      });
      onCreated && onCreated(formData.bot_id);
      onClose();
    } catch (err) {
      setError(err.message || t('errors.createFailed'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4">
      <div className="w-full max-w-xl bg-panel rounded-2xl border border-line-strong shadow-2xl overflow-hidden flex flex-col max-h-[92vh]">
        <div className="p-4 border-b border-line flex items-center justify-between bg-subtle">
          <div className="flex items-center space-x-2.5">
            <div className="p-2 bg-brand/10 text-brand rounded-xl border border-brand/20">
              <Bot className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-sm font-bold text-ink">{t('header.title')}</h3>
              <p className="text-xs text-ink-3">{t('header.subtitle')}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-ink-3 hover:bg-muted hover:text-ink transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 overflow-y-auto space-y-4 text-xs">
          {error && (
            <div className="p-2.5 rounded-xl bg-danger-soft border border-danger-line text-danger-strong text-xs">
              ⚠️ {error}
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.botId')}</label>
              <input
                type="text"
                required
                value={formData.bot_id}
                onChange={(e) => setFormData({ ...formData, bot_id: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl font-mono text-xs"
              />
            </div>
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.name')}</label>
              <input
                type="text"
                required
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="block text-xs font-semibold text-ink-2">{t('form.symbol')}</label>
                <span className="text-xs text-brand font-mono">{t('form.symbolCount', { count: MAINSTREAM_SYMBOLS.length })}</span>
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
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs font-mono font-bold"
              >
                {MAINSTREAM_SYMBOLS.map((s) => (
                  <option key={s.symbol} value={s.symbol}>
                    {s.symbol} · {s.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.exchange')}</label>
              <select
                value={formData.exchange}
                onChange={(e) => setFormData({ ...formData, exchange: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs font-mono font-bold"
              >
                <option value="BINANCE">{t('options.binance')}</option>
                <option value="HYPERLIQUID">HYPERLIQUID</option>
                <option value="OKX">OKX</option>
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.market')}</label>
              <select
                value={formData.market_type}
                onChange={(e) => setFormData({ ...formData, market_type: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs font-mono font-bold"
              >
                <option value="SPOT">{t('options.spot')}</option>
                <option value="USDM_FUTURES">{t('options.usdmFutures')}</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.mode')}</label>
              <select
                value={formData.execution_mode}
                onChange={(e) => setFormData({ ...formData, execution_mode: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs font-mono font-bold"
              >
                <option value="LOCAL_SIM">{t('options.localSim')}</option>
                <option value="TESTNET">{t('options.testnet')}</option>
                <option value="MAINNET">{t('options.mainnet')}</option>
              </select>
            </div>
          </div>

          {/* 合约保证金模式（只对合约显示；不可选时禁用并说明原因） */}
          {isFutures && (
            <div>
              <label className="block text-xs font-semibold text-ink-2 mb-1">{t('form.marginMode')}</label>
              <select
                value={effectiveMarginMode}
                disabled={!marginEnabled}
                onChange={(e) => setFormData({ ...formData, margin_mode: e.target.value })}
                className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl text-xs font-mono font-bold disabled:opacity-60 disabled:cursor-not-allowed"
              >
                <option value="">{t('options.marginAccount')}</option>
                <option value="ISOLATED">{t('options.marginIsolated')}</option>
                <option value="CROSSED">{t('options.marginCrossed')}</option>
              </select>
              <p className={`mt-1 text-xs ${effectiveMarginMode ? 'text-warning-strong' : 'text-ink-3'}`}>
                {marginDisabledReason
                  || (effectiveMarginMode
                    ? t('hints.marginSet', { mode: marginModeName })
                    : t('hints.marginAccount'))}
              </p>
            </div>
          )}

          {/* 市场规则提示 */}
          {formData.market_type === 'SPOT' ? (
            <div className="px-3 py-2 rounded-xl bg-warning-soft border border-warning-line text-warning-strong text-xs">
              <Trans t={t} i18nKey="hints.spot" components={{ strong: <strong /> }} />
            </div>
          ) : (
            <div className="px-3 py-2 rounded-xl bg-subtle border border-line text-ink-2 text-xs">
              {t('hints.futures')}
            </div>
          )}

          {/* StrategyRouter 4 大核心策略 */}
          <div>
            <label className="block text-xs font-semibold text-ink-2 mb-1.5">{t('form.strategy')}</label>
            <div className="grid grid-cols-2 gap-2">
              {[
                { id: 'BULL_BEAR_DEBATE', labelKey: 'strategies.bullBearDebate', badgeKey: 'strategies.badgeLlm' },
                { id: 'TREND_FOLLOW', labelKey: 'strategies.trendFollow', badgeKey: 'strategies.badgeAlgo' },
                { id: 'GRID', labelKey: 'strategies.grid', badgeKey: 'strategies.badgeAlgo' },
                { id: 'FUNDING_ARB', labelKey: 'strategies.fundingArb', badgeKey: 'strategies.badgeFuturesOnly' },
              ].map((strat) => (
                <div
                  key={strat.id}
                  onClick={() => setFormData({ ...formData, strategy_type: strat.id })}
                  className={`p-2.5 rounded-xl border cursor-pointer transition ${
                    formData.strategy_type === strat.id
                      ? 'bg-surface border-brand text-brand shadow-xs'
                      : 'bg-subtle border-transparent text-ink-2 hover:bg-surface'
                  }`}
                >
                  <div className="font-bold text-xs">{t(strat.labelKey)}</div>
                  <div className="text-xs text-ink-3 mt-0.5 font-mono">{t(strat.badgeKey)}</div>
                </div>
              ))}
            </div>
          </div>

          {/* 策略专属超参 */}
          {activeMeta?.params && activeMeta.params.length > 0 && (
            <div className="p-3 rounded-xl bg-surface border border-line space-y-2">
              <div className="text-xs font-bold text-ink">{t('form.params')}</div>
              <div className="grid grid-cols-3 gap-2">
                {activeMeta.params.map((p) => (
                  <div key={p.key}>
                    <label className="block text-xs text-ink-3 mb-0.5">{p.label}</label>
                    <input
                      type="number"
                      min={p.min}
                      max={p.max}
                      step={p.step}
                      value={strategyConfig[p.key] ?? p.default}
                      onChange={(e) =>
                        setStrategyConfig((prev) => ({ ...prev, [p.key]: Number(e.target.value) }))
                      }
                      className="w-full px-2 py-1 border border-line-strong rounded font-mono text-xs"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 实盘就绪 5 项预检清单（TESTNET / MAINNET 模式自动展示） */}
          {formData.execution_mode !== 'LOCAL_SIM' && (
            <div className="p-3.5 rounded-xl bg-surface border border-line-strong space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-ink flex items-center space-x-1.5">
                  <ShieldCheck className="w-4 h-4 text-brand" />
                  <span>{t('readiness.title')}</span>
                </span>
                <span className="text-xs font-mono text-ink-3">
                  {checkingReadiness ? t('readiness.checking') : `${formData.market_type} · ${formData.execution_mode}`}
                </span>
              </div>
              {readiness?.error ? (
                <div className="text-xs text-danger">{t('readiness.error', { message: readiness.error })}</div>
              ) : (
                <div className="space-y-1.5">
                  {(readiness?.checks || []).map((c) => (
                    <div key={c.id} className="flex items-center justify-between text-xs bg-panel px-2.5 py-1.5 rounded-lg">
                      <span className="flex items-center space-x-1.5">
                        {c.passed ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-success" />
                        ) : (
                          <XCircle className="w-3.5 h-3.5 text-danger" />
                        )}
                        <span className="font-medium text-ink">{c.name}</span>
                      </span>
                      <span className="text-xs text-ink-3 font-mono">{c.detail}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* 初始分配资金 */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-ink-2">{t('form.capital')}</label>
              <span className="font-mono text-xs font-bold text-brand">
                {fmt.usd(Number(formData.allocated_capital))} USDT
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
                      ? 'bg-brand text-white shadow-xs'
                      : 'bg-subtle text-ink-3 hover:bg-surface hover:text-brand border border-line'
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
              className="w-full px-3 py-2 bg-surface border border-line-strong rounded-xl font-mono text-xs"
              placeholder={t('form.capitalPlaceholder')}
            />
          </div>

          <div className="pt-2 flex items-center justify-end space-x-2.5">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-xl border border-line-strong bg-surface text-ink-2 hover:bg-subtle font-medium transition"
            >
              {t('actions.cancel')}
            </button>
            <button
              type="submit"
              disabled={loading || blockedByReadiness}
              className="px-5 py-2 rounded-xl bg-brand hover:bg-brand-hover text-white font-bold transition flex items-center space-x-1.5 shadow-sm disabled:opacity-50"
            >
              <span>{blockedByReadiness ? t('actions.blocked') : loading ? t('actions.creating') : t('actions.submit')}</span>
              {!blockedByReadiness && <ArrowRight className="w-4 h-4" />}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
