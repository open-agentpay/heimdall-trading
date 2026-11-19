/**
 * ================================================================================
 * Heimdall 交易平台 - 顶部导航与状态栏组件 (Header)
 * ================================================================================
 */

import React, { useState } from 'react';
import { Wallet, Key, ChevronDown, CheckCircle2, ShieldCheck } from 'lucide-react';

export default function Header({ 
  activeTab, 
  onTabChange, 
  walletState, 
  onOpenWalletModal, 
  onOpenKeyVaultModal 
}) {
  const [isWalletDropdownOpen, setIsWalletDropdownOpen] = useState(false);

  const tabs = [
    { id: 'arena', label: '对弈大厅', icon: '⚔️' },
    { id: 'sentiment', label: '情绪与情报', icon: '📰' },
    { id: 'nav', label: 'NAV 单位净值', icon: '📈' },
    { id: 'risk', label: '七大铁律风控', icon: '🛡️' },
    { id: 'audit', label: '审计轨迹', icon: '📜' },
  ];

  return (
    <header className="h-11 bg-[#FAF7F2] border-b border-[#E6DFD5] flex items-center justify-between px-4 shrink-0 select-none relative z-30">
      
      {/* 左侧：macOS 视窗三色控制按钮 + 品牌标志 + 面包屑 */}
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

      {/* 中间：全局一级导航胶囊菜单 */}
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

      {/* 右侧：密钥安全金库 + Web3 钱包登录 + 全局环境健康指示 */}
      <div className="flex items-center space-x-2.5 text-xs">
        
        {/* 1. 密钥管理安全金库入口按钮 */}
        <button
          onClick={onOpenKeyVaultModal}
          className="flex items-center space-x-1.5 bg-[#F6F1E8] hover:bg-[#EFE9DF] border border-[#DFD6C7] px-2.5 py-1 rounded-lg text-[#2E2823] font-medium transition shadow-xs group"
          title="打开 API Key 与私钥安全金库"
        >
          <Key className="w-3.5 h-3.5 text-[#DE6438] group-hover:rotate-45 transition duration-200" />
          <span className="hidden sm:inline">密钥金库</span>
        </button>

        {/* 2. Web3 钱包登录与状态徽标 */}
        {walletState?.isConnected ? (
          <div className="relative">
            <button
              onClick={() => setIsWalletDropdownOpen(!isWalletDropdownOpen)}
              className="flex items-center space-x-2 bg-white hover:bg-[#FAF7F2] border border-[#DE6438]/30 px-2.5 py-1 rounded-lg shadow-xs transition"
            >
              <span className="w-2 h-2 rounded-full bg-[#4D835C]"></span>
              <span className="font-mono font-bold text-[#2E2823] text-xs">
                {walletState.shortAddress || '0x71C...49b2'}
              </span>
              <span className="text-[10px] bg-[#FAF4EB] text-[#DE6438] font-semibold px-1.5 py-0.2 rounded-sm border border-[#F2D6CA]">
                {walletState.network || 'Arbitrum'}
              </span>
              <ChevronDown className="w-3 h-3 text-[#7E7469]" />
            </button>

            {/* 钱包气泡下拉菜单 */}
            {isWalletDropdownOpen && (
              <div 
                className="absolute right-0 mt-1.5 w-52 bg-[#FAF7F2] rounded-xl border border-[#E6DFD5] shadow-xl p-2 space-y-1 z-50 text-xs animate-in fade-in duration-100"
                onClick={() => setIsWalletDropdownOpen(false)}
              >
                <div className="p-2 bg-white rounded-lg border border-[#EDE7DD] space-y-1">
                  <div className="text-[10px] text-[#7E7469]">已绑定地址</div>
                  <div className="font-mono text-[11px] font-bold text-[#2E2823] break-all">
                    {walletState.address}
                  </div>
                </div>

                <button
                  onClick={onOpenWalletModal}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-[#EFE9DF] text-[#2E2823] flex items-center justify-between"
                >
                  <span>钱包设置与网络切换</span>
                  <Wallet className="w-3.5 h-3.5 text-[#DE6438]" />
                </button>

                <button
                  onClick={onOpenKeyVaultModal}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-[#EFE9DF] text-[#2E2823] flex items-center justify-between"
                >
                  <span>管理交易与大模型密钥</span>
                  <Key className="w-3.5 h-3.5 text-[#DE6438]" />
                </button>

                <div className="border-t border-[#EAE3D8] my-1"></div>

                <button
                  onClick={onOpenWalletModal}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-[#FBEFEA] text-[#DE6438] font-semibold flex items-center justify-between"
                >
                  <span>断开钱包连接</span>
                </button>
              </div>
            )}
          </div>
        ) : (
          <button
            onClick={onOpenWalletModal}
            className="flex items-center space-x-1.5 bg-[#DE6438] hover:bg-[#C95328] text-white px-3 py-1 rounded-lg font-bold shadow-xs transition"
          >
            <Wallet className="w-3.5 h-3.5" />
            <span>连接钱包</span>
          </button>
        )}

        {/* 3. Bifrost 数据底座健康指示灯 */}
        <div className="hidden lg:flex items-center space-x-1.5 bg-[#FAF4EB] border border-[#E5DACB] px-2.5 py-1 rounded-lg text-[#4D835C] font-mono">
          <span className="w-2 h-2 rounded-full bg-[#4D835C] animate-pulse"></span>
          <span className="font-medium">Bifrost: OK</span>
        </div>

      </div>
    </header>
  );
}
