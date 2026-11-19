/**
 * ================================================================================
 * Heimdall 交易平台 - 右侧思维链日志流与命令行执行终端 (TerminalPanel)
 * ================================================================================
 */

import React, { useState } from 'react';

export default function TerminalPanel() {
  const [activeTab, setActiveTab] = useState("stream"); // "stream" | "console"

  return (
    <aside className="w-80 bg-[#FAF7F2] border-l border-[#E6DFD5] flex flex-col shrink-0 select-none">
      
      {/* 顶栏标签切换 */}
      <div className="h-10 px-3 border-b border-[#EBE3D7] flex items-center justify-between">
        <div className="flex space-x-1">
          <button
            onClick={() => setActiveTab("stream")}
            className={`px-2.5 py-1 rounded-md text-xs font-bold transition ${
              activeTab === "stream"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            🧠 Agent 思维链
          </button>
          <button
            onClick={() => setActiveTab("console")}
            className={`px-2.5 py-1 rounded-md text-xs font-bold transition ${
              activeTab === "console"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            💻 执行控制台
          </button>
        </div>

        <span className="text-[10px] text-[#8C8276] font-mono">速度: 420 tok/s</span>
      </div>

      {/* 思维链流式日志内容区 */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2.5 text-xs font-mono">
        
        <div className="bg-white p-2.5 rounded-xl border border-[#E6DFD5] shadow-xs">
          <div className="flex items-center justify-between text-[10px] text-[#3E6B89] font-bold mb-1">
            <span>[AnalystAgent]</span>
            <span>19:42:01 · 82ms</span>
          </div>
          <p className="text-[#4A4237] text-[11px] leading-relaxed">
            扫描 15m 闭合 K 线，确认在 EMA20 上方获得支撑，RSI 为 58.4（处于健康强势未超买区间），初步定性为 TREND_UP。
          </p>
        </div>

        <div className="bg-white p-2.5 rounded-xl border border-[#E6DFD5] shadow-xs">
          <div className="flex items-center justify-between text-[10px] text-[#4D835C] font-bold mb-1">
            <span>[SentimentWhaleAnalyst]</span>
            <span>19:42:02 · 115ms</span>
          </div>
          <p className="text-[#4A4237] text-[11px] leading-relaxed">
            恐慌指数从 18 拐头回升至 24，大户连续 24h 提现 1,250 万美元 ETH 锁仓，链上抛压极轻，综合情绪得分 +0.45。
          </p>
        </div>

        <div className="bg-[#FFFDFB] p-2.5 rounded-xl border border-[#F2D6CA] shadow-xs">
          <div className="flex items-center justify-between text-[10px] text-[#DE6438] font-bold mb-1">
            <span>[DebateModerator 裁判裁决]</span>
            <span>19:42:04 · 240ms</span>
          </div>
          <p className="text-[#4A4237] text-[11px] leading-relaxed">
            多头研究员立论充分（现货买盘真实），空头提出的 3200 阻力位已通过将止损点收紧至 3060 得到有效防御。裁决：批准发起做多！
          </p>
        </div>

        <div className="bg-[#F6FCF7] p-2.5 rounded-xl border border-[#CEE5D3] shadow-xs">
          <div className="flex items-center justify-between text-[10px] text-[#4D835C] font-bold mb-1">
            <span>[HardRiskGuard 确定性七大铁律]</span>
            <span>19:42:05 · 4ms</span>
          </div>
          <ul className="text-[10px] text-[#3A6B46] space-y-0.5">
            <li>✓ 铁律1 当日回撤 (1.2% &lt; 5.0%)</li>
            <li>✓ 铁律2 Bifrost 价差 (0.03% &lt; 0.5%)</li>
            <li>✓ 铁律3 杠杆倍数 (1.5x &lt; 3.0x)</li>
            <li>✓ 铁律4 单笔头寸 ($4,680 &lt; $5,000)</li>
            <li>✓ 铁律5 允许滑点 (0.1% &lt; 0.3%)</li>
            <li>✓ 铁律6 宏观事件避险日历 (静默期外)</li>
            <li>✓ 铁律7 极端插针防范 (单K振幅 1.1% 平稳)</li>
          </ul>
        </div>

      </div>

      {/* 底部控制台窗口 (Mini Console) */}
      <div className="h-36 bg-[#231F1C] p-2.5 flex flex-col font-mono text-[11px] text-[#EFE8DE] border-t border-[#3A332C]">
        <div className="flex items-center justify-between pb-1 border-b border-[#3A332C] text-[10px] text-[#9E917F]">
          <span>💻 PaperEngine Execution Daemon</span>
          <span className="text-[#62C554]">● LIVE</span>
        </div>
        <div className="flex-1 overflow-y-auto space-y-1 pt-1.5 text-[10px] text-[#D8CEBE]">
          <div className="text-[#9E9486]">[20:40:00] Bifrost WebSocket heartbeat ack</div>
          <div>[20:42:05] <span className="text-[#62C554]">PROPOSAL_APPROVED</span> id=prop_20261119_001</div>
          <div>[20:42:06] Paper matched fill: 1.5 ETH @ 3,120.00</div>
          <div>[20:42:06] <span className="text-[#F4BF4F]">NAV Updated</span>: 1.0418 -&gt; 1.0422</div>
          <div className="text-[#9E9486]">[20:42:07] Waiting next 15m cycle...</div>
        </div>
      </div>

    </aside>
  );
}
