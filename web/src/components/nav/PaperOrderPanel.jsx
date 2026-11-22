import React, { useState, useEffect } from 'react';
import { useTranslation, Trans } from 'react-i18next';
import { fmt } from '../../i18n';
import { placePaperOrder, getPaperOrderQuote } from '../../api/endpoints';
import { RefreshCw } from 'lucide-react';
import { priceSourceMeta, formatUsd, formatDeviation, deviationClass } from './navFormat';

/** 快捷沙盒模拟下单：方向、金额、杠杆、可选指定成交价（与跨所实时参考价比对偏差） */
export default function PaperOrderPanel({ activeBotId, accountKnown, balance, onOrderPlaced }) {
  const { t, i18n } = useTranslation('nav');
  const locale = i18n.resolvedLanguage || i18n.language;
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

  // 读取跨所实时参考价与偏差容差（与后端下单接口同一取价规则）
  const fetchQuote = async () => {
    setQuoteLoading(true);
    try {
      const q = await getPaperOrderQuote(activeBotId);
      setQuote(q);
      setQuoteError(null);
    } catch (err) {
      setQuote(null);
      setQuoteError(err?.message || t('order.quoteFailed'));
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
          title: t('order.filled'),
          // 后端提示目前只有中文；其他语言用前端文案（后端返回结构化错误码后再统一，见多语言第 5 步）
          msg: (locale === 'zh-CN' && res.message) || t('order.filledMsg', { nav: Number(res.nav || 1.0).toFixed(4) }),
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
        await onOrderPlaced?.();
        fetchQuote();
      } else {
        dismissMs = 12000;
        setOrderStatus({
          success: false,
          title: t('order.incomplete'),
          msg: res?.detail || res?.message || t('order.incomplete')
        });
      }
    } catch (err) {
      // 按后端拒单原因分类展示：503 行情不可用 / 400 价格偏差 / 400 风控拦截 / 400 资金不足 / 其他
      dismissMs = 12000;
      const statusCode = err?.status;
      const detail = err?.message || t('order.requestError');
      let title = t('order.rejected');
      let kind = 'generic';
      if (statusCode === 503) {
        title = t('order.marketUnavailable');
        kind = 'market_unavailable';
      } else if (statusCode === 400 && detail.includes('偏离')) {
        title = t('order.priceDeviation');
        kind = 'price_deviation';
      } else if (statusCode === 400 && detail.includes('风控拦截')) {
        title = t('order.riskGuard');
        kind = 'risk_guard';
      } else if (statusCode === 400 && detail.includes('资金不足')) {
        title = t('order.insufficientFunds');
        kind = 'insufficient_funds';
      } else if (statusCode === 401 || statusCode === 403) {
        title = t('order.noPermission');
        kind = 'auth';
      } else if (statusCode === 0 || statusCode === 408) {
        title = t('order.network');
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
    fetchQuote();
    if (!useCustomPrice) return undefined;
    const timer = setInterval(fetchQuote, 15000);
    return () => clearInterval(timer);
  }, [activeBotId, useCustomPrice]);

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

  return (
    <div className="bg-surface p-5 rounded-2xl border border-line shadow-xs space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <span className="text-base">⚡</span>
          <h3 className="text-xs font-bold text-ink">
            {t('order.title')}
          </h3>
          <span className="text-xs font-mono bg-success-soft text-success border border-success-line px-2 py-0.5 rounded-full font-semibold">
            Paper Execution Engine
          </span>
        </div>
        <span className="text-xs text-ink-3">
          {t('order.subtitle')}
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
            <span>{t('order.buy')}</span>
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
            <span>{t('order.sell')}</span>
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
            <span>{t('order.close')}</span>
          </button>
        </div>

        {/* 名义金额输入与购买力控制 */}
        {orderSide !== 'CLOSE' && (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs text-ink-3 font-medium">{t('order.size')}</span>
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
                { label: t('order.max100'), ratio: 1.0 },
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
            <span className="text-xs text-ink-3 font-medium">{t('order.leverage')}</span>
            <select
              value={orderLeverage}
              onChange={(e) => setOrderLeverage(e.target.value)}
              className="bg-surface border border-line-strong rounded-lg px-2.5 py-1.5 text-xs font-mono font-bold focus:outline-none focus:border-brand"
            >
              <option value="1">{t('order.lev1')}</option>
              <option value="1.5">{t('order.lev15')}</option>
              <option value="2">{t('order.lev2')}</option>
              <option value="3">{t('order.lev3')}</option>
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
            <span>{t('order.customPrice')}</span>
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
                  placeholder={referencePrice > 0 ? String(referencePrice) : t('order.pricePlaceholder')}
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
                {t('order.useReference')}
              </button>
              {customDeviationPct !== null && (
                <span className={`text-xs font-mono font-bold ${customPriceExceeds ? 'text-danger' : deviationClass(customDeviationPct)}`}>
                  {t('order.deviation', { value: formatDeviation(customDeviationPct) })}
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
          <span>{orderLoading ? t('order.filling') : t('order.submit')}</span>
        </button>
      </div>

      {/* 实时购买力限额说明与超额告警 */}
      {orderSide !== 'CLOSE' && (
        <div className="flex flex-wrap items-center justify-between text-xs px-1 pt-0.5 text-ink-3">
          <div className="flex items-center space-x-2 font-mono">
            <span><Trans t={t} i18nKey="order.freeCashLine" values={{ amount: accountKnown ? fmt.usd(freeCash) : '--' }} components={{ strong: <strong className="text-success" /> }} /></span>
            <span>·</span>
            <span><Trans t={t} i18nKey="order.buyingPowerLine" values={{ amount: accountKnown ? fmt.usd(maxPurchasingPower, 0) : '--' }} components={{ strong: <strong className="text-ink" /> }} /></span>
          </div>
          {!accountKnown && (
            <span className="text-warning font-semibold">{t('order.accountUnavailable')}</span>
          )}
          {overBuyingPower && (
            <span className="text-danger font-semibold flex items-center space-x-1">
              <span>⚠️</span>
              <span>{t('order.overBuyingPower')}</span>
            </span>
          )}
        </div>
      )}

      {/* 跨所实时参考价 & 指定价偏差预检 */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs px-1 text-ink-3">
        <div className="flex flex-wrap items-center gap-2 font-mono">
          <span>
            {t('order.refPrice')}{' '}
            <strong className={quote?.tradable ? 'text-ink' : 'text-warning'}>
              {quote?.tradable ? formatUsd(quote.reference_price) : (quoteLoading && !quote ? t('common:loading') : t('common:unavailable'))}
            </strong>
          </span>
          {quote?.tradable && (
            <span className={`px-1.5 py-0.5 rounded border font-sans font-semibold ${priceSourceMeta(quote.reference_price_source).className}`}>
              {priceSourceMeta(quote.reference_price_source).label}
            </span>
          )}
          <span>·</span>
          <span>{t('order.tolerance', { value: formatDeviation(maxDeviationPct) })}</span>
          <button
            type="button"
            onClick={fetchQuote}
            disabled={quoteLoading}
            className="flex items-center space-x-1 text-ink-3 hover:text-brand disabled:opacity-40"
            title={t('order.refreshQuote')}
          >
            <RefreshCw className={`w-3 h-3 ${quoteLoading ? 'animate-spin' : ''}`} />
            <span className="font-sans">{t('common:refresh')}</span>
          </button>
        </div>
        {useCustomPrice && customPriceExceeds && (
          <span className="text-danger font-semibold">
            {t('order.overTolerance', { value: formatDeviation(customDeviationPct), max: formatDeviation(maxDeviationPct) })}
          </span>
        )}
        {useCustomPrice && customPrice !== '' && !customPriceValid && (
          <span className="text-danger font-semibold">{t('order.invalidPrice')}</span>
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
                  {orderStatus.title || (orderStatus.success ? t('order.filled') : t('order.rejected'))}
                  {!orderStatus.success && orderStatus.statusCode ? (
                    <span className="ml-1.5 font-mono font-normal opacity-70">HTTP {orderStatus.statusCode}</span>
                  ) : null}
                </div>
                <div className="mt-0.5 font-medium break-words">{orderStatus.msg}</div>
                {orderStatus.kind === 'market_unavailable' && (
                  <div className="mt-1 text-xs opacity-80">
                    {t('order.marketUnavailableHint')}
                  </div>
                )}
                {orderStatus.kind === 'price_deviation' && (
                  <div className="mt-1 text-xs opacity-80">
                    {t('order.deviationHint')}
                  </div>
                )}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setOrderStatus(null)}
              className="shrink-0 opacity-60 hover:opacity-100 font-mono"
              aria-label={t('common:dismiss')}
            >
              ✕
            </button>
          </div>

          {/* M2：成交价格核对明细（成交价 / 价格来源 / 跨所参考价 / 偏差） */}
          {orderStatus.success && orderStatus.pricing && (
            <div className="mt-2 pt-2 border-t border-success-line grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs text-ink-2">
              <div>
                <div className="text-ink-3">{t('order.fillPrice')}</div>
                <div className="font-mono font-bold text-ink">{formatUsd(orderStatus.pricing.fillPrice)}</div>
              </div>
              <div>
                <div className="text-ink-3">{t('order.priceSource')}</div>
                <span className={`inline-block mt-0.5 px-1.5 py-0.5 rounded border font-semibold ${priceSourceMeta(orderStatus.pricing.priceSource).className}`}>
                  {priceSourceMeta(orderStatus.pricing.priceSource).label}
                </span>
              </div>
              <div>
                <div className="text-ink-3">{t('order.refPriceLabel')}</div>
                <div className="font-mono font-bold text-ink">
                  {formatUsd(orderStatus.pricing.referencePrice)}
                </div>
                {orderStatus.pricing.referenceSource && orderStatus.pricing.referenceSource !== 'LIVE' && (
                  <div className="text-xs text-warning">{priceSourceMeta(orderStatus.pricing.referenceSource).label}</div>
                )}
              </div>
              <div>
                <div className="text-ink-3">{t('order.deviationLabel')}</div>
                <div className={`font-mono font-bold ${deviationClass(orderStatus.pricing.deviationPct)}`}>
                  {formatDeviation(orderStatus.pricing.deviationPct)}
                </div>
              </div>
            </div>
          )}
          {orderStatus.success && orderStatus.pricing?.priceSource === 'SIMULATED' && (
            <div className="mt-2 text-xs text-warning font-semibold">
              {t('order.simulatedWarn')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
