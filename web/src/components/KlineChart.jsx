/**
 * ================================================================================
 * Heimdall 交易平台 - 实时 K 线与盘口蜡烛图组件 (KlineCandleChart)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 1. 什么是 K 线（蜡烛图）？
 *    - 每一根蜡烛代表一段时间（比如 15 分钟）内的 4 个关键价格：
 *      * Open (开盘价)、High (最高价)、Low (最低价)、Close (收盘价)；
 *      * 收盘价 > 开盘价：收阳线（绿柱/红柱，这里采用国际通用的绿色代表上涨 🟢）；
 *      * 收盘价 < 开盘价：收阴线（红色代表下跌 🔴）；
 *      * 上下细线叫“影线”，代表多空争夺曾到达的最高和最低边界。
 * 2. 为什么需要 WebSocket (WS) 数据流？
 *    - 普通 HTTP 接口每次看行情都要发一次请求，太慢；
 *    - WebSocket 是一条永远不断开的双向电话线，交易所每一笔几毫秒的最新成交，
 *      都会实时推送到你的屏幕上，蜡烛图随之实时跳动！
 */

import React, { useState, useEffect, useRef } from 'react';
import { INITIAL_KLINES } from '../data/mockData';

export default function KlineChart({ symbol = "ETH", interval = "15m" }) {
  const [klines, setKlines] = useState(INITIAL_KLINES);
  const [activeInterval, setActiveInterval] = useState(interval);
  const [wsConnected, setWsConnected] = useState(false);
  const [hoveredCandle, setHoveredCandle] = useState(null);
  const [latestPrice, setLatestPrice] = useState(3124.50);
  const [priceChangePct, setPriceChangePct] = useState(2.45);
  const [tickDirection, setTickDirection] = useState("up"); // "up" or "down"
  const canvasRef = useRef(null);

  // ---------------------------------------------------------------------------
  // 1. WebSocket 实时数据流与断线自愈重连机制 (WebSocket Feed)
  // ---------------------------------------------------------------------------
  useEffect(() => {
    let ws = null;
    let timer = null;
    let isSubscribed = true;

    const connectWebSocket = () => {
      try {
        // 尝试连接后端 WebSocket 网关 (Bifrost 或 FastAPI WS 路由)
        const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/klines?symbol=${symbol}&interval=${activeInterval}`;
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
          if (!isSubscribed) return;
          setWsConnected(true);
          console.log(`[WebSocket] 成功连接至 Bifrost 行情通道 | ${symbol} ${activeInterval}`);
        };

        ws.onmessage = (event) => {
          if (!isSubscribed) return;
          try {
            const data = JSON.parse(event.data);
            if (data && data.price) {
              setLatestPrice(data.price);
              setTickDirection(data.price >= latestPrice ? "up" : "down");
            }
            if (data && data.kline) {
              setKlines((prev) => [...prev.slice(1), data.kline]);
            }
          } catch (err) {
            console.error("[WebSocket] 数据解析异常:", err);
          }
        };

        ws.onerror = () => {
          setWsConnected(false);
        };

        ws.onclose = () => {
          setWsConnected(false);
        };
      } catch (e) {
        setWsConnected(false);
      }
    };

    connectWebSocket();

    // 备用模拟器：若未启动真实后端 WS，启动毫秒级平滑价格跳动模拟器，确保界面活跃
    timer = setInterval(() => {
      if (!wsConnected) {
        setLatestPrice((prev) => {
          const delta = (Math.random() - 0.49) * 1.5;
          const next = +(prev + delta).toFixed(2);
          setTickDirection(delta >= 0 ? "up" : "down");

          // 同步更新最后一根 K 线的收盘价与最高最低
          setKlines((old) => {
            if (!old.length) return old;
            const last = { ...old[old.length - 1] };
            last.close = next;
            last.high = Math.max(last.high, next);
            last.low = Math.min(last.low, next);
            return [...old.slice(0, -1), last];
          });

          return next;
        });
      }
    }, 1800);

    return () => {
      isSubscribed = false;
      if (ws) ws.close();
      if (timer) clearInterval(timer);
    };
  }, [symbol, activeInterval, wsConnected]);

  // ---------------------------------------------------------------------------
  // 2. HTML5 Canvas 高性能 K 线与均线渲染引擎 (Canvas Renderer)
  // ---------------------------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.width;
    const height = canvas.height;

    // 清空画布
    ctx.clearRect(0, 0, width, height);

    if (!klines || klines.length === 0) return;

    // 计算最高价与最低价范围 (Scale)
    const prices = klines.flatMap((k) => [k.high, k.low]);
    const minPrice = Math.min(...prices) * 0.998;
    const maxPrice = Math.max(...prices) * 1.002;
    const priceRange = maxPrice - minPrice || 1;

    // 主 K 线区域占比 75%，副图成交量占比 25%
    const chartHeight = height * 0.72;
    const volumeHeight = height * 0.22;
    const maxVolume = Math.max(...klines.map((k) => k.volume || 1));

    const candleWidth = Math.max(4, (width - 60) / klines.length - 4);
    const spacing = (width - 60) / klines.length;

    // 坐标映射辅助函数
    const getY = (val) => chartHeight - ((val - minPrice) / priceRange) * chartHeight + 10;
    const getVolY = (vol) => height - (vol / maxVolume) * volumeHeight;

    // 绘制横向网格参考线
    ctx.strokeStyle = '#EDE6DC';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    for (let i = 1; i <= 4; i++) {
      const y = (chartHeight / 5) * i;
      ctx.beginPath();
      ctx.moveTo(10, y);
      ctx.lineTo(width - 50, y);
      ctx.stroke();

      // 右侧标尺价格
      const p = maxPrice - (priceRange / 5) * i;
      ctx.fillStyle = '#9E917F';
      ctx.font = '10px monospace';
      ctx.fillText(`$${p.toFixed(1)}`, width - 46, y + 3);
    }
    ctx.setLineDash([]);

    // 计算简易 EMA20 (平滑移动平均线)
    const ema20Points = [];
    let kValue = 2 / (20 + 1);
    let prevEma = klines[0].close;
    klines.forEach((k, idx) => {
      const ema = idx === 0 ? k.close : k.close * kValue + prevEma * (1 - kValue);
      prevEma = ema;
      ema20Points.push({ x: 10 + idx * spacing + candleWidth / 2, y: getY(ema) });
    });

    // 绘制 EMA20 黄色均线
    ctx.beginPath();
    ctx.strokeStyle = '#D48B38';
    ctx.lineWidth = 1.5;
    ema20Points.forEach((pt, i) => {
      if (i === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
    ctx.stroke();

    // 逐根绘制蜡烛线 (Candlestick Bars)
    klines.forEach((k, index) => {
      const x = 10 + index * spacing;
      const isUp = k.close >= k.open;
      const candleColor = isUp ? '#4D835C' : '#C24D4D';
      const candleFill = isUp ? '#EBF5EE' : '#FCECE8';

      const openY = getY(k.open);
      const closeY = getY(k.close);
      const highY = getY(k.high);
      const lowY = getY(k.low);

      const bodyTop = Math.min(openY, closeY);
      const bodyHeight = Math.max(2, Math.abs(openY - closeY));

      // 1. 绘制上下影线
      ctx.strokeStyle = candleColor;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x + candleWidth / 2, highY);
      ctx.lineTo(x + candleWidth / 2, lowY);
      ctx.stroke();

      // 2. 绘制蜡烛实体矩形
      ctx.fillStyle = isUp ? '#4D835C' : '#C24D4D';
      ctx.fillRect(x, bodyTop, candleWidth, bodyHeight);

      // 3. 绘制副图成交量柱状图 (Volume)
      const volY = getVolY(k.volume);
      ctx.fillStyle = isUp ? 'rgba(77, 131, 92, 0.4)' : 'rgba(194, 77, 77, 0.4)';
      ctx.fillRect(x, volY, candleWidth, height - volY);
    });
  }, [klines]);

  return (
    <div className="w-full h-full flex flex-col bg-white rounded-2xl border border-[#E6DFD5] shadow-xs overflow-hidden">
      
      <!-- 头部控制栏：标的信息、价格、涨跌幅、周期切换、WS 状态 -->
      <div className="h-11 px-4 border-b border-[#F0EAE1] flex items-center justify-between bg-[#FAF7F2]">
        
        <!-- 左侧：标的与动态价格跳动 -->
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1.5 font-bold text-sm text-[#2E2823]">
            <span className="text-base">⚡</span>
            <span>{symbol}-USDT</span>
            <span className="text-[10px] bg-[#EFE9DF] text-[#7E7469] px-1.5 py-0.2 rounded font-normal font-mono">
              永续合约
            </span>
          </div>

          <div className="flex items-baseline space-x-1.5 font-mono">
            <span
              className={`text-base font-bold transition-colors duration-300 ${
                tickDirection === "up" ? "text-[#4D835C]" : "text-[#C24D4D]"
              }`}
            >
              ${latestPrice.toLocaleString('en-US', { minimumFractionDigits: 2 })}
            </span>
            <span
              className={`text-xs font-semibold px-1 rounded ${
                priceChangePct >= 0
                  ? "bg-[#EBF5EE] text-[#4D835C]"
                  : "bg-[#FCECE8] text-[#C24D4D]"
              }`}
            >
              {priceChangePct >= 0 ? "+" : ""}{priceChangePct}%
            </span>
          </div>
        </div>

        <!-- 中间：K 线周期选择器 (Timeframe Pills) -->
        <div className="flex items-center space-x-1 bg-[#EFE9DF] p-0.5 rounded-lg border border-[#DFD6C7]">
          {["1m", "5m", "15m", "1h", "4h", "1d"].map((tf) => (
            <button
              key={tf}
              onClick={() => setActiveInterval(tf)}
              className={`px-2 py-0.5 rounded text-[11px] font-mono transition ${
                activeInterval === tf
                  ? "bg-white text-[#DE6438] font-bold shadow-xs"
                  : "text-[#7E7469] hover:text-[#2E2823]"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        <!-- 右侧：WebSocket 连接状态指示器 -->
        <div className="flex items-center space-x-2 text-xs">
          <div className="flex items-center space-x-1.5 px-2 py-0.5 rounded-md bg-[#FAF4EB] border border-[#E5DACB]">
            <span
              className={`w-2 h-2 rounded-full ${
                wsConnected
                  ? "bg-[#4D835C] animate-pulse"
                  : "bg-[#D48B38] animate-ping"
              }`}
            ></span>
            <span className="font-mono text-[10px] text-[#7E7469]">
              {wsConnected ? "WS: LIVE STREAM" : "WS: SIMULATED (0.5s)"}
            </span>
          </div>
          <span className="text-[11px] text-[#9E917F] font-mono">EMA20: $3,108.5</span>
        </div>
      </div>

      <!-- K 线画布主体 (HTML5 Canvas) -->
      <div className="flex-1 w-full relative p-2 bg-[#FCFAF7]">
        <canvas
          ref={canvasRef}
          width={640}
          height={320}
          className="w-full h-full block cursor-crosshair"
        />

        <!-- 图表左上角图例 -->
        <div className="absolute top-3 left-4 flex space-x-3 text-[10px] font-mono text-[#7E7469] pointer-events-none">
          <span className="flex items-center space-x-1">
            <span className="w-2 h-0.5 bg-[#D48B38]"></span>
            <span>EMA(20): 3108.50</span>
          </span>
          <span className="flex items-center space-x-1">
            <span className="w-2 h-2 bg-[#4D835C]/50"></span>
            <span>Vol: 1,850 ETH</span>
          </span>
        </div>
      </div>

      <!-- 底部辅助状态栏 -->
      <div className="h-6 px-4 border-t border-[#F0EAE1] bg-[#FAF7F2] flex items-center justify-between text-[10px] text-[#9E917F] font-mono">
        <span>数据底座: Bifrost Hub (Zero-Lookahead Closed Klines)</span>
        <span>时区: UTC+0 · 实时撮合沙盒</span>
      </div>
    </div>
  );
}
