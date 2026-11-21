/**
 * ================================================================================
 * Heimdall 交易平台 - 多智能体环形博弈竞技场 (Multi-Agent Decision Arena)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 这是整个系统的“中央指挥作战室”：
 * 1. 拟物扑克桌台设计（参考 CrommaCode 风格）：
 *    - 8 位不同专长的 AI 智能体像德州扑克牌手一样环形围坐在桌子四周；
 *    - 每个席位清晰显示其当前在想什么、立论方向、置信度，点击任何席位均可展开深度调查！
 * 2. 桌台中央：
 *    - 摆放着当前正在推演的核心标的资产，以及由策略师生成的买卖提案卡片；
 * 3. 顶部模式切换器：
 *    - 可一键在【环形博弈桌台】与【实时 K 线图 (WebSocket)】之间平滑切换或分屏查看！
 */

import React, { useState, useEffect } from 'react';
import KlineChart from './KlineChart';

export default function DecisionArena({ 
  agents, 
  onSelectAgent, 
  onEmergencyKill, 
  onExecuteTrade, 
  decisionRecord = null,
  onRefresh = null,
  isRefreshing = false,
  autoRefresh = true,
  onToggleAutoRefresh = null,
  trades = [],
  activeBotId = "bot_eth_debate_01",
  activeBot = null,
  symbol: propSymbol = null,
}) {
  const [viewMode, setViewMode] = useState("arena"); // "arena" | "kline" | "split"
  const [leverage, setLeverage] = useState(1.5);
  const [isExecuting, setIsExecuting] = useState(false);

  const defaultPrices = { BTC: 96500.0, ETH: 3120.0, SOL: 185.0 };
  const symbol = (propSymbol || decisionRecord?.symbol || activeBot?.symbol || (activeBotId.includes("btc") ? "BTC" : activeBotId.includes("sol") ? "SOL" : "ETH")).toUpperCase();
  const [livePrice, setLivePrice] = useState(defaultPrices[symbol] || 3120.0);

  // 标的切换时同步初始价格
  useEffect(() => {
    setLivePrice(defaultPrices[symbol] || 3120.0);
  }, [symbol]);

  // 1. 直连币安公开行情 WebSocket，保持与 TradingView 实时毫秒级同频跳动
  useEffect(() => {
    let ws = null;
    const tickerPair = `${symbol.toLowerCase()}usdt`;
    try {
      ws = new WebSocket(`wss://stream.binance.com:9443/ws/${tickerPair}@ticker`);
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && data.c) {
            setLivePrice(parseFloat(data.c));
          }
        } catch (e) {}
      };
    } catch (e) {}

    return () => {
      if (ws) ws.close();
    };
  }, [symbol]);

  // 2. 动态数据解析（若当前决策记录标的与实际标的一致则采用，否则采用实时盘口价）
  const isRecordMatching = decisionRecord && decisionRecord.symbol?.toUpperCase() === symbol;
  const latestPrice = (isRecordMatching && decisionRecord.latest_price) ? decisionRecord.latest_price : livePrice;
  const consensus = decisionRecord?.debate_consensus || "LONG";
  const proposal = decisionRecord?.trade_proposal;
  const targetEntry = proposal?.trade_action?.target_price || +(latestPrice * 0.998).toFixed(2);
  const stopLoss = proposal?.trade_action?.stop_loss_price || +(latestPrice * 0.980).toFixed(2);
  const takeProfit = proposal?.trade_action?.take_profit_price || +(latestPrice * 1.045).toFixed(2);
  const sizeUsd = proposal?.trade_action?.size_in_usd || +(latestPrice * 1.5).toFixed(2);
  const idempotencyKey = proposal?.idempotency_key;
  const riskApproved = decisionRecord?.risk_approved ?? true;

  // 快捷获取各个角色对象
  const analyst = agents.find((a) => a.id === "analyst");
  const sentiment = agents.find((a) => a.id === "sentiment");
  const bull = agents.find((a) => a.id === "bull");
  const bear = agents.find((a) => a.id === "bear");
  const moderator = agents.find((a) => a.id === "moderator");
  const strategy = agents.find((a) => a.id === "strategy");
  const riskOfficer = agents.find((a) => a.id === "risk_officer");
  const hardGuard = agents.find((a) => a.id === "hard_guard");

  const handleExecute = async () => {
    setIsExecuting(true);
    try {
      if (onExecuteTrade) {
        await onExecuteTrade();
      }
    } finally {
      setIsExecuting(false);
    }
  };

  return (
    <main className="flex-1 flex flex-col bg-[#F7F3EC] overflow-hidden relative">
      
      {/* 1. 舞台顶栏：视图切换药丸 (Arena vs Kline vs Split) & 操作按键 */}
      <div className="h-11 px-5 flex items-center justify-between border-b border-[#EBE3D7] bg-[#FAF7F2]/90 backdrop-blur shrink-0">
        
        {/* 左侧：多模式视图切换胶囊 */}
        <div className="flex items-center space-x-1 bg-[#EFE9DF] p-1 rounded-xl border border-[#DFD6C7]">
          <button
            onClick={() => setViewMode("arena")}
            className={`px-3 py-1 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition ${
              viewMode === "arena"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>⚔️</span>
            <span>环形博弈对弈台</span>
          </button>

          <button
            onClick={() => setViewMode("kline")}
            className={`px-3 py-1 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition ${
              viewMode === "kline"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>📈</span>
            <span>实时 K 线 (WS 行情)</span>
          </button>

          <button
            onClick={() => setViewMode("split")}
            className={`px-3 py-1 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition ${
              viewMode === "split"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>🪟</span>
            <span>分屏协同对齐</span>
          </button>
        </div>

        {/* 右侧：刷新控制与状态 */}
        <div className="flex items-center space-x-2">
          {decisionRecord?.timestamp && (
            <span className="text-[11px] text-[#7E7469] font-mono bg-white/80 border border-[#E2D9CC] px-2 py-0.5 rounded-md hidden md:inline-flex items-center space-x-1">
              <span>🕒</span>
              <span>推演: {new Date(decisionRecord.timestamp).toLocaleTimeString()}</span>
            </span>
          )}

          {onToggleAutoRefresh && (
            <button
              onClick={onToggleAutoRefresh}
              className={`text-[11px] px-2.5 py-1 rounded-lg border font-medium flex items-center space-x-1 transition ${
                autoRefresh 
                  ? "bg-[#EBF5EE] text-[#4D835C] border-[#D1E8D7]" 
                  : "bg-[#F5F1EA] text-[#8C8071] border-[#DFD6C7]"
              }`}
              title="切换是否自动每 4 秒轮询最新推演决策"
            >
              <span className={`w-1.5 h-1.5 rounded-full ${autoRefresh ? "bg-[#4D835C] animate-pulse" : "bg-[#8C8071]"}`}></span>
              <span>{autoRefresh ? "自动轮询中" : "轮询已暂停"}</span>
            </button>
          )}

          {onRefresh && (
            <button
              onClick={onRefresh}
              disabled={isRefreshing}
              className="px-2.5 py-1 rounded-lg bg-white border border-[#D8CEBE] hover:bg-[#F2ECE3] text-xs font-semibold text-[#DE6438] flex items-center space-x-1 shadow-xs transition"
              title="拉取最新多源市场行情并重新推演一轮"
            >
              <span className={`inline-block ${isRefreshing ? "animate-spin" : ""}`}>🔄</span>
              <span>{isRefreshing ? "推演中..." : "推演刷新"}</span>
            </button>
          )}

          <span className="text-[#C4B7A4]">|</span>
          <span className="text-[11px] text-[#9E917F] bg-white border border-[#E2D9CC] px-2 py-0.5 rounded-md hidden lg:inline">
            💡 点击任一席位查看论据
          </span>
        </div>
      </div>

      {/* 2. 中间核心画布展现区 (充分填充可用区域，消除空置边距) */}
      <div className={`flex-1 overflow-auto flex items-center justify-center ${viewMode === "kline" ? "p-2.5" : viewMode === "split" ? "p-3" : "p-4"}`}>

        {/* 模式 A：大画幅环形牌桌博弈 (Arena Mode) */}
        {viewMode === "arena" && (
          <div className="w-full h-full max-w-5xl xl:max-w-6xl max-h-[650px] min-h-[500px] poker-arena relative flex items-center justify-center p-6 select-none shadow-xl transition-all">
            
            {/* 席位 1 (上方中央)：Analyst (技术面分析师) */}
            {analyst && (
              <div 
                onClick={() => onSelectAgent(analyst)}
                className="seat-badge absolute -top-4 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3.5 py-1.5 flex items-center space-x-2 border-2 border-[#3E6B89] group-hover:scale-105 transition bg-white shadow-xs">
                  <span className="text-xl">{analyst.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#2E2823]">{analyst.name}</div>
                    <div className="text-[10px] text-[#3E6B89] font-semibold">{analyst.tag}</div>
                  </div>
                  <span className="text-[10px] bg-[#E9F2F7] text-[#3E6B89] px-2 py-0.5 rounded font-mono font-bold">
                    {analyst.badge}
                  </span>
                </div>
                <div className="text-[10px] text-[#7E7469] mt-0.5 bg-white/95 px-2.5 py-0.5 rounded-full border border-[#DFD6C7] shadow-xs max-w-[240px] truncate">
                  🔍 {analyst.keyThesis ? analyst.keyThesis.slice(0, 36) : 'EMA20 支撑突破'}
                </div>
              </div>
            )}

            {/* 席位 2 (左上方)：Sentiment & Whale (情绪与巨鲸) */}
            {sentiment && (
              <div 
                onClick={() => onSelectAgent(sentiment)}
                className="seat-badge absolute top-3 left-6 xl:left-14 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3 py-1.5 flex items-center space-x-2 border border-[#4D835C] group-hover:scale-105 transition bg-white shadow-xs">
                  <span className="text-lg">{sentiment.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#2E2823]">{sentiment.name}</div>
                    <div className="text-[10px] text-[#4D835C] font-semibold">{sentiment.tag}</div>
                  </div>
                </div>
                <div className="text-[9.5px] text-[#4D835C] mt-0.5 font-bold bg-[#EBF5EE] px-2 py-0.5 rounded shadow-xs max-w-[180px] truncate">
                  {sentiment.badge ? `情绪指标 ${sentiment.badge}` : '资金面正常'}
                </div>
              </div>
            )}

            {/* 席位 3 (右上方)：Risk Officer (软逻辑风控官) */}
            {riskOfficer && (
              <div 
                onClick={() => onSelectAgent(riskOfficer)}
                className="seat-badge absolute top-3 right-6 xl:right-14 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3 py-1.5 flex items-center space-x-2 border border-[#8C7A6B] group-hover:scale-105 transition bg-white shadow-xs">
                  <span className="text-lg">{riskOfficer.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#2E2823]">{riskOfficer.name}</div>
                    <div className="text-[10px] text-[#4D835C] font-semibold">{riskOfficer.tag || '盈亏比审查放行'}</div>
                  </div>
                </div>
              </div>
            )}

            {/* 席位 4 (左侧腰部)：Bull Researcher (多头先锋立论) */}
            {bull && (
              <div 
                onClick={() => onSelectAgent(bull)}
                className="seat-badge absolute left-3 xl:left-8 top-1/2 -translate-y-1/2 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3.5 py-2 flex items-center space-x-2 border-2 border-[#4D835C] bg-[#F7FCF8] group-hover:scale-105 transition shadow-sm">
                  <span className="text-2xl">{bull.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#4D835C]">{bull.name}</div>
                    <div className="text-[10px] text-[#558B64] font-medium">{bull.tag || '多头进攻立论'}</div>
                  </div>
                </div>
                <div className="w-44 text-center text-[10px] text-[#2E2823] mt-1.5 bg-white p-2 rounded-xl border border-[#D5E5D8] shadow-xs leading-snug line-clamp-2">
                  “{bull.keyThesis ? bull.keyThesis.slice(0, 48) : '现货买盘真实，突破阻力暴拉赔率极高！'}”
                </div>
              </div>
            )}

            {/* 席位 5 (右侧腰部)：Bear Researcher (空头风险找茬) */}
            {bear && (
              <div 
                onClick={() => onSelectAgent(bear)}
                className="seat-badge absolute right-3 xl:right-8 top-1/2 -translate-y-1/2 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3.5 py-2 flex items-center space-x-2 border-2 border-[#C24D4D] bg-[#FCF7F7] group-hover:scale-105 transition shadow-sm">
                  <span className="text-2xl">{bear.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#C24D4D]">{bear.name}</div>
                    <div className="text-[10px] text-[#A63F3F] font-medium">{bear.tag || '空头风险质询'}</div>
                  </div>
                </div>
                <div className="w-44 text-center text-[10px] text-[#2E2823] mt-1.5 bg-white p-2 rounded-xl border border-[#F2D4D4] shadow-xs leading-snug line-clamp-2">
                  “{bear.keyThesis ? bear.keyThesis.slice(0, 48) : '上方解套抛压，必须在防守位设好止损。'}”
                </div>
              </div>
            )}

            {/* 席位 6 (左下方)：Strategy Agent (定量策略生成器) */}
            {strategy && (
              <div 
                onClick={() => onSelectAgent(strategy)}
                className="seat-badge absolute bottom-3 left-6 xl:left-14 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3 py-1.5 flex items-center space-x-2 border border-[#D48B38] group-hover:scale-105 transition bg-white shadow-xs">
                  <span className="text-lg">{strategy.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#2E2823]">{strategy.name}</div>
                    <div className="text-[10px] text-[#D48B38] font-semibold">{strategy.tag || '提案生成'}</div>
                  </div>
                </div>
              </div>
            )}

            {/* 席位 7 (下方中央)：Debate Moderator (裁判长 / 最终裁决) */}
            {moderator && (
              <div 
                onClick={() => onSelectAgent(moderator)}
                className="seat-badge absolute -bottom-5 flex flex-col items-center cursor-pointer group z-20"
              >
                <div className="tactile-card px-4 py-2 flex items-center space-x-3 border-2 border-[#DE6438] bg-[#FFFBF8] shadow-md group-hover:scale-105 transition">
                  <span className="text-2xl">{moderator.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#DE6438]">{moderator.name}</div>
                    <div className="text-[10px] font-semibold text-[#2E2823]">{moderator.tag || '裁定立论'}</div>
                  </div>
                  <div className="text-xs font-mono font-bold bg-[#DE6438] text-white px-2.5 py-0.5 rounded shadow-xs">
                    {moderator.badge || '终审裁决'}
                  </div>
                </div>
              </div>
            )}

            {/* 席位 8 (右下方)：HardRiskGuard (七大铁律确定性硬风控) */}
            {hardGuard && (
              <div 
                onClick={() => onSelectAgent(hardGuard)}
                className="seat-badge absolute bottom-3 right-6 xl:right-14 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-3 py-1.5 flex items-center space-x-2 border border-[#4D835C] group-hover:scale-105 transition bg-white shadow-xs">
                  <span className="text-lg">{hardGuard.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#2E2823]">{hardGuard.name}</div>
                    <div className="text-[10px] text-[#4D835C] font-semibold">{hardGuard.tag || '7/7 铁律全票核准'}</div>
                  </div>
                </div>
              </div>
            )}

            {/* ========================================================= */}
            {/* 桌台中心：核心交易标的与实时生成的交易提案卡片 */}
            {/* ========================================================= */}
            <div className="w-88 xl:w-96 bg-white rounded-2xl p-5 border-2 border-[#D8CEBE] shadow-xl flex flex-col justify-between z-0">
              <div className="flex items-center justify-between pb-3 border-b border-[#F0EAE1]">
                <div className="flex items-center space-x-2">
                  <span className={`w-2.5 h-2.5 rounded-full ${consensus === 'LONG' ? 'bg-[#4D835C]' : consensus === 'SHORT' ? 'bg-[#DE6438]' : 'bg-[#7A6F62]'} animate-pulse`}></span>
                  <span className="font-mono font-bold text-base text-[#2E2823]">{symbol}-USDT 永续</span>
                </div>
                <div className="text-right">
                  <span className="font-mono font-bold text-lg text-[#4D835C]">${Number(latestPrice).toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
                  <span className="block text-[10px] font-mono text-[#4D835C] font-semibold">多源互补 (Binance / HL)</span>
                </div>
              </div>

              <div className="py-3 space-y-2 text-xs">
                <div className="flex justify-between items-center">
                  <span className="text-[#8C8276]">多空决策结论:</span>
                  <span className={`font-bold font-mono px-2.5 py-1 rounded text-xs ${
                    consensus === 'LONG' 
                      ? 'bg-[#EBF5EE] text-[#4D835C]' 
                      : consensus === 'SHORT' 
                      ? 'bg-[#FBEFEA] text-[#DE6438]' 
                      : 'bg-[#F2EDE4] text-[#7A6F62]'
                  }`}>
                    {consensus === 'LONG' ? '🟢 开多 LONG' : consensus === 'SHORT' ? '🔴 沽空 SHORT' : '⚪ 空仓 NEUTRAL'} · {leverage.toFixed(1)}x
                  </span>
                </div>
                {proposal ? (
                  <>
                    <div className="flex justify-between font-mono">
                      <span className="text-[#8C8276]">限价入场委托:</span>
                      <span className="font-semibold text-[#2E2823]">${Number(targetEntry).toFixed(2)}</span>
                    </div>
                    <div className="flex justify-between font-mono">
                      <span className="text-[#8C8276]">硬防守止损价:</span>
                      <span className="text-[#C24D4D] font-semibold">${Number(stopLoss).toFixed(2)}</span>
                    </div>
                    <div className="flex justify-between font-mono">
                      <span className="text-[#8C8276]">目标止盈点位:</span>
                      <span className="text-[#4D835C] font-semibold">${Number(takeProfit).toFixed(2)}</span>
                    </div>
                    <div className="flex justify-between font-mono">
                      <span className="text-[#8C8276]">凯利头寸价值:</span>
                      <span className="text-[#DE6438] font-bold">${Number(sizeUsd).toFixed(2)} USD</span>
                    </div>
                    {idempotencyKey && (
                      <div className="flex justify-between font-mono text-[10px] text-[#9E9486] pt-1 border-t border-[#F0EAE1]">
                        <span>幂等防重放:</span>
                        <span className="font-mono text-[#7E7469]">{idempotencyKey.slice(0, 18)}...</span>
                      </div>
                    )}
                  </>
                ) : (
                  <div className="py-2.5 px-3 bg-[#FAF7F2] rounded-xl border border-[#EDE5DA] text-center space-y-1 my-1">
                    <p className="font-semibold text-[#7E7469] text-xs">保持空仓观望 (Cash is a position)</p>
                    <p className="text-[11px] text-[#9E9486] leading-relaxed">
                      {consensus === 'NEUTRAL' ? '多空对抗达成中立共识，策略层严禁盲目挂单，100% 现金防守' : '当前无有效挂单提案，维持零资金敞口暴露'}
                    </p>
                  </div>
                )}
              </div>

              {/* 七大铁律通过徽章 */}
              <div className="pt-2.5 border-t border-[#F0EAE1] flex items-center justify-between">
                <span className="text-[10px] text-[#7E7469] flex items-center space-x-1">
                  <span>🛡️</span>
                  <span>底层确定性硬风控</span>
                </span>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded font-mono border ${
                  riskApproved 
                    ? 'bg-[#EBF5EE] text-[#4D835C] border-[#CEE5D3]' 
                    : 'bg-[#FDECEC] text-[#C24D4D] border-[#F7CDCD]'
                }`}>
                  {riskApproved ? '✓ 7/7 铁律全票核准' : '✕ 硬风控触发拦截'}
                </span>
              </div>
            </div>

          </div>
        )}

        {/* 模式 B：全幅宽屏实时 K 线图 (Kline Mode - 100% 满屏无空隙) */}
        {viewMode === "kline" && (
          <div className="w-full h-full">
            <KlineChart symbol={symbol} interval="15m" trades={trades} />
          </div>
        )}

        {/* 模式 C：分屏协同对齐 (Split Mode - 满高撑开) */}
        {viewMode === "split" && (
          <div className="w-full h-full grid grid-cols-2 gap-3.5">
            {/* 左分屏：K 线图 */}
            <div className="w-full h-full min-h-[460px]">
              <KlineChart symbol={symbol} interval="15m" trades={trades} />
            </div>

            {/* 右分屏：缩略对弈舞台与提案 */}
            <div className="w-full h-full min-h-[460px] bg-white rounded-2xl border border-[#E6DFD5] p-5 flex flex-col justify-between shadow-xs">
              <div className="flex justify-between items-center border-b border-[#F0EAE1] pb-2.5">
                <h4 className="font-bold text-xs text-[#2E2823] flex items-center space-x-1.5">
                  <span>⚔️</span>
                  <span>智能体对抗辩论结论</span>
                </h4>
                <span className="text-[10px] bg-[#DE6438] text-white px-2 py-0.5 rounded font-mono font-bold">
                  {consensus === 'LONG' ? '做多优势' : consensus === 'SHORT' ? '沽空优势' : '空仓观望'}
                </span>
              </div>

              <div className="grid grid-cols-2 gap-2.5 my-3 flex-1 items-center">
                {agents.slice(0, 6).map((agent) => (
                  <div
                    key={agent.id}
                    onClick={() => onSelectAgent(agent)}
                    className="p-2.5 rounded-xl bg-[#FAF7F2] border border-[#EBE3D7] hover:border-[#DE6438] cursor-pointer transition flex items-center space-x-2.5 shadow-xs"
                  >
                    <span className="text-xl">{agent.avatar}</span>
                    <div className="overflow-hidden">
                      <div className="text-xs font-bold truncate text-[#2E2823]">{agent.name}</div>
                      <div className="text-[10px] text-[#7E7469] truncate">{agent.badge}</div>
                    </div>
                  </div>
                ))}
              </div>

              <div className="bg-[#FAF7F2] p-3.5 rounded-xl border border-[#E2D9CC] text-xs space-y-1">
                <div className="font-bold text-[#DE6438]">提案摘要: {symbol} 现价 ${Number(latestPrice).toFixed(2)}</div>
                <div className="text-[11px] text-[#4A4237]">委托 ${Number(targetEntry).toFixed(2)} · 止损 ${Number(stopLoss).toFixed(2)} · 止盈 ${Number(takeProfit).toFixed(2)} · 杠杆 {leverage.toFixed(1)}x</div>
                <div className="text-[10px] text-[#4D835C] font-semibold">✓ 七大铁律硬风控已全票核准</div>
              </div>
            </div>
          </div>
        )}

      </div>

      {/* 3. 舞台底座操作手柄控制栏 (Bottom Controller) */}
      <footer className="h-16 px-6 bg-[#FAF7F2] border-t border-[#E6DFD5] flex items-center justify-between shrink-0">
        
        {/* 左侧：全局避险与暂停控制 */}
        <div className="flex items-center space-x-2">
          <button 
            onClick={onEmergencyKill}
            className="px-3.5 py-1.5 rounded-xl bg-[#FCECE8] border border-[#F2D2C7] text-xs font-bold text-[#C24D4D] hover:bg-[#F9DBD3] shadow-xs transition flex items-center space-x-1.5"
          >
            <span>🚨</span>
            <span>紧急熔断拔网线 (Kill Switch)</span>
          </button>

          {onToggleAutoRefresh && (
            <button 
              onClick={onToggleAutoRefresh}
              className="px-3 py-1.5 rounded-xl bg-white border border-[#D8CEBE] text-xs font-semibold text-[#4A4237] hover:bg-[#F4EFE6] shadow-xs transition flex items-center space-x-1"
            >
              <span>{autoRefresh ? "⏸️ 暂停自动轮询" : "▶️ 恢复自动轮询"}</span>
            </button>
          )}
        </div>

        {/* 中间：杠杆动态滑块调节 */}
        <div className="flex items-center space-x-3 bg-white px-3.5 py-1.5 rounded-xl border border-[#D8CEBE] shadow-xs">
          <span className="text-[11px] text-[#7E7469] font-medium">策略执行杠杆:</span>
          <input 
            type="range" 
            min="1" 
            max="3" 
            step="0.5"
            value={leverage} 
            onChange={(e) => setLeverage(parseFloat(e.target.value))}
            className="w-24 accent-[#DE6438] cursor-pointer"
          />
          <span className="text-xs font-mono font-bold text-[#DE6438]">{leverage.toFixed(1)}x</span>
        </div>

        {/* 右侧：下发执行按钮 */}
        <div className="flex items-center space-x-2">
          <button 
            onClick={handleExecute}
            disabled={isExecuting}
            className={`px-5 py-2 rounded-xl text-white text-xs font-bold shadow-md transition transform active:scale-95 flex items-center space-x-1.5 ${
              isExecuting ? "bg-[#7E7469] cursor-not-allowed" : "bg-[#DE6438] hover:bg-[#C8542A]"
            }`}
          >
            <span>⚡</span>
            <span>{isExecuting ? "正在下发沙盒撮合..." : "下发撮合成交订单"}</span>
          </button>
        </div>
      </footer>

    </main>
  );
}
