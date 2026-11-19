/**
 * ================================================================================
 * Heimdall 交易平台 - 左侧机器人与数据源探索栏 (Sidebar)
 * ================================================================================
 */

import React, { useState } from 'react';

export default function Sidebar({ activeBotId, onSelectBot }) {
  const bots = [
    {
      id: "bot_eth_debate_01",
      name: "ETH 多空激辩对战",
      tag: "01 · 运行中",
      symbol: "ETH",
      timeframe: "15m",
      nav: 1.042,
      runtime: "13小时",
      tokens: "529k",
      active: true,
    },
    {
      id: "bot_btc_arb_02",
      name: "BTC 资金费率套利",
      tag: "02 · 待命",
      symbol: "BTC",
      timeframe: "1h",
      nav: 1.008,
      runtime: "8小时",
      tokens: "124k",
      active: false,
    },
    {
      id: "bot_sol_grid_03",
      name: "SOL 波动率动态网格",
      tag: "03 · 待命",
      symbol: "SOL",
      timeframe: "5m",
      nav: 1.000,
      runtime: "暂停",
      tokens: "45k",
      active: false,
    },
  ];

  return (
    <aside className="w-64 bg-[#FAF7F2] border-r border-[#E6DFD5] flex flex-col shrink-0 select-none">
      
      <!-- 分组 1: 机器人实例列表 -->
      <div className="p-3 border-b border-[#EBE4DA]">
        <div className="flex items-center justify-between text-xs font-semibold text-[#8C8276] uppercase tracking-wider mb-2">
          <span>策略机器人 (Bots)</span>
          <span className="text-xs bg-[#EFE9DF] px-1.5 py-0.5 rounded text-[#5E554B] font-mono">3 活跃</span>
        </div>

        <div className="space-y-1.5">
          {bots.map((bot) => (
            <div
              key={bot.id}
              onClick={() => onSelectBot(bot.id)}
              className={`p-2.5 rounded-xl border cursor-pointer transition ${
                activeBotId === bot.id
                  ? "bg-white border-[#DE6438] shadow-sm border-l-4"
                  : "bg-[#FAF7F2] border-[#E6DFD5] hover:bg-white opacity-85 hover:opacity-100"
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-bold text-[#2E2823]">{bot.name}</span>
                <span
                  className={`text-[10px] px-1.5 py-0.2 rounded font-mono ${
                    activeBotId === bot.id
                      ? "bg-[#DE6438] text-white"
                      : "bg-[#E2DACF] text-[#6E6458]"
                  }`}
                >
                  {bot.tag}
                </span>
              </div>
              <div className="flex items-center justify-between text-[11px] text-[#7E7469]">
                <span>周期: {bot.timeframe} · Hyperliquid</span>
                <span className="font-mono text-[#4D835C] font-semibold">NAV {bot.nav.toFixed(3)}</span>
              </div>
              <div className="mt-1.5 pt-1.5 border-t border-[#F2EDE4] flex justify-between text-[10px] text-[#9E9486]">
                <span>⏱️ {bot.runtime}</span>
                <span>🔥 {bot.tokens}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      <!-- 分组 2: 数据底座与信号感知源状态树 -->
      <div className="flex-1 overflow-y-auto p-3">
        <div className="flex items-center justify-between text-xs font-semibold text-[#8C8276] uppercase tracking-wider mb-2">
          <span>底座信号源 (Data Hub)</span>
          <span className="text-[10px] text-[#A89F93]">零前视接入</span>
        </div>

        <div className="space-y-1 text-xs text-[#5E554B]">
          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF] cursor-pointer">
            <span className="text-[#DE6438]">📁</span>
            <span className="font-medium">bifrost.closed_klines</span>
            <span className="ml-auto text-[10px] text-[#4D835C] font-mono">15m已封盘</span>
          </div>

          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF] cursor-pointer">
            <span className="text-[#3E6B89]">⚡</span>
            <span>funding_rate_stream</span>
            <span className="ml-auto text-[10px] text-[#7E7469] font-mono">+12.4% apy</span>
          </div>

          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF] cursor-pointer">
            <span className="text-[#DE6438]">😨</span>
            <span>alternative.me_fng</span>
            <span className="ml-auto text-[10px] bg-[#FCECE6] text-[#DE6438] px-1.5 rounded font-mono font-bold">24 极恐</span>
          </div>

          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF] cursor-pointer">
            <span className="text-[#4D835C]">🐋</span>
            <span>whale_onchain_flow</span>
            <span className="ml-auto text-[10px] text-[#4D835C] font-mono">-$12.5M锁仓</span>
          </div>

          <div className="flex items-center space-x-2 p-1.5 rounded-lg hover:bg-[#EFE9DF] cursor-pointer">
            <span className="text-[#A25D8F]">📅</span>
            <span>macro_fomc_calendar</span>
            <span className="ml-auto text-[10px] text-[#8C8276] font-mono">距CPI 3天</span>
          </div>
        </div>
      </div>

      <!-- 分组 3: 底部用户资产与回撤水位槽 -->
      <div className="p-3 bg-[#F2EDE4] border-t border-[#E6DFD5]">
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center space-x-2">
            <div className="w-7 h-7 rounded-full bg-[#DE6438] text-white flex items-center justify-center font-bold text-xs shadow-xs">
              纯
            </div>
            <div>
              <div className="text-xs font-bold text-[#2E2823]">赵纯想 (Trader)</div>
              <div className="text-[10px] text-[#7E7469]">量化实盘专业席位</div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-xs font-mono font-bold text-[#2E2823]">$10,420.50</div>
            <div className="text-[10px] font-mono text-[#4D835C] font-semibold">+4.20%</div>
          </div>
        </div>

        <div className="w-full bg-[#E5DACB] h-1.5 rounded-full overflow-hidden mt-2">
          <div className="bg-[#4D835C] h-full rounded-full" style={{ width: '24%' }}></div>
        </div>
        <div className="flex justify-between text-[10px] text-[#8C8276] mt-1 font-mono">
          <span>当日回撤: 1.20%</span>
          <span>熔断线: 5.00%</span>
        </div>
      </div>

    </aside>
  );
}
