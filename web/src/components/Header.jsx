/**
 * ================================================================================
 * Heimdall 交易平台 - 顶部导航与状态栏组件 (Header)
 * ================================================================================
 */

import React from 'react';

export default function Header({ activeTab, onTabChange }) {
  const tabs = [
    { id: 'arena', label: '对弈大厅', icon: '⚔️' },
    { id: 'sentiment', label: '情绪与情报', icon: '📰' },
    { id: 'nav', label: 'NAV 单位净值', icon: '📈' },
    { id: 'risk', label: '七大铁律风控', icon: '🛡️' },
    { id: 'audit', label: '审计轨迹', icon: '📜' },
  ];

  return (
    <header className="h-11 bg-[#FAF7F2] border-b border-[#E6DFD5] flex items-center justify-between px-4 shrink-0 select-none">
      
      <!-- 左侧：macOS 视窗三色控制按钮 + 品牌标志 + 面包屑 -->
      <div className="flex items-center space-x-3">
        <div className="flex space-x-1.5 mr-2">
          <div className="w-3 h-3 rounded-full bg-[#EC6A5E] border border-[#D04E43] cursor-pointer" title="关闭"></div>
          <div className="w-3 h-3 rounded-full bg-[#F4BF4F] border border-[#D6A035] cursor-pointer" title="最小化"></div>
          <div className="w-3 h-3 rounded-full bg-[#62C554] border border-[#4BA53D] cursor-pointer" title="全屏"></div>
        </div>

        <div className="flex items-center space-x-2 font-bold text-sm tracking-tight text-[#2E2823]">
          <span className="text-base">🛡️</span>
          <span>Heimdall Trading</span>
        </div>

        <span className="text-xs text-[#9E9486]">/</span>
        <span className="text-xs text-[#7E7469] font-mono bg-[#EFE9DF] px-2 py-0.5 rounded-md border border-[#E0D8CB]">
          main / bot_eth_debate_01
        </span>
      </div>

      <!-- 中间：全局一级导航胶囊菜单 -->
      <nav className="flex items-center space-x-1 bg-[#EFE9DF] p-1 rounded-xl border border-[#DFD6C7]">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`px-3 py-1 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition ${
              activeTab === tab.id
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        ))}
      </nav>

      <!-- 右侧：全局环境与安全状态指示 -->
      <div className="flex items-center space-x-3 text-xs">
        <div className="flex items-center space-x-1.5 bg-[#FAF4EB] border border-[#E5DACB] px-2.5 py-1 rounded-lg text-[#4D835C] font-mono">
          <span className="w-2 h-2 rounded-full bg-[#4D835C] animate-pulse"></span>
          <span className="font-medium">Bifrost: HEALTHY</span>
        </div>

        <div className="text-[#DE6438] font-mono font-semibold bg-[#FBEFEA] px-2.5 py-1 rounded-lg border border-[#F2D6CA]">
          24/7 守护在线
        </div>
      </div>
    </header>
  );
}
