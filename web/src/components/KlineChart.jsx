/**
 * ================================================================================
 * Heimdall 交易平台 - 实时 K 线与量化指标图表组件 (KlineChart with TradingView & Bifrost)
 * ================================================================================
 * 
 * 【功能特性】：
 * 1. 官方 TradingView 高级实时交互组件 (直连 Binance/OKX 毫秒级 Tick 数据)；
 * 2. Bifrost 零前视偏差底座 (Zero-Lookahead Canvas Engine)：
 *    - 叠加 EMA(20) 与 EMA(50) 动态均线轨道；
 *    - 下附成交量柱状分布图；
 *    - 实时盘面交易打点：在历史 K 线上高亮买入 (▲) / 卖出 (▼) 撮合成交标记点与持仓均价线。
 */

import React, { useState, useEffect, useRef } from 'react';
import { useTranslation, Trans } from 'react-i18next';
import { fmt } from '../i18n';
import { cssColor, useThemeName } from '../utils/theme';

// 计算 EMA 指数移动平均线
function computeEMA(prices, period) {
  if (!prices || prices.length < period) return [];
  const multiplier = 2 / (period + 1);
  const ema = [];
  let prev = prices.slice(0, period).reduce((a, b) => a + b, 0) / period;

  for (let i = 0; i < prices.length; i++) {
    if (i < period - 1) {
      ema.push(null);
    } else if (i === period - 1) {
      ema.push(prev);
    } else {
      prev = prices[i] * multiplier + prev * (1 - multiplier);
      ema.push(prev);
    }
  }
  return ema;
}

export default function KlineChart({ 
  symbol = "ETH", 
  interval = "15m",
  trades = [],
}) {
  const { t, i18n } = useTranslation('arena');
  // TradingView 组件语言随界面语言：zh-CN -> zh_CN，en-US -> en
  const tvLocale = (i18n.resolvedLanguage || i18n.language) === 'en-US' ? 'en' : 'zh_CN';
  const [chartEngine, setChartEngine] = useState("tradingview"); // "tradingview" | "bifrost"
  // M5：不再预置 2024 年的离线假 K 线与写死的标的价格；未取到真实数据前显示加载 / 不可用状态
  const [klines, setKlines] = useState([]);
  const themeName = useThemeName();
  // status: idle | loading | ok | empty | error；provenance: LIVE | SIMULATED | UNAVAILABLE
  const [klinesState, setKlinesState] = useState({ status: 'idle', provenance: null, message: '' });
  const [activeInterval, setActiveInterval] = useState(interval);
  const [wsConnected, setWsConnected] = useState(false);
  const [latestPrice, setLatestPrice] = useState(null);
  const [showIndicators, setShowIndicators] = useState(true);
  const [showTradeMarkers, setShowTradeMarkers] = useState(true);

  const canvasRef = useRef(null);
  const tvContainerRef = useRef(null);

  // 标的切换时清空旧标的价格，等待新标的真实行情
  useEffect(() => {
    setLatestPrice(null);
  }, [symbol]);

  // 监听币安公开行情 WebSocket，保持价格与 TradingView 实时毫秒级同频
  useEffect(() => {
    let ws = null;
    const tickerPair = `${symbol.toLowerCase()}usdt`;
    try {
      ws = new WebSocket(`wss://stream.binance.com:9443/ws/${tickerPair}@ticker`);
      ws.onopen = () => setWsConnected(true);
      ws.onclose = () => setWsConnected(false);
      ws.onerror = () => setWsConnected(false);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && data.c) {
            const p = parseFloat(data.c);
            setLatestPrice(p);
          }
        } catch (e) {}
      };
    } catch (e) {}

    return () => {
      setWsConnected(false);
      if (ws) {
        // 先解绑回调，避免旧连接延迟触发 onclose 把新连接的状态误置为“未连接”
        ws.onopen = null;
        ws.onclose = null;
        ws.onerror = null;
        ws.onmessage = null;
        ws.close();
      }
    };
  }, [symbol]);

  // TradingView 周期映射
  const intervalMap = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
  };

  // ---------------------------------------------------------------------------
  // 1. TradingView 原生专业组件动态挂载
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (chartEngine !== "tradingview") return;

    const container = tvContainerRef.current;
    if (!container) return;

    container.innerHTML = ""; // 清理既有实例

    const widgetWrapper = document.createElement("div");
    widgetWrapper.className = "tradingview-widget-container__widget";
    widgetWrapper.style.height = "100%";
    widgetWrapper.style.width = "100%";
    container.appendChild(widgetWrapper);

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.type = "text/javascript";
    script.async = true;

    const tvSymbol = `BINANCE:${symbol.toUpperCase()}USDT`;
    const tvInterval = intervalMap[activeInterval] || "15";

    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: tvSymbol,
      interval: tvInterval,
      timezone: "Etc/UTC",
      theme: themeName === "dark" ? "dark" : "light",
      style: "1",
      locale: tvLocale,
      enable_publishing: false,
      allow_symbol_change: true,
      hide_side_toolbar: false,
      calendar: false,
      support_host: "https://www.tradingview.com",
      backgroundColor: cssColor("panel"),
      gridColor: cssColor("muted"),
      studies: [
        "STD;EMA",
        "STD;RSI"
      ]
    });

    container.appendChild(script);
  }, [chartEngine, symbol, activeInterval, themeName, tvLocale]);

  // ---------------------------------------------------------------------------
  // 2. 从后端加载零前视偏差 K 线数据
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (chartEngine !== "bifrost") return;

    let isSubscribed = true;
    setKlines([]);
    setKlinesState({ status: 'loading', provenance: null, message: '' });
    fetch(`/api/v1/market/klines?symbol=${encodeURIComponent(symbol)}&interval=${activeInterval}&limit=100`)
      .then(async (res) => {
        const payload = await res.json().catch(() => null);
        if (!res.ok) {
          throw new Error((payload && payload.detail) || `HTTP ${res.status}`);
        }
        return payload;
      })
      .then((payload) => {
        if (!isSubscribed) return;
        const data = payload && Array.isArray(payload.data) ? payload.data : [];
        if (data.length === 0) {
          setKlinesState({ status: 'empty', provenance: payload?.provenance || 'UNAVAILABLE', message: '' });
          return;
        }
        setKlines(data);
        setKlinesState({ status: 'ok', provenance: payload?.provenance || 'LIVE', message: '' });
        // 离线仿真 K 线不得覆盖“最新标记价”
        const last = data[data.length - 1];
        if (payload?.provenance !== 'SIMULATED' && last && Number(last.close) > 0) {
          setLatestPrice(Number(last.close));
        }
      })
      .catch((err) => {
        if (!isSubscribed) return;
        setKlinesState({ status: 'error', provenance: 'UNAVAILABLE', message: err?.message || '' });
      });

    return () => {
      isSubscribed = false;
    };
  }, [chartEngine, symbol, activeInterval]);

  // ---------------------------------------------------------------------------
  // 3. Bifrost Canvas 渲染 (带 EMA 指标层 + 交易打点标记)
  // ---------------------------------------------------------------------------
  useEffect(() => {
    if (chartEngine !== "bifrost") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width;
    const height = canvas.height;

    ctx.clearRect(0, 0, width, height);
    if (!klines || klines.length === 0) return;

    const closePrices = klines.map((k) => k.close);
    const ema20 = computeEMA(closePrices, 20);
    const ema50 = computeEMA(closePrices, 50);

    const prices = klines.flatMap((k) => [k.high, k.low]);
    const minPrice = Math.min(...prices) * 0.998;
    const maxPrice = Math.max(...prices) * 1.002;
    const priceRange = maxPrice - minPrice || 1;

    const chartHeight = height * 0.72;
    const volumeHeight = height * 0.22;
    const maxVolume = Math.max(...klines.map((k) => k.volume || 1));

    const candleWidth = Math.max(4, (width - 60) / klines.length - 4);
    const spacing = (width - 60) / klines.length;

    const getY = (val) => chartHeight - ((val - minPrice) / priceRange) * chartHeight + 10;
    const getVolY = (vol) => height - (vol / maxVolume) * volumeHeight;

    // 绘制价格网格线
    ctx.strokeStyle = cssColor('line-soft');
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    for (let i = 1; i <= 4; i++) {
      const y = (chartHeight / 5) * i;
      ctx.beginPath();
      ctx.moveTo(10, y);
      ctx.lineTo(width - 50, y);
      ctx.stroke();

      const p = maxPrice - (priceRange / 5) * i;
      ctx.fillStyle = cssColor('ink-4');
      ctx.font = '10px monospace';
      ctx.fillText(`$${p.toFixed(1)}`, width - 46, y + 3);
    }
    ctx.setLineDash([]);

    // 绘制 K 线与成交量柱
    klines.forEach((k, index) => {
      const x = 10 + index * spacing;
      const isUp = k.close >= k.open;
      const candleColor = isUp ? cssColor('success') : cssColor('danger');

      const openY = getY(k.open);
      const closeY = getY(k.close);
      const highY = getY(k.high);
      const lowY = getY(k.low);

      const bodyTop = Math.min(openY, closeY);
      const bodyHeight = Math.max(2, Math.abs(openY - closeY));

      // 影线
      ctx.strokeStyle = candleColor;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x + candleWidth / 2, highY);
      ctx.lineTo(x + candleWidth / 2, lowY);
      ctx.stroke();

      // 实体
      ctx.fillStyle = candleColor;
      ctx.fillRect(x, bodyTop, candleWidth, bodyHeight);

      // 成交量
      const volY = getVolY(k.volume);
      ctx.fillStyle = isUp ? 'rgba(77, 131, 92, 0.35)' : 'rgba(194, 77, 77, 0.35)';
      ctx.fillRect(x, volY, candleWidth, height - volY);
    });

    // 绘制 EMA(20) 与 EMA(50) 均线
    if (showIndicators) {
      // EMA20 (橙色)
      ctx.strokeStyle = cssColor('brand');
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let startedEma20 = false;
      ema20.forEach((val, idx) => {
        if (val !== null) {
          const x = 10 + idx * spacing + candleWidth / 2;
          const y = getY(val);
          if (!startedEma20) {
            ctx.moveTo(x, y);
            startedEma20 = true;
          } else {
            ctx.lineTo(x, y);
          }
        }
      });
      ctx.stroke();

      // EMA50 (蓝色)
      ctx.strokeStyle = cssColor('info');
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let startedEma50 = false;
      ema50.forEach((val, idx) => {
        if (val !== null) {
          const x = 10 + idx * spacing + candleWidth / 2;
          const y = getY(val);
          if (!startedEma50) {
            ctx.moveTo(x, y);
            startedEma50 = true;
          } else {
            ctx.lineTo(x, y);
          }
        }
      });
      ctx.stroke();

      // 左上角指标数值图例
      const lastEma20 = ema20[ema20.length - 1];
      const lastEma50 = ema50[ema50.length - 1];
      ctx.font = '10px monospace';
      ctx.fillStyle = cssColor('brand');
      ctx.fillText(`EMA(20): $${lastEma20 ? lastEma20.toFixed(1) : '-'}`, 15, 20);
      ctx.fillStyle = cssColor('info');
      ctx.fillText(`EMA(50): $${lastEma50 ? lastEma50.toFixed(1) : '-'}`, 130, 20);
    }

    // 盘面交易打点标记 (Trade Execution Markers)
    if (showTradeMarkers && trades && trades.length > 0) {
      const recentTrades = trades.slice(0, 5);
      recentTrades.forEach((tr, i) => {
        const isBuy = (tr.side || '').toUpperCase() === 'BUY';
        const price = Number(tr.price || tr.avg_price || latestPrice);
        if (!Number.isFinite(price) || price <= 0) return;
        const y = getY(price);
        const x = width - 70 - i * 40;

        ctx.fillStyle = isBuy ? cssColor('success') : cssColor('brand');
        ctx.beginPath();
        if (isBuy) {
          // 向上买入绿色三角
          ctx.moveTo(x, y + 8);
          ctx.lineTo(x - 5, y + 16);
          ctx.lineTo(x + 5, y + 16);
        } else {
          // 向下卖出红色三角
          ctx.moveTo(x, y - 8);
          ctx.lineTo(x - 5, y - 16);
          ctx.lineTo(x + 5, y - 16);
        }
        ctx.fill();

        ctx.font = '9px sans-serif';
        ctx.fillText(isBuy ? 'BUY' : 'SELL', x - 8, isBuy ? y + 26 : y - 18);
      });
    }

  }, [chartEngine, klines, showIndicators, showTradeMarkers, trades, latestPrice, themeName]);

  return (
    <div className="w-full h-full flex flex-col bg-surface rounded-2xl border border-line shadow-xs overflow-hidden">
      
      {/* 头部控制栏 */}
      <div className="h-11 px-4 border-b border-line-soft flex items-center justify-between bg-panel">
        
        {/* 左侧：标的与引擎模式切换 */}
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1.5 font-bold text-sm text-ink">
            <span className="text-base">⚡</span>
            <span>{symbol}-USDT</span>
            <span className="text-xs bg-muted text-ink-3 px-1.5 py-0.2 rounded font-normal font-mono">
              {t('kline.perpetual')}
            </span>
          </div>

          {/* 图表引擎选择器 */}
          <div className="flex items-center bg-muted p-0.5 rounded-lg border border-line-strong">
            <button
              onClick={() => setChartEngine("tradingview")}
              className={`px-2 py-0.5 rounded text-xs font-bold transition flex items-center space-x-1 cursor-pointer ${
                chartEngine === "tradingview"
                  ? "bg-surface text-brand shadow-xs"
                  : "text-ink-3 hover:text-ink"
              }`}
            >
              <span>📈 TradingView</span>
            </button>
            <button
              onClick={() => setChartEngine("bifrost")}
              className={`px-2 py-0.5 rounded text-xs font-bold transition flex items-center space-x-1 cursor-pointer ${
                chartEngine === "bifrost"
                  ? "bg-surface text-brand shadow-xs"
                  : "text-ink-3 hover:text-ink"
              }`}
            >
              <span>{t('kline.bifrost')}</span>
            </button>
          </div>
        </div>

        {/* 中间：周期选择器 & 指标开关 */}
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1 bg-muted p-0.5 rounded-lg border border-line-strong">
            {["1m", "5m", "15m", "1h", "4h", "1d"].map((tf) => (
              <button
                key={tf}
                onClick={() => setActiveInterval(tf)}
                className={`px-2 py-0.5 rounded text-xs font-mono transition cursor-pointer ${
                  activeInterval === tf
                    ? "bg-surface text-brand font-bold shadow-xs"
                    : "text-ink-3 hover:text-ink"
                }`}
              >
                {tf}
              </button>
            ))}
          </div>

          {chartEngine === "bifrost" && (
            <div className="flex items-center space-x-2 text-xs text-ink-3">
              <label className="flex items-center space-x-1 cursor-pointer">
                <input 
                  type="checkbox" 
                  checked={showIndicators} 
                  onChange={(e) => setShowIndicators(e.target.checked)}
                  className="rounded text-brand" 
                />
                <span>{t('kline.ema')}</span>
              </label>
              <label className="flex items-center space-x-1 cursor-pointer">
                <input 
                  type="checkbox" 
                  checked={showTradeMarkers} 
                  onChange={(e) => setShowTradeMarkers(e.target.checked)}
                  className="rounded text-success" 
                />
                <span>{t('kline.tradeMarkers')}</span>
              </label>
            </div>
          )}
        </div>

        {/* 右侧：状态指示器 */}
        <div className="flex items-center space-x-2 text-xs">
          {(() => {
            // M5：状态灯反映真实连接 / 数据来源，不再常亮绿色 LIVE
            const st = chartEngine === "tradingview"
              ? (wsConnected
                  ? { dot: 'bg-success-solid animate-pulse', text: t('kline.status.tvConnected') }
                  : { dot: 'bg-ink-4', text: t('kline.status.tvDisconnected') })
              : klinesState.status === 'loading'
              ? { dot: 'bg-warning-solid animate-pulse', text: t('kline.status.loading', { interval: activeInterval }) }
              : klinesState.status === 'ok' && klinesState.provenance === 'SIMULATED'
              ? { dot: 'bg-warning-solid', text: t('kline.status.simulated', { interval: activeInterval }) }
              : klinesState.status === 'ok'
              ? { dot: 'bg-success-solid animate-pulse', text: `BIFROST · ${activeInterval} ZERO-LOOKAHEAD` }
              : { dot: 'bg-danger-solid', text: t('kline.status.unavailable', { interval: activeInterval }) };
            return (
              <div className="flex items-center space-x-1.5 px-2 py-0.5 rounded-md bg-canvas border border-line">
                <span className={`w-2 h-2 rounded-full ${st.dot}`}></span>
                <span className="font-mono text-xs text-ink-3">{st.text}</span>
              </div>
            );
          })()}
        </div>
      </div>

      {/* 图表展示区 (100% 垂直满高) */}
      <div className="flex-1 w-full min-h-0 relative bg-panel">
        {chartEngine === "tradingview" ? (
          <div 
            ref={tvContainerRef} 
            className="w-full h-full tradingview-widget-container"
          />
        ) : (
          <>
            <canvas
              ref={canvasRef}
              width={1200}
              height={600}
              className="w-full h-full block"
            />
            {klinesState.status !== 'ok' && (
              <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                <div className="px-4 py-3 rounded-xl bg-surface/90 border border-line text-xs text-ink-3 text-center shadow-xs">
                  {klinesState.status === 'loading' || klinesState.status === 'idle'
                    ? t('kline.loading')
                    : klinesState.status === 'empty'
                    ? t('kline.empty')
                    : t('kline.unavailable', { reason: klinesState.message || t('kline.networkFailed') })}
                </div>
              </div>
            )}
            {klinesState.status === 'ok' && klinesState.provenance === 'SIMULATED' && (
              <div className="absolute top-2 left-1/2 -translate-x-1/2 px-3 py-1 rounded-lg bg-warning-soft border border-warning-line text-xs font-semibold text-warning pointer-events-none">
                {t('kline.simulatedBanner')}
              </div>
            )}
          </>
        )}
      </div>

      {/* 底部行情条 */}
      <div className="h-7 px-4 bg-panel border-t border-line-soft flex items-center justify-between text-xs text-ink-3">
        <div className="flex items-center space-x-4">
          <span><Trans t={t} i18nKey="kline.markPrice" values={{ price: Number.isFinite(latestPrice) ? fmt.price(latestPrice) : '--' }} components={{ strong: <strong className="font-mono text-ink" /> }} /></span>
          <span>{t('kline.priceSource')} <span className={`font-mono ${wsConnected ? 'text-success' : 'text-ink-4'}`}>{wsConnected ? t('kline.wsLive') : t('kline.wsDisconnected')}</span></span>
        </div>
        <div className="flex items-center space-x-2">
          <span>Heimdall Zero-Lookahead Engine v2.0</span>
        </div>
      </div>

    </div>
  );
}
