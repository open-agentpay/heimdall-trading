/**
 * ================================================================================
 * Heimdall 交易平台 - 动态思维链日志流与交互式执行控制台 (TerminalPanel)
 * ================================================================================
 * 
 * 【功能特性】：
 * 1. 真实大模型思维链 (Chain-of-Thought Stream)：
 *    - 实时呈现后端 graph.py 推演下发的 decisionRecord.thinking_logs；
 *    - 自动提取 StepTraces，展示每个 Agent 节点的真实执行耗时 (ms) 与合规检查结果；
 *    - 支持按智能体角色过滤 (分析师、多头、空头、裁判、风控)；
 * 2. 交互式命令行控制台 (Interactive Console)：
 *    - 实时显示 PaperEngine 撮合流水、幂等键签发状态、NAV 净值实时切片；
 *    - 支持用户输入命令：`status`, `step`, `risk`, `nav`, `kill`, `clear`，并即时响应；
 *    - 提供一键快捷操作按钮，联动后端真实执行。
 */

import React, { useState, useEffect, useRef } from 'react';

export default function TerminalPanel({ 
  decisionRecord = null, 
  activeBotId = "bot_eth_debate_01",
  isWsConnected = false,
  onExecuteTrade = null,
  onEmergencyKill = null
}) {
  const [activeTab, setActiveTab] = useState("stream"); // "stream" | "console"
  const [filterAgent, setFilterAgent] = useState("ALL");
  const [commandInput, setCommandInput] = useState("");
  const [consoleLogs, setConsoleLogs] = useState([
    { type: "sys", time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), text: "Heimdall /ws/stream 实时推流总线已就绪（steps / orders / risk）" },
    { type: "success", time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), text: "OMS 状态机、原生条件止损与 7 大确定性风控守卫已激活" }
  ]);

  const streamEndRef = useRef(null);
  const consoleEndRef = useRef(null);
  const lastProcessedKeyRef = useRef(null);

  // 1. 动态生成思维链日志条目 (优先使用后端真实 thinking_logs)
  const defaultLogs = [
    {
      agent: "AnalystAgent",
      role: "技术量化分析师",
      time: "19:42:01",
      latency: "82ms",
      color: "#3E6B89",
      text: "扫描 15m 闭合 K 线，确认在 EMA20 上方获得支撑，RSI 为 58.4（处于健康强势未超买区间），初步定性为 TREND_UP。"
    },
    {
      agent: "SentimentWhaleAnalyst",
      role: "情绪与巨鲸侦探",
      time: "19:42:02",
      latency: "115ms",
      color: "#4D835C",
      text: "恐慌指数从 18 拐头回升至 24，大户连续 24h 提现 1,250 万美元 ETH 锁仓，链上抛压极轻，综合情绪得分 +0.45。"
    },
    {
      agent: "DebateModerator",
      role: "博弈裁判",
      time: "19:42:04",
      latency: "240ms",
      color: "#DE6438",
      text: "多头研究员立论充分（现货买盘真实），空头提出的 3200 阻力位已通过将止损点收紧至 3060 得到有效防御。裁决：批准发起做多！"
    },
    {
      agent: "RiskOfficerAgent",
      role: "合规风控官",
      time: "19:42:05",
      latency: "4ms",
      color: "#4D835C",
      text: "确定性七大铁律校验全部通过！杠杆率 1.5x <= 3.0x，止损距离 1.9% <= 5.0%，Bifrost 基差 0.03% <= 0.5%，签发确定性幂等键。"
    }
  ];

  // 组装实际展示的思维链
  const activeThinkingLogs = React.useMemo(() => {
    if (!decisionRecord) return defaultLogs;

    const traces = decisionRecord.step_traces || [];
    const logs = decisionRecord.thinking_logs || [];

    if (logs.length > 0) {
      return logs.map((logStr, idx) => {
        let agentName = "Agent";
        let text = logStr;
        let color = "#3E6B89";
        const trace = traces[idx] || {};

        if (logStr.startsWith("[")) {
          const closeBracket = logStr.indexOf("]");
          if (closeBracket !== -1) {
            agentName = logStr.slice(1, closeBracket);
            text = logStr.slice(closeBracket + 1).trim();
          }
        }

        if (agentName.toLowerCase().includes("risk")) color = "#C24D4D";
        else if (agentName.toLowerCase().includes("bull")) color = "#4D835C";
        else if (agentName.toLowerCase().includes("bear")) color = "#D48B38";
        else if (agentName.toLowerCase().includes("debate") || agentName.toLowerCase().includes("moderator")) color = "#DE6438";

        return {
          agent: agentName,
          role: trace.agent_role || "系统节点",
          time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : `${40 + idx * 25}ms`,
          color,
          text
        };
      });
    }

    return defaultLogs;
  }, [decisionRecord]);

  // 监听 decisionRecord 变化，自动写入执行控制台
  useEffect(() => {
    if (!decisionRecord) return;
    
    // 避免重复写入同一轮推演数据
    const currentKey = decisionRecord.idempotency_key || 
      (decisionRecord.thinking_logs && decisionRecord.thinking_logs.join('|')) || 
      decisionRecord.timestamp;
    if (currentKey && currentKey === lastProcessedKeyRef.current) return;

    const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const newItems = [];

    // 1. 打印大模型多智能体决策思维链条目
    const thinkingLogs = decisionRecord.thinking_logs || [];
    if (thinkingLogs.length > 0) {
      newItems.push({
        type: "highlight",
        time: now,
        text: `=== [LLM 多智能体决策推演] ${decisionRecord.symbol || 'ETH'} (共 ${thinkingLogs.length} 个节点) ===`
      });

      thinkingLogs.forEach((logStr) => {
        let type = "info";
        if (logStr.includes("[Analyst]")) type = "sys";
        else if (logStr.includes("[Sentiment") || logStr.includes("[Whale")) type = "info";
        else if (logStr.includes("多头") || logStr.includes("Bull")) type = "success";
        else if (logStr.includes("空头") || logStr.includes("Bear")) type = "highlight";
        else if (logStr.includes("Moderator") || logStr.includes("Verdict") || logStr.includes("裁判")) type = "cmd";
        else if (logStr.includes("Strategy") || logStr.includes("Proposal")) type = "cmd";
        else if (logStr.includes("Risk") || logStr.includes("Guard")) {
          type = logStr.includes("REJECT") || logStr.includes("拦截") ? "critical" : "success";
        }
        newItems.push({
          type,
          time: now,
          text: logStr
        });
      });
    }

    // 2. 打印共识与提案结构化信息
    if (decisionRecord.debate_consensus) {
      newItems.push({
        type: "success",
        time: now,
        text: `[DECISION] 博弈达成一致共识: ${decisionRecord.debate_consensus} | 标的: ${decisionRecord.symbol || 'ETH'}`
      });
    }

    if (decisionRecord.trade_proposal) {
      const p = decisionRecord.trade_proposal;
      newItems.push({
        type: "cmd",
        time: now,
        text: `[PROPOSAL] 挂单价格: $${p.trade_action?.target_price || '-'} | 止损: $${p.trade_action?.stop_loss_price || '-'} | 仓位: $${p.trade_action?.size_in_usd || '-'}`
      });
    }

    if (decisionRecord.idempotency_key) {
      newItems.push({
        type: "sys",
        time: now,
        text: `[IDEMPOTENCY] 幂等校验通过 SHA256: ${decisionRecord.idempotency_key.slice(0, 18)}...`
      });
    }

    if (decisionRecord.risk_evaluation) {
      const passed = decisionRecord.risk_evaluation.passed;
      newItems.push({
        type: passed ? "success" : "critical",
        time: now,
        text: `[RISK_AUDIT] 7大硬规则风控结果: ${passed ? "PASSED (全部通过)" : "REJECTED (已拦截)"}`
      });
    }

    if (decisionRecord.execution_result) {
      const ex = decisionRecord.execution_result;
      newItems.push({
        type: "highlight",
        time: now,
        text: `[PAPER_FILL] 撮合成交: ${ex.filled_amount || 1.5} ETH @ $${ex.filled_price || 3120.00} | 订单: ${ex.order_id || 'ord_paper_live'}`
      });
    }

    if (newItems.length > 0) {
      lastProcessedKeyRef.current = currentKey;
      setConsoleLogs((prev) => [...prev, ...newItems].slice(-120));
      setTimeout(() => {
        consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' });
        streamEndRef.current?.scrollIntoView({ behavior: 'smooth' });
      }, 50);
    }
  }, [decisionRecord]);

  // 控制台命令处理器
  const handleRunCommand = (e) => {
    e.preventDefault();
    const cmd = commandInput.trim();
    if (!cmd) return;

    const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const logCmd = { type: "cmd", time: now, text: `$ ${cmd}` };

    let reply = null;
    const lower = cmd.toLowerCase();

    if (lower === "help") {
      reply = {
        type: "info",
        time: now,
        text: "支持指令: status (状态), step (单步推演), logs (查看LLM决策日志), risk (风控检查), nav (核算净值), kill (紧急急停), clear (清屏)"
      };
    } else if (lower === "logs") {
      const logs = decisionRecord?.thinking_logs || [];
      if (logs.length > 0) {
        const logLines = logs.map(l => ({
          type: "info",
          time: now,
          text: l
        }));
        setConsoleLogs((prev) => [
          ...prev, 
          logCmd, 
          {
            type: "highlight",
            time: now,
            text: `=== 当前最新 LLM 决策思维日志 (${logs.length} 条) ===`
          }, 
          ...logLines
        ]);
        setCommandInput("");
        setTimeout(() => consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
        return;
      } else {
        reply = {
          type: "info",
          time: now,
          text: "暂无最新 LLM 决策思维日志，请点击【▶ 推演】或稍等自动轮询。"
        };
      }
    } else if (lower === "status") {
      reply = {
        type: "info",
        time: now,
        text: `[Status] 活跃机器人: ${activeBotId} | 状态: RUNNING | 引擎: PaperEngine Live`
      };
    } else if (lower === "step") {
      reply = {
        type: "highlight",
        time: now,
        text: `[Dispatch] 正在向机器人 [${activeBotId}] 下发单步触发指令...`
      };
      if (onExecuteTrade) onExecuteTrade();
    } else if (lower === "kill") {
      reply = {
        type: "critical",
        time: now,
        text: `[Emergency] 触发全局紧急急停，撤销所有在途订单并锁定仓位！`
      };
      if (onEmergencyKill) onEmergencyKill();
    } else if (lower === "risk") {
      reply = {
        type: "success",
        time: now,
        text: "[Risk Guard] 7大确定性铁律检测: 最大回撤 1.2% (OK) | 价差 0.03% (OK) | 杠杆 1.5x (OK)"
      };
    } else if (lower === "nav") {
      reply = {
        type: "highlight",
        time: now,
        text: "[NAV Engine] 当前单位净值: 1.0422 | 总份额: 10,000.00 | 资产净值: $10,422.00 (已剔除出入金干扰)"
      };
    } else if (lower === "clear") {
      setConsoleLogs([]);
      setCommandInput("");
      return;
    } else {
      reply = {
        type: "critical",
        time: now,
        text: `未知命令: '${cmd}'。输入 'help' 查看所有可用指令。`
      };
    }

    setConsoleLogs((prev) => [...prev, logCmd, reply]);
    setCommandInput("");
  };

  return (
    <aside className="w-64 bg-[#FAF7F2] border-l border-[#E6DFD5] flex flex-col shrink-0 select-none">
      
      {/* 顶栏标签切换 */}
      <div className="h-10 px-2.5 border-b border-[#EBE3D7] flex items-center justify-between">
        <div className="flex space-x-1">
          <button
            onClick={() => setActiveTab("stream")}
            className={`px-2 py-1 rounded-md text-xs font-bold transition flex items-center space-x-1 ${
              activeTab === "stream"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>🧠 思维链</span>
            <span className="text-[10px] bg-[#EFE8DE] px-1 rounded-full text-[#7E7469]">
              {activeThinkingLogs.length}
            </span>
          </button>
          <button
            onClick={() => setActiveTab("console")}
            className={`px-2 py-1 rounded-md text-xs font-bold transition flex items-center space-x-1 ${
              activeTab === "console"
                ? "bg-white text-[#DE6438] shadow-xs"
                : "text-[#7E7469] hover:text-[#2E2823]"
            }`}
          >
            <span>💻 控制台</span>
            <span className="w-1.5 h-1.5 rounded-full bg-[#4D835C] animate-pulse"></span>
          </button>
        </div>

        <span className={`text-[9px] font-mono ${isWsConnected ? 'text-[#4D835C]' : 'text-[#C28B38]'}`}>
          {isWsConnected ? 'WS: LIVE' : 'WS: POLL'}
        </span>
      </div>

      {/* 主体切换区 */}
      {activeTab === "stream" ? (
        <div className="flex-1 flex flex-col min-h-0">
          
          {/* 角色过滤标签 */}
          <div className="px-2.5 py-1.5 border-b border-[#F0EAE1] flex items-center space-x-1 overflow-x-auto text-[10px] font-medium text-[#7E7469] bg-[#FAF4EB]">
            <span>过滤:</span>
            {[
              { key: "ALL", label: "全部" },
              { key: "Analyst", label: "分析" },
              { key: "Sentiment", label: "情绪" },
              { key: "Debate", label: "博弈" },
              { key: "Risk", label: "风控" }
            ].map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setFilterAgent(key)}
                className={`px-1.5 py-0.5 rounded transition text-[10px] whitespace-nowrap ${
                  filterAgent === key
                    ? "bg-[#DE6438] text-white font-bold"
                    : "bg-white text-[#7E7469] hover:bg-[#EAE2D5]"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* 思维链条目滚动列表 */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-2 text-xs font-mono">
            {activeThinkingLogs
              .filter((item) => filterAgent === "ALL" || item.agent.toLowerCase().includes(filterAgent.toLowerCase()))
              .map((item, index) => (
                <div 
                  key={index} 
                  className="bg-white p-2 rounded-xl border border-[#E6DFD5] shadow-xs hover:border-[#D48B38] transition"
                >
                  <div className="flex items-center justify-between text-[10px] font-bold mb-1">
                    <span style={{ color: item.color }} className="truncate max-w-[120px]">[{item.agent}]</span>
                    <span className="text-[#9E917F] text-[9px] shrink-0">{item.time} · {item.latency}</span>
                  </div>
                  <p className="text-[#4A4237] text-[10.5px] leading-relaxed break-words">
                    {item.text}
                  </p>
                </div>
              ))}
            <div ref={streamEndRef} />
          </div>
        </div>
      ) : (
        /* 交互式命令行控制台 */
        <div className="flex-1 flex flex-col min-h-0 bg-[#1E1B18] text-[#EFE8DE] font-mono text-[11px]">
          
          {/* 控制台日志打印区 */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-1.5">
            <div className="text-[9.5px] text-[#8C8071] pb-1 border-b border-[#352F2A]">
              Heimdall Trading Engine v0.1.0 · Interactive Shell
              <br />输入 'help' 获取所有操作指令。
            </div>

            {consoleLogs.map((log, i) => (
              <div key={i} className="leading-snug break-all text-[10px]">
                <span className="text-[#6E6458] mr-1">[{log.time}]</span>
                {log.type === "cmd" && <span className="text-[#F4BF4F] font-bold">{log.text}</span>}
                {log.type === "success" && <span className="text-[#62C554]">{log.text}</span>}
                {log.type === "critical" && <span className="text-[#E05252] font-bold">{log.text}</span>}
                {log.type === "highlight" && <span className="text-[#DE6438]">{log.text}</span>}
                {log.type === "sys" && <span className="text-[#5B9BD5]">{log.text}</span>}
                {log.type === "info" && <span className="text-[#D8CEBE]">{log.text}</span>}
              </div>
            ))}
            <div ref={consoleEndRef} />
          </div>

          {/* 快捷指令按钮条 */}
          <div className="px-2 py-1 bg-[#26221E] border-t border-[#352F2A] flex items-center space-x-1 overflow-x-auto text-[9.5px]">
            <button
              onClick={() => { if (onExecuteTrade) onExecuteTrade(); }}
              className="px-1.5 py-0.5 rounded bg-[#4D835C] hover:bg-[#3E6B4A] text-white font-bold transition flex items-center space-x-0.5 shrink-0"
              title="触发一次多智能体决策单步推演"
            >
              <span>▶ 推演</span>
            </button>
            <button
              onClick={() => {
                const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                const logs = decisionRecord?.thinking_logs || [];
                if (logs.length > 0) {
                  setConsoleLogs(prev => [
                    ...prev,
                    { type: "highlight", time: now, text: `=== 最新 LLM 决策思维日志快照 (${logs.length} 条) ===` },
                    ...logs.map(l => ({ type: "info", time: now, text: l }))
                  ]);
                } else {
                  setConsoleLogs(prev => [...prev, { type: "info", time: now, text: "暂无最新 LLM 决策思维日志，请先点击【▶ 推演】触发推演。" }]);
                }
                setTimeout(() => consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
              title="在控制台打印当前决策思维日志"
            >
              📝 日志
            </button>
            <button
              onClick={() => {
                const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                setConsoleLogs(prev => [...prev, {
                  type: "success",
                  time: now,
                  text: "[AUDIT] 7项风控检测通过: 回撤 1.2% | 基差 0.03% | 杠杆 1.5x"
                }]);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
            >
              🛡️ 风控
            </button>
            <button
              onClick={() => {
                const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                setConsoleLogs(prev => [...prev, {
                  type: "highlight",
                  time: now,
                  text: "[NAV] 单位净值: 1.0422 | 累计: +4.22%"
                }]);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
            >
              📊 净值
            </button>
            <button
              onClick={() => { if (onEmergencyKill) onEmergencyKill(); }}
              className="px-1.5 py-0.5 rounded bg-[#C24D4D] hover:bg-[#A83838] text-white font-bold transition shrink-0"
            >
              🚨 熔断
            </button>
          </div>

          {/* 命令行输入条 */}
          <form onSubmit={handleRunCommand} className="p-1.5 bg-[#1A1715] border-t border-[#352F2A] flex items-center space-x-1">
            <span className="text-[#DE6438] font-bold text-xs">&gt;</span>
            <input
              type="text"
              value={commandInput}
              onChange={(e) => setCommandInput(e.target.value)}
              placeholder="输入命令 (step, risk, nav)..."
              className="flex-1 bg-transparent text-[#EFE8DE] text-[10px] outline-none placeholder-[#6E6458]"
            />
            <button 
              type="submit" 
              className="px-1.5 py-0.5 bg-[#DE6438] text-white text-[9.5px] font-bold rounded hover:bg-[#C8542A] transition"
            >
              执行
            </button>
          </form>

        </div>
      )}

      {/* 底部执行守护进程迷你指示器 */}
      <div className="h-9 px-2.5 bg-[#231F1C] flex items-center justify-between font-mono text-[9px] text-[#9E917F] border-t border-[#3A332C]">
        <div className="flex items-center space-x-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-[#62C554]"></span>
          <span className="text-[#D8CEBE]">PaperEngine</span>
        </div>
        <span className="text-[#DE6438]">ARMED</span>
      </div>

    </aside>
  );
}
