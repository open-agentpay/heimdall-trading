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

import React, { useState } from 'react';
import KlineChart from './KlineChart';

export default function DecisionArena({ agents, onSelectAgent, onEmergencyKill, onExecuteTrade }) {
  const [viewMode, setViewMode] = useState("arena"); // "arena" | "kline" | "split"
  const [leverage, setLeverage] = useState(1.5);
  const [isExecuting, setIsExecuting] = useState(false);

  // 快捷获取各个角色对象
  const analyst = agents.find((a) => a.id === "analyst");
  const sentiment = agents.find((a) => a.id === "sentiment");
  const bull = agents.find((a) => a.id === "bull");
  const bear = agents.find((a) => a.id === "bear");
  const moderator = agents.find((a) => a.id === "moderator");
  const strategy = agents.find((a) => a.id === "strategy");
  const riskOfficer = agents.find((a) => a.id === "risk_officer");
  const hardGuard = agents.find((a) => a.id === "hard_guard");

  const handleExecute = () => {
    setIsExecuting(true);
    setTimeout(() => {
      setIsExecuting(false);
      if (onExecuteTrade) onExecuteTrade();
    }, 600);
  };

  return (
    <main className="flex-1 flex flex-col bg-[#F7F3EC] overflow-hidden relative">
      
      <!-- 1. 舞台顶栏：视图切换药丸 (Arena vs Kline vs Split) & 操作按键 -->
      <div className="h-11 px-5 flex items-center justify-between border-b border-[#EBE3D7] bg-[#FAF7F2]/90 backdrop-blur shrink-0">
        
        <!-- 左侧：多模式视图切换胶囊 -->
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

        <!-- 右侧：当前轮次指示与状态 -->
        <div className="flex items-center space-x-2.5">
          <span className="text-xs text-[#7E7469] font-mono">
            对抗激辩: <strong className="text-[#4D835C]">第 2 轮已收敛</strong>
          </span>
          <span className="text-[#C4B7A4]">|</span>
          <span className="text-[11px] text-[#9E917F] bg-white border border-[#E2D9CC] px-2 py-0.5 rounded-md">
            💡 点击任一席位可深入查看其决策论据与指标
          </span>
        </div>
      </div>

      <!-- 2. 中间核心画布展现区 -->
      <div className="flex-1 p-5 overflow-auto flex items-center justify-center">

        {/* 模式 A：纯环形牌桌博弈 (Arena Mode) */}
        {viewMode === "arena" && (
          <div className="w-full max-w-4xl h-[470px] poker-arena relative flex items-center justify-center p-6 select-none">
            
            {/* 席位 1 (上方中央)：Analyst (技术面分析师) */}
            {analyst && (
              <div 
                onClick={() => onSelectAgent(analyst)}
                className="seat-badge absolute -top-5 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-3 py-1.5 flex items-center space-x-2 border-2 border-[#3E6B89] group-hover:scale-105 transition bg-white">
                  <span className="text-lg">{analyst.avatar}</span>
                  <div>
                    <div className="text-[11px] font-bold text-[#2E2823]">{analyst.name}</div>
                    <div className="text-[9px] text-[#3E6B89] font-semibold">{analyst.tag}</div>
                  </div>
                  <span className="text-[10px] bg-[#E9F2F7] text-[#3E6B89] px-1.5 py-0.5 rounded font-mono font-bold">
                    {analyst.badge}
                  </span>
                </div>
                <div className="text-[10px] text-[#7E7469] mt-0.5 bg-white/95 px-2 rounded-full border border-[#DFD6C7] shadow-xs">
                  🔍 EMA20 上穿金叉突破 3,120
                </div>
              </div>
            )}

            {/* 席位 2 (左上方)：Sentiment & Whale (情绪与巨鲸) */}
            {sentiment && (
              <div 
                onClick={() => onSelectAgent(sentiment)}
                className="seat-badge absolute -top-2 left-8 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-2.5 py-1.5 flex items-center space-x-2 border border-[#4D835C] group-hover:scale-105 transition bg-white">
                  <span className="text-base">{sentiment.avatar}</span>
                  <div>
                    <div className="text-[11px] font-bold text-[#2E2823]">{sentiment.name}</div>
                    <div className="text-[9px] text-[#4D835C] font-semibold">{sentiment.tag}</div>
                  </div>
                </div>
                <div className="text-[9px] text-[#4D835C] mt-0.5 font-bold bg-[#EBF5EE] px-1.5 rounded shadow-xs">
                  提币锁仓 $12.5M
                </div>
              </div>
            )}

            {/* 席位 3 (右上方)：Risk Officer (软逻辑风控官) */}
            {riskOfficer && (
              <div 
                onClick={() => onSelectAgent(riskOfficer)}
                className="seat-badge absolute -top-2 right-8 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-2.5 py-1.5 flex items-center space-x-2 border border-[#8C7A6B] group-hover:scale-105 transition bg-white">
                  <span className="text-base">{riskOfficer.avatar}</span>
                  <div>
                    <div className="text-[11px] font-bold text-[#2E2823]">{riskOfficer.name}</div>
                    <div className="text-[9px] text-[#4D835C] font-semibold">盈亏比 2.2:1 (放行)</div>
                  </div>
                </div>
              </div>
            )}

            {/* 席位 4 (左侧腰部)：Bull Researcher (多头先锋立论) */}
            {bull && (
              <div 
                onClick={() => onSelectAgent(bull)}
                className="seat-badge absolute left-2 top-1/2 -translate-y-1/2 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-3 py-2 flex items-center space-x-2 border-2 border-[#4D835C] bg-[#F7FCF8] group-hover:scale-105 transition">
                  <span className="text-2xl">{bull.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#4D835C]">{bull.name}</div>
                    <div className="text-[10px] text-[#558B64] font-medium">目标 $3,250 · 置信 85%</div>
                  </div>
                </div>
                <div className="w-40 text-center text-[10px] text-[#2E2823] mt-1.5 bg-white p-1.5 rounded-lg border border-[#D5E5D8] shadow-xs leading-tight">
                  “现货买盘真实，3150 空头止损密集，轧空暴拉赔率极高！”
                </div>
              </div>
            )}

            {/* 席位 5 (右侧腰部)：Bear Researcher (空头风险找茬) */}
            {bear && (
              <div 
                onClick={() => onSelectAgent(bear)}
                className="seat-badge absolute right-2 top-1/2 -translate-y-1/2 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-3 py-2 flex items-center space-x-2 border-2 border-[#C24D4D] bg-[#FCF7F7] group-hover:scale-105 transition">
                  <span className="text-2xl">{bear.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#C24D4D]">{bear.name}</div>
                    <div className="text-[10px] text-[#A63F3F] font-medium">防守 $3,060 · 置信 45%</div>
                  </div>
                </div>
                <div className="w-40 text-center text-[10px] text-[#2E2823] mt-1.5 bg-white p-1.5 rounded-lg border border-[#F2D4D4] shadow-xs leading-tight">
                  “上方 3200 仍有解套抛压，必须在 3060 挂牢硬止损防冲高回落。”
                </div>
              </div>
            )}

            {/* 席位 6 (左下方)：Strategy Agent (定量策略生成器) */}
            {strategy && (
              <div 
                onClick={() => onSelectAgent(strategy)}
                className="seat-badge absolute -bottom-2 left-10 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-2.5 py-1.5 flex items-center space-x-2 border border-[#D48B38] group-hover:scale-105 transition bg-white">
                  <span className="text-base">{strategy.avatar}</span>
                  <div>
                    <div className="text-[11px] font-bold text-[#2E2823]">{strategy.name}</div>
                    <div className="text-[9px] text-[#D48B38] font-semibold">{strategy.tag}</div>
                  </div>
                </div>
              </div>
            )}

            {/* 席位 7 (下方中央)：Debate Moderator (裁判长 / 最终裁决) */}
            {moderator && (
              <div 
                onClick={() => onSelectAgent(moderator)}
                className="seat-badge absolute -bottom-6 flex flex-col items-center cursor-pointer group z-10"
              >
                <div className="tactile-card px-4 py-2 flex items-center space-x-3 border-2 border-[#DE6438] bg-[#FFFBF8] shadow-md group-hover:scale-105 transition">
                  <span className="text-xl">{moderator.avatar}</span>
                  <div>
                    <div className="text-xs font-bold text-[#DE6438]">{moderator.name}</div>
                    <div className="text-[10px] font-semibold text-[#2E2823]">{moderator.tag}</div>
                  </div>
                  <div className="text-xs font-mono font-bold bg-[#DE6438] text-white px-2 py-0.5 rounded shadow-xs">
                    {moderator.badge}
                  </div>
                </div>
              </div>
            )}

            {/* 席位 8 (右下方)：HardRiskGuard (七大铁律确定性硬风控) */}
            {hardGuard && (
              <div 
                onClick={() => onSelectAgent(hardGuard)}
                className="seat-badge absolute -bottom-2 right-10 flex flex-col items-center cursor-pointer group"
              >
                <div className="tactile-card px-2.5 py-1.5 flex items-center space-x-2 border border-[#4D835C] group-hover:scale-105 transition bg-white">
                  <span className="text-base">{hardGuard.avatar}</span>
                  <div>
                    <div className="text-[11px] font-bold text-[#2E2823]">{hardGuard.name}</div>
                    <div className="text-[9px] text-[#4D835C] font-semibold">7/7 铁律全票放行</div>
                  </div>
                </div>
              </div>
            )}

            {/* ========================================================= */}
            {/* 桌台中心：核心交易标的与实时生成的交易提案卡片 */}
            {/* ========================================================= */}
            <div className="w-80 bg-white rounded-2xl p-4 border-2 border-[#D8CEBE] shadow-xl flex flex-col justify-between z-0">
              <div className="flex items-center justify-between pb-2.5 border-b border-[#F0EAE1]">
                <div className="flex items-center space-x-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-[#4D835C] animate-pulse"></span>
                  <span className="font-mono font-bold text-sm text-[#2E2823]">ETH-USDT 永续</span>
                </div>
                <div className="text-right">
                  <span className="font-mono font-bold text-base text-[#4D835C]">$3,124.50</span>
                  <span className="block text-[10px] font-mono text-[#4D835C] font-semibold">+2.45%</span>
                </div>
              </div>

              <div className="py-2.5 space-y-1.5 text-xs">
                <div className="flex justify-between items-center">
                  <span className="text-[#8C8276]">多空决策结论:</span>
                  <span className="font-bold text-[#4D835C] font-mono bg-[#EBF5EE] px-2 py-0.5 rounded text-xs">
                    🟢 开多 LONG · 1.5x
                  </span>
                </div>
                <div className="flex justify-between font-mono">
                  <span className="text-[#8C8276]">限价入场委托:</span>
                  <span className="font-semibold text-[#2E2823]">$3,120.00</span>
                </div>
                <div className="flex justify-between font-mono">
                  <span className="text-[#8C8276]">硬防守止损价:</span>
                  <span className="text-[#C24D4D] font-semibold">$3,060.00 (-1.9%)</span>
                </div>
                <div className="flex justify-between font-mono">
                  <span className="text-[#8C8276]">目标止盈点位:</span>
                  <span className="text-[#4D835C] font-semibold">$3,250.00 (+4.1%)</span>
                </div>
                <div className="flex justify-between font-mono">
                  <span className="text-[#8C8276]">凯利头寸价值:</span>
                  <span className="text-[#DE6438] font-bold">$4,680.00 USD</span>
                </div>
              </div>

              {/* 七大铁律通过徽章 */}
              <div className="pt-2 border-t border-[#F0EAE1] flex items-center justify-between">
                <span className="text-[10px] text-[#7E7469] flex items-center space-x-1">
                  <span>🛡️</span>
                  <span>底层确定性硬风控</span>
                </span>
                <span className="text-[10px] font-bold text-[#4D835C] bg-[#EBF5EE] px-2 py-0.5 rounded font-mono border border-[#CEE5D3]">
                  ✓ 7/7 铁律全票通过
                </span>
              </div>
            </div>

          </div>
        )}

        {/* 模式 B：纯宽屏实时 K 线图 (Kline Mode) */}
        {viewMode === "kline" && (
          <div className="w-full h-full max-w-5xl max-h-[500px]">
            <KlineChart symbol="ETH" interval="15m" />
          </div>
        )}

        {/* 模式 C：分屏协同对齐 (Split Mode) */}
        {viewMode === "split" && (
          <div className="w-full h-full max-w-6xl grid grid-cols-2 gap-4">
            <!-- 左分屏：K 线图 -->
            <div className="h-[460px]">
              <KlineChart symbol="ETH" interval="15m" />
            </div>

            <!-- 右分屏：缩略对弈舞台与提案 -->
            <div className="h-[460px] bg-white rounded-2xl border border-[#E6DFD5] p-4 flex flex-col justify-between shadow-xs">
              <div className="flex justify-between items-center border-b border-[#F0EAE1] pb-2">
                <h4 className="font-bold text-xs text-[#2E2823] flex items-center space-x-1.5">
                  <span>⚔️</span>
                  <span>智能体对抗辩论结论</span>
                </h4>
                <span className="text-[10px] bg-[#DE6438] text-white px-2 py-0.5 rounded font-mono font-bold">
                  做多置信度 82%
                </span>
              </div>

              <div className="grid grid-cols-2 gap-2 my-2">
                {agents.slice(0, 6).map((agent) => (
                  <div
                    key={agent.id}
                    onClick={() => onSelectAgent(agent)}
                    className="p-2 rounded-xl bg-[#FAF7F2] border border-[#EBE3D7] hover:border-[#DE6438] cursor-pointer transition flex items-center space-x-2"
                  >
                    <span className="text-base">{agent.avatar}</span>
                    <div className="overflow-hidden">
                      <div className="text-[11px] font-bold truncate text-[#2E2823]">{agent.name}</div>
                      <div className="text-[9px] text-[#7E7469] truncate">{agent.badge}</div>
                    </div>
                  </div>
                ))}
              </div>

              <div className="bg-[#FAF7F2] p-3 rounded-xl border border-[#E2D9CC] text-xs space-y-1">
                <div className="font-bold text-[#DE6438]">提案摘要: ETH 现价 $3,124.50</div>
                <div className="text-[11px] text-[#4A4237]">委托 3,120 · 止损 3,060 · 止盈 3,250 · 杠杆 1.5x</div>
                <div className="text-[10px] text-[#4D835C] font-semibold">✓ 七大铁律硬风控已全票核准</div>
              </div>
            </div>
          </div>
        )}

      </div>

      <!-- 3. 舞台底座操作手柄控制栏 (Bottom Controller) -->
      <footer className="h-16 px-6 bg-[#FAF7F2] border-t border-[#E6DFD5] flex items-center justify-between shrink-0">
        
        <!-- 左侧：全局避险与暂停控制 -->
        <div className="flex items-center space-x-2">
          <button 
            onClick={onEmergencyKill}
            className="px-3.5 py-1.5 rounded-xl bg-[#FCECE8] border border-[#F2D2C7] text-xs font-bold text-[#C24D4D] hover:bg-[#F9DBD3] shadow-xs transition flex items-center space-x-1.5"
          >
            <span>🚨</span>
            <span>紧急熔断拔网线 (Kill Switch)</span>
          </button>

          <button className="px-3 py-1.5 rounded-xl bg-white border border-[#D8CEBE] text-xs font-semibold text-[#4A4237] hover:bg-[#F4EFE6] shadow-xs transition">
            ⏸️ 暂停自动轮询
          </button>
        </div>

        <!-- 中间：杠杆动态滑块调节 -->
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

        <!-- 右侧：下发执行按钮 -->
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
