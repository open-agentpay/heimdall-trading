/**
 * ================================================================================
 * Heimdall 交易平台 - 前端应用主入口组件 (App.jsx)
 * ================================================================================
 * 
 * 核心升级：
 * 1. 彻底告别纯静态 Mock：自动向后端请求真实的机器人列表与全量多 Agent 决策状态；
 * 2. 真实响应单步决策推演 (Step Run)，将后端运行结果实时同步至桌台席位、K线与终端；
 * 3. 完整接入子页面多视图路由 (对弈大厅 / 情绪雷达 / NAV单位净值 / 七大铁律风控 / 审计轨迹)；
 * 4. 真实集成 Web3 钱包挑战验签与密钥金库安全托管。
 */

import React, { useState, useEffect } from 'react';
import { showConfirm } from './utils/dialog';
import Header from './components/Header';
import Sidebar from './components/Sidebar';
import DecisionArena from './components/DecisionArena';
import TerminalPanel from './components/TerminalPanel';
import AgentModal from './components/AgentModal';
import WalletModal from './components/WalletModal';
import KeyVaultModal from './components/KeyVaultModal';
import CreateBotModal from './components/CreateBotModal';
import CapitalDistributionModal from './components/CapitalDistributionModal';
import AuditTracesView from './components/AuditTracesView';
import NavView from './components/NavView';
import SentimentView from './components/SentimentView';
import RiskRadarView from './components/RiskRadarView';
import SwarmView from './components/SwarmView';
import BacktestView from './components/BacktestView';
import { createPendingAgents, fmtUsd } from './data/agentRoles';
import { applyDecisionToAgents } from './data/decisionToAgents';
import Toast from './components/Toast';
import { useToast } from './hooks/useToast';
import { useTheme } from './hooks/useTheme';
import { useWalletSession } from './hooks/useWalletSession';
import { 
  triggerEmergencyKill, 
  stepBot, 
  listBots, 
  deleteBot,
  getBotLatestState,
  getBotTrades,
} from './api/endpoints';
import { useWebSocket } from './api/useWebSocket';
import {
  isSoundMuted,
  setSoundMuted,
  playTradeFillChime,
  playRiskAlertBeep,
  requestNotificationPermission,
  sendBrowserNotification,
} from './utils/alerts';

export default function App() {
  const { notification, showToast } = useToast();
  const [activeTab, setActiveTab] = useState("arena"); // arena | swarm | sentiment | nav | risk | audit
  const [activeBotId, setActiveBotId] = useState(() => {
    return localStorage.getItem("heimdall_active_bot_id") || "bot_eth_debate_01";
  });
  const [selectedAgent, setSelectedAgent] = useState(null);
  // M5：首轮真实推演之前，席位只显示角色说明与“等待推演”，不再预置编造的分析结论
  const [agents, setAgents] = useState(createPendingAgents);
  const [botsList, setBotsList] = useState([]);
  const [decisionRecord, setDecisionRecord] = useState(null);
  const [trades, setTrades] = useState([]);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Web3 钱包状态与密钥金库弹窗状态
  const [isWalletModalOpen, setIsWalletModalOpen] = useState(false);
  const [isKeyVaultModalOpen, setIsKeyVaultModalOpen] = useState(false);
  const [isCreateBotModalOpen, setIsCreateBotModalOpen] = useState(false);
  const [prefillBotConfig, setPrefillBotConfig] = useState(null);
  const { theme, toggleTheme: handleToggleTheme } = useTheme();
  const [soundMuted, setSoundMutedState] = useState(() => isSoundMuted());
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [mobileTerminalOpen, setMobileTerminalOpen] = useState(false);

  const handleToggleSound = async () => {
    const next = !soundMuted;
    setSoundMutedState(next);
    setSoundMuted(next);
    if (!next) {
      await requestNotificationPermission();
      playTradeFillChime();
      showToast('🔊 已开启成交与极端风控音效及桌面通知', 'info');
    } else {
      showToast('🔇 已静音告警提示音', 'info');
    }
  };
  const [isCapitalModalOpen, setIsCapitalModalOpen] = useState(false);
  const { walletState, handleConnectWallet, handleDisconnectWallet } = useWalletSession({
    showToast,
    onAuthExpired: () => setIsWalletModalOpen(true),
  });

  // 2. 根据后端真实决策更新席位（映射逻辑见 data/decisionToAgents.js）
  const updateAgentsFromDecision = (record) => {
    if (!record || record.error) return;
    setAgents((prevAgents) => applyDecisionToAgents(prevAgents, record));
  };

  // 3. 真实拉取机器人列表
  const fetchBots = async () => {
    try {
      const bots = await listBots();
      if (Array.isArray(bots)) {
        setBotsList(bots);
        return bots;
      }
    } catch (e) {
      console.warn("读取机器人列表接口未连通，采用默认模型列表:", e);
    }
    return null;
  };

  // 3b. 删除指定机器人（含二次确认、鉴权检查与删除后自动切换活跃席位）
  const handleDeleteBot = async (bot) => {
    const id = bot?.bot_id || bot?.id;
    if (!id) return;
    if (!localStorage.getItem('heimdall_session_token')) {
      showToast('🔐 删除机器人需要先连接钱包签名鉴权', 'critical');
      setIsWalletModalOpen(true);
      return;
    }
    const label = bot?.name || id;
    const confirmed = await showConfirm(
      `确认停止并彻底删除机器人「${label}」(${id}) 吗？\n该操作将撤销其残留挂单并清理对应的历史决策、净值与成交流水，且不可恢复。`,
      { title: '删除机器人', confirmText: '停止并删除', danger: true }
    );
    if (!confirmed) return;
    try {
      await deleteBot(id);
      const updated = (await fetchBots()) || botsList.filter((b) => (b.bot_id || b.id) !== id);
      if (id === activeBotId) {
        const fallbackBot = updated[0];
        const nextId = fallbackBot ? (fallbackBot.bot_id || fallbackBot.id) : '';
        setActiveBotId(nextId);
        if (nextId) {
          localStorage.setItem('heimdall_active_bot_id', nextId);
          fetchBotLatest(nextId);
        } else {
          localStorage.removeItem('heimdall_active_bot_id');
          setDecisionRecord(null);
        }
      }
      showToast(`🗑️ 已停止并删除机器人: ${label}`, 'success');
    } catch (err) {
      showToast(`删除失败: ${err.message}`, 'critical');
    }
  };

  // 4. 真实拉取指定机器人的最新推演状态
  const fetchBotLatest = async (botId, refresh = false) => {
    try {
      const res = await getBotLatestState(botId, refresh);
      if (res && res.decision && Object.keys(res.decision).length > 0) {
        setDecisionRecord(res.decision);
        updateAgentsFromDecision(res.decision);
      } else if (res && res.symbol) {
        setDecisionRecord({ symbol: res.symbol, latest_price: 0 });
        setAgents(createPendingAgents());
      }
    } catch (e) {
      console.warn("拉取机器人推演状态异常:", e);
    }
  };

  // 5. 切换选中的机器人席位 (支持本地持久化记录与视图即刻对齐)
  const handleSelectBot = (id) => {
    setActiveBotId(id);
    localStorage.setItem("heimdall_active_bot_id", id);
    setDecisionRecord(null);
    // M5：切换机器人时清空上一台机器人的席位结论，避免串显
    setAgents(createPendingAgents());
    fetchBotLatest(id);
    const target = botsList.find(b => (b.bot_id || b.id) === id);
    showToast(`已切换至机器人: ${target?.name || id} (${target?.symbol || 'USDT'})`);
  };


  // 手动强制触发一轮最新决策推演与对弈刷新（后端已拦截匿名 refresh=true）
  const handleManualRefresh = async () => {
    if (!localStorage.getItem('heimdall_session_token')) {
      showToast('🔐 强制刷新 LLM 推演需要先连接钱包签名鉴权', 'critical');
      setIsWalletModalOpen(true);
      return;
    }
    setIsRefreshing(true);
    try {
      await fetchBotLatest(activeBotId, true);
      await fetchBots();
      showToast("🔄 对弈台已执行最新一轮推演并刷新", "success");
    } catch (e) {
      showToast("刷新失败: " + e.message, "critical");
    } finally {
      setIsRefreshing(false);
    }
  };

  // 初始化拉取
  useEffect(() => {
    fetchBots();
    fetchBotLatest(activeBotId);
  }, [activeBotId]);

  // 当 agents 列表随最新推演决策更新时，同步刷新正在弹窗展示的 selectedAgent 详情
  useEffect(() => {
    if (selectedAgent) {
      const updated = agents.find(a => a.id === selectedAgent.id);
      if (updated) {
        setSelectedAgent(updated);
      }
    }
  }, [agents]);

  // WebSocket 实时流式推流监听（完整消费 steps / orders / risk 事件 + 音效/桌面通知）
  const { isConnected: isWsConnected } = useWebSocket({
    topics: ['klines', 'steps', 'orders', 'risk', 'radar'],
    onStep: (result, botId) => {
      if (botId === activeBotId && result) {
        setDecisionRecord(result);
        updateAgentsFromDecision(result);
        showToast(`⚡ WebSocket 收到 [${botId}] 最新决策推演`, "info");
      }
    },
    onOrder: (orderEvt, botId) => {
      playTradeFillChime();
      const msg = `成交回报 [${botId || activeBotId}]: ${orderEvt?.symbol || ''} ${orderEvt?.execution_id || orderEvt?.order_id || ''}`;
      sendBrowserNotification('Heimdall 订单成交', msg);
      showToast(`✅ ${msg}`, 'success');
      fetchBots();
      getBotTrades(activeBotId, 30).then((res) => { if (Array.isArray(res)) setTrades(res); }).catch(() => {});
    },
    onRiskAlert: (alert) => {
      playRiskAlertBeep();
      const msg = alert?.message || alert?.action || '触发硬风控拦截';
      sendBrowserNotification('Heimdall 极端风控告警', msg);
      showToast(`🚨 风控熔断告警: ${msg}`, "critical");
    },
  });

  // 后台自动静默轮询 (若 WebSocket 处于连接状态则拉长轮询间隔至 15s 作为兜底)
  useEffect(() => {
    let timer = null;
    if (autoRefresh && activeTab === "arena") {
      timer = setInterval(() => {
        fetchBotLatest(activeBotId, false);
      }, isWsConnected ? 15000 : 4000);
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [autoRefresh, activeTab, activeBotId, isWsConnected]);

  // 获取当前机器人的成交流水，供 K 线图进行盘面交易打点
  useEffect(() => {
    getBotTrades(activeBotId, 30)
      .then((res) => {
        if (Array.isArray(res)) setTrades(res);
      })
      .catch(() => {});
  }, [activeBotId, decisionRecord]);


  // 紧急熔断急停动作响应 (真实调用后端接口)
  const handleEmergencyKill = async () => {
    try {
      const res = await triggerEmergencyKill(activeBotId);
      showToast(res.message || "🚨 [紧急熔断触发] 已撤销全部挂单并强制锁仓！", "critical");
      await fetchBots();
    } catch (e) {
      showToast(`🚨 急停指令下发: ${e.message}`, "critical");
    }
  };

  // 下发撮合订单 / 单步执行响应 (真实调用单步决策)
  const handleExecuteTrade = async () => {
    try {
      showToast(`⚡ 正在触发机器人 [${activeBotId}] 启动单步多 Agent 闭环推演...`, "info");
      const res = await stepBot(activeBotId);
      // 后端失败即关闭（宏观日历失效 / 凭证缺失 / 部署安全预检未通过 / 行情异常）时如实提示，不更新席位
      if (res && res.result && res.result.error) {
        showToast(`⛔ 本轮推演被拒绝: ${res.result.error}`, "critical");
        await fetchBots();
        return;
      }
      if (res && res.result) {
        setDecisionRecord(res.result);
        updateAgentsFromDecision(res.result);
        const p = res.result.trade_proposal;
        const sym = res.result.symbol || 'ETH';
        showToast(`⚡ [推演完成] ${sym} 现价 ${fmtUsd(res.result.latest_price)} | 结论: ${res.result.debate_consensus || '--'}${p ? ' | 已生成提案' : ' | 无提案'}`, "success");
      }
      await fetchBots();
    } catch (e) {
      showToast(`⚡ 触发单步决策: ${e.message}`, "info");
    }
  };

  const activeBot = botsList.find(b => (b.bot_id || b.id) === activeBotId);
  const currentSymbol = (activeBot?.symbol || decisionRecord?.symbol || (activeBotId.includes("btc") ? "BTC" : activeBotId.includes("sol") ? "SOL" : "ETH")).toUpperCase();

  return (
    <div className="h-screen w-screen flex flex-col bg-canvas text-ink overflow-hidden">
      
      {/* 1. 顶部视窗导航栏 */}
      <Header 
        activeTab={activeTab} 
        onTabChange={(tab) => {
          setActiveTab(tab);
          const tabNames = {
            arena: '对弈大厅',
            swarm: '蜂群编排',
            sentiment: '情绪与情报',
            nav: 'NAV 单位净值',
            risk: '七大铁律风控',
            audit: '审计轨迹'
          };
          showToast(`已切换至视图: ${tabNames[tab] || tab}`);
        }} 
        walletState={walletState}
        onOpenWalletModal={() => setIsWalletModalOpen(true)}
        onOpenKeyVaultModal={() => setIsKeyVaultModalOpen(true)}
        onDisconnectWallet={handleDisconnectWallet}
        activeBotId={activeBotId}
        activeBot={activeBot}
        symbol={currentSymbol}
        theme={theme}
        onToggleTheme={handleToggleTheme}
        soundMuted={soundMuted}
        onToggleSound={handleToggleSound}
        onToggleMobileSidebar={() => setMobileSidebarOpen((v) => !v)}
        onToggleMobileTerminal={() => setMobileTerminalOpen((v) => !v)}
      />

      {/* 2. 主体三栏工作区（大屏固定三栏，小屏支持左右抽屉折叠） */}
      <div className="flex-1 flex overflow-hidden relative">
        
        {/* 左侧栏：桌面端常驻，小屏滑出抽屉 */}
        <div className={`${mobileSidebarOpen ? 'fixed top-12 bottom-0 left-0 z-40 flex shadow-2xl' : 'hidden lg:flex'}`}>
          <Sidebar 
            activeBotId={activeBotId} 
            onSelectBot={(id) => {
              handleSelectBot(id);
              setMobileSidebarOpen(false);
            }}
            onDeleteBot={handleDeleteBot}
            walletState={walletState}
            onOpenWalletModal={() => setIsWalletModalOpen(true)}
            onOpenKeyVaultModal={() => setIsKeyVaultModalOpen(true)}
            onOpenCreateBotModal={() => setIsCreateBotModalOpen(true)}
            onOpenCapitalModal={() => setIsCapitalModalOpen(true)}
            botsList={botsList}
          />
        </div>

        {/* 中间多视图路由切换 */}
        {activeTab === 'arena' && (
          <DecisionArena 
            agents={agents}
            decisionRecord={decisionRecord}
            activeBotId={activeBotId}
            activeBot={activeBot}
            symbol={currentSymbol}
            onSelectAgent={(agent) => setSelectedAgent(agent)}
            onEmergencyKill={handleEmergencyKill}
            onExecuteTrade={handleExecuteTrade}
            onRefresh={handleManualRefresh}
            isRefreshing={isRefreshing}
            autoRefresh={autoRefresh}
            onToggleAutoRefresh={() => setAutoRefresh(prev => !prev)}
            trades={trades}
          />
        )}

        {activeTab === 'swarm' && (
          <SwarmView 
            bots={botsList}
            activeBotId={activeBotId}
            onSelectBot={(id) => {
              handleSelectBot(id);
              setActiveTab("arena");
            }}
            onDeleteBot={handleDeleteBot}
            onRefreshBots={fetchBots}
            onOpenCreateModal={() => setIsCreateBotModalOpen(true)}
            onOpenCapitalModal={() => setIsCapitalModalOpen(true)}
            onShowToast={showToast}
          />
        )}

        {activeTab === 'sentiment' && (
          <SentimentView 
            symbol={currentSymbol}
            activeBot={activeBot}
            sentimentReport={decisionRecord?.sentiment_report}
          />
        )}

        {activeTab === 'nav' && (
          <NavView activeBotId={activeBotId} symbol={currentSymbol} />
        )}

        {activeTab === 'backtest' && (
          <BacktestView
            onShowToast={showToast}
            onApplyToCreateBot={(cfg) => {
              setPrefillBotConfig(cfg);
              setIsCreateBotModalOpen(true);
            }}
          />
        )}

        {activeTab === 'risk' && (
          <RiskRadarView onEmergencyKill={handleEmergencyKill} symbol={currentSymbol} activeBotId={activeBotId} />
        )}

        {activeTab === 'audit' && (
          <AuditTracesView activeBotId={activeBotId} symbol={currentSymbol} />
        )}

        {/* 右侧栏：桌面端常驻，小屏滑出抽屉 */}
        <div className={`${mobileTerminalOpen ? 'fixed top-12 bottom-0 right-0 z-40 flex shadow-2xl' : 'hidden lg:flex'}`}>
          <TerminalPanel 
            decisionRecord={decisionRecord}
            activeBotId={activeBotId}
            isWsConnected={isWsConnected}
            symbol={currentSymbol}
            onExecuteTrade={handleExecuteTrade}
            onEmergencyKill={handleEmergencyKill}
          />
        </div>

      </div>

      {/* 3. 点击任意 Agent 弹出的深度决策原因解析抽屉 */}
      {selectedAgent && (
        <AgentModal 
          agent={selectedAgent} 
          onClose={() => setSelectedAgent(null)} 
        />
      )}

      {/* 4. Web3 钱包登录与签名鉴权弹窗 */}
      <WalletModal 
        isOpen={isWalletModalOpen}
        onClose={() => setIsWalletModalOpen(false)}
        walletState={walletState}
        onConnectWallet={handleConnectWallet}
        onDisconnectWallet={handleDisconnectWallet}
      />

      {/* 5. 敏感 API Key 与交易所私钥管理安全金库 */}
      <KeyVaultModal 
        isOpen={isKeyVaultModalOpen}
        onClose={() => setIsKeyVaultModalOpen(false)}
        walletState={walletState}
        onConnectWallet={handleConnectWallet}
      />

      {/* 6. 新建多智能体策略机器人弹窗 */}
      <CreateBotModal
        isOpen={isCreateBotModalOpen}
        prefill={prefillBotConfig}
        onClose={() => {
          setIsCreateBotModalOpen(false);
          setPrefillBotConfig(null);
        }}
        onCreated={(newBotId) => {
          fetchBots();
          setActiveBotId(newBotId);
          showToast(`🎉 成功创建机器人 [${newBotId}]！已无缝装载席位`, "success");
        }}
      />

      {/* 7. 组合资金配比看板弹窗 */}
      <CapitalDistributionModal
        isOpen={isCapitalModalOpen}
        onClose={() => setIsCapitalModalOpen(false)}
        onUpdated={() => {
          fetchBots();
          showToast("📊 机器人组合资金配比已同步更新", "success");
        }}
      />

      {/* 6. 全局浮动操作反馈 Toast */}
      <Toast notification={notification} />

    </div>
  );
}
