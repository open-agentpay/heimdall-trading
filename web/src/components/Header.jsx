/**
 * ================================================================================
 * Heimdall 交易平台 - 顶部导航与状态栏组件 (Header)
 * ================================================================================
 *
 * 布局优化：
 * - 所有导航胶囊与操作按钮均强制 `whitespace-nowrap shrink-0`，杜绝中文字折行挤压；
 * - 采用简洁精炼的导航标题，并将次要环境徽章设为超大屏 (`2xl`) 展示，保证 1280px~1920px 单行优雅对齐。
 */

import React, { useState } from 'react';
import { Wallet, Key, ChevronDown, LogOut, Moon, Sun, Volume2, VolumeX, PanelLeft, PanelRight } from 'lucide-react';

export default function Header({
  activeTab,
  onTabChange,
  walletState,
  onOpenWalletModal,
  onOpenKeyVaultModal,
  onDisconnectWallet,
  activeBotId = 'bot_eth_debate_01',
  activeBot = null,
  symbol = 'ETH',
  theme = 'light',
  onToggleTheme,
  soundMuted = false,
  onToggleSound,
  onToggleMobileSidebar,
  onToggleMobileTerminal,
}) {
  const [isWalletDropdownOpen, setIsWalletDropdownOpen] = useState(false);

  const tabs = [
    { id: 'arena', label: '对弈大厅', icon: '⚔️' },
    { id: 'swarm', label: '蜂群编排', icon: '🐝' },
    { id: 'backtest', label: '回测', icon: '🧪' },
    { id: 'sentiment', label: '情绪', icon: '📰' },
    { id: 'nav', label: '净值', icon: '📈' },
    { id: 'risk', label: '风控', icon: '🛡️' },
    { id: 'audit', label: '审计', icon: '📜' },
  ];

  return (
    <header className="h-12 bg-panel border-b border-line flex items-center justify-between gap-2 px-3 shrink-0 relative z-30">
      {/* 左侧：移动端侧边栏开关 + 品牌标志 + 超大屏面包屑 */}
      <div className="flex items-center space-x-2 shrink-0 min-w-0">
        <button
          onClick={onToggleMobileSidebar}
          className="lg:hidden p-1.5 rounded-lg bg-muted text-ink-2 hover:text-brand shrink-0"
          title="展开/收起左侧机器人栏"
        >
          <PanelLeft className="w-4 h-4" />
        </button>

        <div className="flex items-center space-x-1.5 font-bold text-sm tracking-tight text-ink whitespace-nowrap shrink-0">
          <span className="text-base">🛡️</span>
          <span>Heimdall</span>
        </div>

        <span className="hidden 2xl:inline text-xs text-ink-4">/</span>
        <span className="hidden 2xl:flex items-center space-x-1.5 text-xs text-ink-3 font-mono bg-muted px-2 py-0.5 rounded-md border border-line-strong whitespace-nowrap max-w-[200px] truncate">
          <span className="text-brand font-bold">{symbol.toUpperCase()}</span>
          <span>·</span>
          <span className="truncate">{activeBot?.name || activeBotId}</span>
        </span>
      </div>

      {/* 中间：全局一级导航胶囊菜单（强制单行不换行，窄屏支持横向滚动） */}
      <nav className="flex items-center space-x-0.5 bg-muted p-1 rounded-xl border border-line-strong overflow-x-auto no-scrollbar shrink min-w-0">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`px-2.5 py-1 rounded-lg text-xs font-semibold flex items-center space-x-1 whitespace-nowrap shrink-0 transition cursor-pointer ${
              activeTab === tab.id
                ? 'bg-surface text-brand shadow-xs'
                : 'text-ink-3 hover:text-ink'
            }`}
          >
            <span>{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        ))}
      </nav>

      {/* 右侧：主题/音效/小屏终端开关 + 密钥安全金库 + Web3 钱包登录 */}
      <div className="flex items-center space-x-1.5 text-xs shrink-0">
        <button
          onClick={onToggleTheme}
          className="p-1.5 rounded-lg bg-canvas hover:bg-muted border border-line-strong text-ink-2 hover:text-brand transition shrink-0 cursor-pointer"
          title={theme === 'dark' ? '切换暖沙工匠主题' : '切换夜间暗黑量化主题'}
        >
          {theme === 'dark' ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
        </button>

        <button
          onClick={onToggleSound}
          className="p-1.5 rounded-lg bg-canvas hover:bg-muted border border-line-strong text-ink-2 hover:text-brand transition shrink-0 cursor-pointer"
          title={soundMuted ? '已静音（点击开启成交与风控提示音）' : '告警音效与桌面通知已开启（点击静音）'}
        >
          {soundMuted ? <VolumeX className="w-3.5 h-3.5 text-ink-4" /> : <Volume2 className="w-3.5 h-3.5 text-success" />}
        </button>

        <button
          onClick={onToggleMobileTerminal}
          className="lg:hidden p-1.5 rounded-lg bg-canvas hover:bg-muted border border-line-strong text-ink-2 hover:text-brand shrink-0"
          title="展开/收起右侧思维链终端"
        >
          <PanelRight className="w-3.5 h-3.5" />
        </button>

        {/* 1. 密钥管理安全金库入口按钮 */}
        <button
          onClick={onOpenKeyVaultModal}
          className="flex items-center space-x-1 bg-canvas hover:bg-muted border border-line-strong px-2.5 py-1 rounded-lg text-ink font-medium transition shadow-xs whitespace-nowrap shrink-0 cursor-pointer"
          title="打开 API Key 与私钥安全金库"
        >
          <Key className="w-3.5 h-3.5 text-brand" />
          <span className="hidden xl:inline">密钥金库</span>
        </button>

        {/* 2. Web3 钱包登录与状态徽标 */}
        {walletState?.isConnected ? (
          <div className="relative shrink-0">
            <button
              onClick={() => setIsWalletDropdownOpen(!isWalletDropdownOpen)}
              className="flex items-center space-x-1.5 bg-surface hover:bg-panel border border-brand/30 px-2.5 py-1 rounded-lg shadow-xs transition whitespace-nowrap shrink-0 cursor-pointer"
            >
              <span className="w-2 h-2 rounded-full bg-success-solid shrink-0"></span>
              <span className="font-mono font-bold text-ink text-xs">
                {walletState.shortAddress || '已连接'}
              </span>
              <ChevronDown className="w-3 h-3 text-ink-3 shrink-0" />
            </button>

            {isWalletDropdownOpen && (
              <div
                className="absolute right-0 mt-1.5 w-52 bg-panel rounded-xl border border-line shadow-xl p-2 space-y-1 z-50 text-xs"
                onClick={() => setIsWalletDropdownOpen(false)}
              >
                <div className="p-2 bg-surface rounded-lg border border-line-soft space-y-1">
                  <div className="text-xs text-ink-3">已绑定地址 ({walletState.network || 'Arbitrum'})</div>
                  <div className="font-mono text-xs font-bold text-ink break-all">
                    {walletState.address}
                  </div>
                </div>

                <button
                  onClick={onOpenWalletModal}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-muted text-ink flex items-center justify-between"
                >
                  <span>钱包设置与网络切换</span>
                  <Wallet className="w-3.5 h-3.5 text-brand" />
                </button>

                <button
                  onClick={onOpenKeyVaultModal}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-muted text-ink flex items-center justify-between"
                >
                  <span>管理交易与大模型密钥</span>
                  <Key className="w-3.5 h-3.5 text-brand" />
                </button>

                <div className="border-t border-line my-1"></div>

                <button
                  onClick={() => {
                    setIsWalletDropdownOpen(false);
                    if (onDisconnectWallet) onDisconnectWallet();
                    else if (onOpenWalletModal) onOpenWalletModal();
                  }}
                  className="w-full text-left px-2.5 py-1.5 rounded-lg hover:bg-brand-soft text-brand font-semibold flex items-center justify-between cursor-pointer"
                >
                  <span>断开钱包连接</span>
                  <LogOut className="w-3.5 h-3.5 text-brand" />
                </button>
              </div>
            )}
          </div>
        ) : (
          <button
            onClick={onOpenWalletModal}
            className="flex items-center space-x-1.5 bg-brand hover:bg-brand-hover text-white px-3 py-1 rounded-lg font-bold shadow-xs transition whitespace-nowrap shrink-0 cursor-pointer"
          >
            <Wallet className="w-3.5 h-3.5" />
            <span>连接钱包</span>
          </button>
        )}

        {/* 3. 数据底座健康指示灯（超大屏展示） */}
        <div className="hidden 2xl:flex items-center space-x-1.5 bg-canvas border border-line px-2 py-1 rounded-lg text-success font-mono whitespace-nowrap shrink-0">
          <span className="w-2 h-2 rounded-full bg-success-solid animate-pulse"></span>
          <span className="font-medium">Hub: OK</span>
        </div>
      </div>
    </header>
  );
}
