/**
 * ================================================================================
 * Heimdall 交易平台 - 前端应用主入口组件 (App.jsx)
 * ================================================================================
 * 
 * 【小白速懂】：
 * 本组件是整个前端界面的“大脑中枢”：
 * 1. 维护全局状态：当前正在查看哪个一级菜单（对弈大厅、K线、情绪、风控等）；
 * 2. 维护正在点击查看的智能体详情弹窗（选中的 Agent 对象）；
 * 3. 联动左侧机器人选择栏、中间多智能体竞技场以及右侧思维链终端。
 */

import React, { useState } from 'react';
import Header from './components/Header';
import Sidebar from './components/Sidebar';
import DecisionArena from './components/DecisionArena';
import TerminalPanel from './components/TerminalPanel';
import AgentModal from './components/AgentModal';
import { INITIAL_AGENTS } from './data/mockData';

export default function App() {
  const [activeTab, setActiveTab] = useState("arena");
  const [activeBotId, setActiveBotId] = useState("bot_eth_debate_01");
  const [selectedAgent, setSelectedAgent] = useState(null);
  const [agents, setAgents] = useState(INITIAL_AGENTS);
  const [notification, setNotification] = useState(null);

  // 显示操作浮窗提示
  const showToast = (msg, type = "info") => {
    setNotification({ msg, type });
    setTimeout(() => setNotification(null), 3000);
  };

  // 紧急熔断急停动作响应
  const handleEmergencyKill = () => {
    showToast("🚨 [紧急熔断触发] 已撤销全部挂单并强制锁仓！", "critical");
  };

  // 下发撮合订单响应
  const handleExecuteTrade = () => {
    showToast("⚡ [撮合成功] PaperExecution 已成交 1.5 ETH @ $3,120.00", "success");
  };

  return (
    <div className="h-screen w-screen flex flex-col bg-[#F6F2EB] text-[#2E2823] overflow-hidden">
      
      <!-- 1. 顶部视窗导航栏 -->
      <Header 
        activeTab={activeTab} 
        onTabChange={(tab) => {
          setActiveTab(tab);
          showToast(`已切换至标签: ${tab}`);
        }} 
      />

      <!-- 2. 主体三栏工作区 -->
      <div className="flex-1 flex overflow-hidden">
        
        <!-- 左侧栏：机器人选择与信号源树 -->
        <Sidebar 
          activeBotId={activeBotId} 
          onSelectBot={(id) => {
            setActiveBotId(id);
            showToast(`已切换至机器人: ${id}`);
          }} 
        />

        <!-- 中间：多智能体环形对弈台 (包含 K 线图模式与各个 Agent 角色) -->
        <DecisionArena 
          agents={agents}
          onSelectAgent={(agent) => setSelectedAgent(agent)}
          onEmergencyKill={handleEmergencyKill}
          onExecuteTrade={handleExecuteTrade}
        />

        <!-- 右侧栏：大模型思维链日志与执行控制台 -->
        <TerminalPanel />

      </div>

      <!-- 3. 点击任意 Agent 弹出的深度决策原因解析抽屉 -->
      {selectedAgent && (
        <AgentModal 
          agent={selectedAgent} 
          onClose={() => setSelectedAgent(null)} 
        />
      )}

      <!-- 4. 全局浮动操作反馈 Toast -->
      {notification && (
        <div 
          className={`fixed bottom-6 right-6 z-50 px-4 py-2.5 rounded-xl shadow-lg border text-xs font-semibold flex items-center space-x-2 animate-bounce ${
            notification.type === "critical"
              ? "bg-[#C24D4D] text-white border-[#A83838]"
              : notification.type === "success"
              ? "bg-[#4D835C] text-white border-[#3B6A48]"
              : "bg-[#FAF7F2] text-[#2E2823] border-[#D8CEBE]"
          }`}
        >
          <span>{notification.msg}</span>
        </div>
      )}

    </div>
  );
}
