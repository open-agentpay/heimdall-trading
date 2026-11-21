/**
 * ================================================================================
 * Heimdall 交易平台 - 蜂群编排与多机器人集群管控中心 (SwarmView)
 * ================================================================================
 *
 * 核心功能：
 * 1. 蜂群集群资产与策略全景态势（总本金、运行中机器人比例、24h总吞吐量）；
 * 2. 机器人卡片墙矩阵（支持多模型、多标的、多策略类型并行监控）；
 * 3. 批量一键启停与单机器人立即推演（Step）执行控制；
 * 4. 联动新建机器人与资金划转配比调节。
 */

import React, { useState } from 'react';
import { 
  Cpu, Play, Square, RefreshCw, Plus, PieChart, ShieldCheck, 
  ExternalLink, Layers, ArrowUpRight, Activity, TrendingUp, AlertCircle, Trash2
} from 'lucide-react';
import { startBot, stopBot, stepBot, setBotMacroBias } from '../api/endpoints';

export default function SwarmView({
  bots = [],
  activeBotId,
  onSelectBot,
  onDeleteBot,
  onRefreshBots,
  onOpenCreateModal,
  onOpenCapitalModal,
  onShowToast,
}) {
  const [loadingActions, setLoadingActions] = useState({});

  // 1. 批量全部启动
  const handleBatchStart = async () => {
    onShowToast?.("🚀 正在批量启动所有待命中机器人...", "info");
    for (const b of bots) {
      if (b.status !== "RUNNING") {
        try {
          await startBot(b.bot_id || b.id, 60);
        } catch (e) {
          console.warn(`启动 ${b.bot_id} 告警:`, e);
        }
      }
    }
    await onRefreshBots?.();
    onShowToast?.("✅ 所有策略机器人已全部投入后台盯盘！", "success");
  };

  // 2. 批量全部挂起
  const handleBatchStop = async () => {
    onShowToast?.("⏸️ 正在批量挂起所有运行中机器人...", "info");
    for (const b of bots) {
      if (b.status === "RUNNING") {
        try {
          await stopBot(b.bot_id || b.id);
        } catch (e) {
          console.warn(`挂起 ${b.bot_id} 告警:`, e);
        }
      }
    }
    await onRefreshBots?.();
    onShowToast?.("⏹️ 蜂群策略协程已全量安全挂起", "info");
  };

  // 3. 单个机器人启停切换
  const handleToggleBot = async (b) => {
    const id = b.bot_id || b.id;
    setLoadingActions(prev => ({ ...prev, [id]: true }));
    try {
      if (b.status === "RUNNING") {
        await stopBot(id);
        onShowToast?.(`⏹️ 机器人 [${b.name || id}] 已挂起`, "info");
      } else {
        await startBot(id, 60);
        onShowToast?.(`▶️ 机器人 [${b.name || id}] 已启动盯盘`, "success");
      }
      await onRefreshBots?.();
    } catch (err) {
      onShowToast?.(`操作失败: ${err.message}`, "critical");
    } finally {
      setLoadingActions(prev => ({ ...prev, [id]: false }));
    }
  };

  // 4. 单步立即推演
  const handleSingleStep = async (b) => {
    const id = b.bot_id || b.id;
    setLoadingActions(prev => ({ ...prev, [`step_${id}`]: true }));
    try {
      await stepBot(id);
      onShowToast?.(`⚡ 机器人 [${b.name || id}] 完成单步多智能体推演`, "success");
      await onRefreshBots?.();
    } catch (err) {
      onShowToast?.(`推演失败: ${err.message}`, "critical");
    } finally {
      setLoadingActions(prev => ({ ...prev, [`step_${id}`]: false }));
    }
  };

  // 5. 低频 LLM MacroBias 旁路门控快捷切换（降仓 0.5x <-> 恢复 NORMAL）
  const handleToggleMacroBias = async (b) => {
    const id = b.bot_id || b.id;
    const isActive = Boolean(b.macro_bias);
    try {
      if (isActive) {
        await setBotMacroBias(id, { action: 'NORMAL', risk_scale: 1.0, blocked_directions: [] });
        onShowToast?.(`✅ 已清除 [${b.name || id}] 宏观门控，恢复正常仓位`, 'success');
      } else {
        await setBotMacroBias(id, {
          action: 'REDUCE_RISK',
          risk_scale: 0.5,
          blocked_directions: [],
          reason: '蜂群控制台手动启用 0.5x 防守降仓门控',
          ttl_minutes: 60,
        });
        onShowToast?.(`🛡️ 已为 [${b.name || id}] 启用 MacroBias 0.5x 降仓门控 (60分钟)`, 'info');
      }
      await onRefreshBots?.();
    } catch (err) {
      onShowToast?.(`宏观门控设置失败: ${err.message}`, 'critical');
    }
  };

  // 统计概览指标
  const totalBots = bots.length;
  const runningBots = bots.filter(b => b.status === "RUNNING").length;
  const totalCapital = bots.reduce((sum, b) => sum + (Number(b.allocated_capital) || 0), 0);

  return (
    <div className="flex-1 flex flex-col bg-[#F6F2EB] overflow-hidden select-none">
      
      {/* 顶部控制与操作栏 */}
      <div className="h-14 bg-[#FAF7F2] border-b border-[#E6DFD5] px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-[#EFE9DF] border border-[#DFD6C7] flex items-center justify-center text-[#DE6438]">
            <Cpu className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-[#1F1E1B] tracking-tight">蜂群编排与集群管控 (Swarm)</h1>
            <p className="text-[10px] text-[#8C8275]">
              多策略并行矩阵 · 划拨本金 ${totalCapital.toLocaleString()} · 运行状态 {runningBots}/{totalBots}
            </p>
          </div>
        </div>

        {/* 右侧批量控制与弹窗触发按钮 */}
        <div className="flex items-center space-x-2">
          <button
            onClick={handleBatchStart}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-medium text-[#3D784E] bg-[#3D784E]/10 hover:bg-[#3D784E]/20 border border-[#3D784E]/30 rounded shadow-2xs transition-all cursor-pointer"
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            <span>全量启动</span>
          </button>

          <button
            onClick={handleBatchStop}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-medium text-[#7E7469] bg-white hover:bg-[#EFE9DF] border border-[#DFD6C7] rounded shadow-2xs transition-all cursor-pointer"
          >
            <Square className="w-3.5 h-3.5 fill-current" />
            <span>全量挂起</span>
          </button>

          <button
            onClick={onOpenCapitalModal}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-medium text-[#2E2823] bg-white hover:bg-[#EFE9DF] border border-[#DFD6C7] rounded shadow-2xs transition-all cursor-pointer"
          >
            <PieChart className="w-3.5 h-3.5" />
            <span>资金配比</span>
          </button>

          <button
            onClick={onOpenCreateModal}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-[#DE6438] hover:bg-[#C9552D] rounded shadow-xs transition-all cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>新建机器人</span>
          </button>
        </div>
      </div>

      {/* 滚动卡片列表区 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        
        {/* 集群状态总览卡片 */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="p-4 rounded-xl border border-[#DFD6C7] bg-[#FAF7F2] shadow-2xs flex items-center justify-between">
            <div>
              <div className="text-[10px] text-[#8C8275] uppercase tracking-wider">活跃策略数</div>
              <div className="text-xl font-bold text-[#1F1E1B] mt-0.5">{runningBots} / {totalBots}</div>
            </div>
            <div className="w-10 h-10 rounded-lg bg-[#3D784E]/10 flex items-center justify-center text-[#3D784E]">
              <Activity className="w-5 h-5" />
            </div>
          </div>

          <div className="p-4 rounded-xl border border-[#DFD6C7] bg-[#FAF7F2] shadow-2xs flex items-center justify-between">
            <div>
              <div className="text-[10px] text-[#8C8275] uppercase tracking-wider">划拨总本金</div>
              <div className="text-xl font-bold font-mono text-[#1F1E1B] mt-0.5">${totalCapital.toLocaleString()}</div>
            </div>
            <div className="w-10 h-10 rounded-lg bg-[#DE6438]/10 flex items-center justify-center text-[#DE6438]">
              <Layers className="w-5 h-5" />
            </div>
          </div>

          <div className="p-4 rounded-xl border border-[#DFD6C7] bg-[#FAF7F2] shadow-2xs flex items-center justify-between">
            <div>
              <div className="text-[10px] text-[#8C8275] uppercase tracking-wider">平均轮询周期</div>
              <div className="text-xl font-bold font-mono text-[#1F1E1B] mt-0.5">60s 盯盘</div>
            </div>
            <div className="w-10 h-10 rounded-lg bg-[#3B72C2]/10 flex items-center justify-center text-[#3B72C2]">
              <TrendingUp className="w-5 h-5" />
            </div>
          </div>

          <div className="p-4 rounded-xl border border-[#DFD6C7] bg-[#FAF7F2] shadow-2xs flex items-center justify-between">
            <div>
              <div className="text-[10px] text-[#8C8275] uppercase tracking-wider">风控防御等级</div>
              <div className="text-xl font-bold text-[#3D784E] mt-0.5">7重全防护</div>
            </div>
            <div className="w-10 h-10 rounded-lg bg-[#3D784E]/10 flex items-center justify-center text-[#3D784E]">
              <ShieldCheck className="w-5 h-5" />
            </div>
          </div>
        </div>

        {/* 蜂群机器人卡片墙 */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-bold text-[#1F1E1B] uppercase tracking-wider flex items-center space-x-1.5">
              <span>机器人编队矩阵</span>
              <span className="text-[10px] text-[#8C8275] font-normal">({bots.length} 台在编)</span>
            </h2>
            <button 
              onClick={onRefreshBots}
              className="text-[11px] text-[#7E7469] hover:text-[#1F1E1B] flex items-center space-x-1 cursor-pointer"
            >
              <RefreshCw className="w-3 h-3" />
              <span>刷新集群状态</span>
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {bots.map((bot) => {
              const id = bot.bot_id || bot.id;
              const isRunning = bot.status === "RUNNING";
              const isSelected = id === activeBotId;
              const isBusy = loadingActions[id] || false;
              const isStepping = loadingActions[`step_${id}`] || false;

              return (
                <div
                  key={id}
                  className={`p-5 rounded-xl border transition-all flex flex-col justify-between bg-[#FAF7F2] shadow-2xs ${
                    isSelected ? "border-[#DE6438] ring-1 ring-[#DE6438]" : "border-[#DFD6C7] hover:border-[#BFB39E]"
                  }`}
                >
                  <div>
                    {/* 卡片头部 */}
                    <div className="flex items-start justify-between">
                      <div className="flex items-center space-x-2">
                        <span className="text-base font-bold font-mono text-[#DE6438] bg-[#EFE9DF] px-2 py-0.5 rounded border border-[#DFD6C7]">
                          {bot.symbol || "ETH"}
                        </span>
                        <div>
                          <h3 className="text-sm font-bold text-[#1F1E1B]">{bot.name || id}</h3>
                          <span className="text-[10px] font-mono text-[#8C8275]">{id}</span>
                        </div>
                      </div>

                      <span
                        className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                          isRunning
                            ? "bg-[#3D784E]/10 text-[#3D784E] border-[#3D784E]/20"
                            : "bg-[#7E7469]/10 text-[#7E7469] border-[#7E7469]/20"
                        }`}
                      >
                        {isRunning ? "● 运行中" : "○ 待命中"}
                      </span>
                    </div>

                    {/* 策略详情参数 */}
                    <div className="mt-4 grid grid-cols-2 gap-2 text-xs bg-[#EFE9DF]/40 p-3 rounded-lg border border-[#E6DFD5]">
                      <div>
                        <span className="text-[10px] text-[#8C8275]">策略模型</span>
                        <div className="font-semibold text-[#2E2823] mt-0.5 text-[11px] font-mono">
                          {bot.strategy || bot.strategy_type || 'TREND_FOLLOW'}
                        </div>
                      </div>
                      <div>
                        <span className="text-[10px] text-[#8C8275]">划拨本金</span>
                        <div className="font-bold font-mono text-[#2E2823] mt-0.5 text-[11px]">
                          ${Number(bot.allocated_capital || 10000).toLocaleString()}
                        </div>
                      </div>
                      <div>
                        <span className="text-[10px] text-[#8C8275]">交易所 · 运行模式</span>
                        <div className="font-semibold text-[#2E2823] mt-0.5 text-[11px] font-mono">
                          {bot.exchange || 'BINANCE'} · {bot.execution_mode || 'PAPER'}
                        </div>
                      </div>
                      <div>
                        <span className="text-[10px] text-[#8C8275]">宏观门控 (MacroBias)</span>
                        <div className="mt-0.5 flex items-center justify-between">
                          <span className={`text-[10px] font-mono font-bold ${bot.macro_bias ? 'text-[#C28B38]' : 'text-[#3D784E]'}`}>
                            {bot.macro_bias ? `${bot.macro_bias.action} (${bot.macro_bias.risk_scale}x)` : 'NORMAL'}
                          </span>
                          <button
                            onClick={() => handleToggleMacroBias(bot)}
                            className="text-[10px] underline text-[#DE6438] cursor-pointer"
                          >
                            {bot.macro_bias ? '解除' : '降仓'}
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* 底部操作工具栏 */}
                  <div className="mt-5 pt-3 border-t border-[#EFE9DF] flex items-center justify-between">
                    <button
                      onClick={() => onSelectBot?.(id)}
                      className={`text-xs font-semibold flex items-center space-x-1 cursor-pointer transition ${
                        isSelected ? "text-[#DE6438]" : "text-[#7E7469] hover:text-[#1F1E1B]"
                      }`}
                    >
                      <span>{isSelected ? "当前监控中" : "切换席位"}</span>
                      <ArrowUpRight className="w-3.5 h-3.5" />
                    </button>

                    <div className="flex items-center space-x-1.5">
                      <button
                        onClick={() => handleSingleStep(bot)}
                        disabled={isStepping}
                        title="立即单步推演"
                        className="px-2.5 py-1 text-xs text-[#5C5549] hover:text-[#1F1E1B] bg-white border border-[#DFD6C7] rounded hover:border-[#1F1E1B] transition cursor-pointer flex items-center space-x-1"
                      >
                        <RefreshCw className={`w-3 h-3 ${isStepping ? 'animate-spin' : ''}`} />
                        <span>推演</span>
                      </button>

                      <button
                        onClick={() => handleToggleBot(bot)}
                        disabled={isBusy}
                        className={`px-3 py-1 text-xs font-semibold rounded transition cursor-pointer flex items-center space-x-1 ${
                          isRunning
                            ? "bg-[#DE6438]/10 text-[#DE6438] hover:bg-[#DE6438]/20 border border-[#DE6438]/30"
                            : "bg-[#3D784E]/10 text-[#3D784E] hover:bg-[#3D784E]/20 border border-[#3D784E]/30"
                        }`}
                      >
                        {isRunning ? <Square className="w-3 h-3 fill-current" /> : <Play className="w-3 h-3 fill-current" />}
                        <span>{isRunning ? "挂起" : "启动"}</span>
                      </button>

                      {onDeleteBot && (
                        <button
                          type="button"
                          onClick={() => onDeleteBot(bot)}
                          title={`删除机器人 ${bot.name || id}`}
                          className="p-1.5 text-xs text-[#8C8275] hover:text-[#C24D4D] bg-white hover:bg-[#C24D4D]/10 border border-[#DFD6C7] hover:border-[#C24D4D]/40 rounded transition cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </div>

                </div>
              );
            })}
          </div>
        </div>

      </div>
    </div>
  );
}
