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
import { useTranslation } from 'react-i18next';
import { fmt } from '../i18n';
import { numOrNull, fmtUsd, fmtPct } from '../data/agentRoles';

// 推演时间：使用决策记录自身的时间戳，而不是页面渲染时刻
function formatRecordTime(ts) {
  if (!ts) return '--';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? '--' : fmt.time(d.getTime());
}

export default function TerminalPanel({ 
  decisionRecord = null, 
  activeBotId = "bot_eth_debate_01",
  isWsConnected = false,
  symbol = null,
  onExecuteTrade = null,
  onEmergencyKill = null
}) {
  const { t } = useTranslation('terminal');
  const [activeTab, setActiveTab] = useState("stream"); // "stream" | "console"
  const [filterAgent, setFilterAgent] = useState("ALL");
  const [commandInput, setCommandInput] = useState("");
  // 启动提示只说明控制台本身已就绪，不再声称推流、OMS 或风控“已激活”（这些状态以实际数据为准）
  const [consoleLogs, setConsoleLogs] = useState([
    { type: "sys", time: fmt.time(Date.now()), text: t('console.ready') }
  ]);

  const streamEndRef = useRef(null);
  const consoleEndRef = useRef(null);
  const lastProcessedKeyRef = useRef(null);

  // 1. 思维链只展示后端真实返回的 thinking_logs；没有推演记录时显示空状态（不再内置编造的示例推演）
  const recordTime = formatRecordTime(decisionRecord?.timestamp);
  const activeThinkingLogs = React.useMemo(() => {
    const logs = decisionRecord?.thinking_logs || [];
    if (logs.length === 0) return [];
    const traces = decisionRecord.step_traces || [];

    return logs.map((logStr) => {
      let agentName = "Agent";
      let text = logStr;
      if (logStr.startsWith("[")) {
        const closeBracket = logStr.indexOf("]");
        if (closeBracket !== -1) {
          agentName = logStr.slice(1, closeBracket);
          text = logStr.slice(closeBracket + 1).replace(/^[:：]\s*/, '').trim();
        }
      }
      const lower = agentName.toLowerCase();
      let color = "rgb(var(--c-info))";
      if (lower.includes("risk") || lower.includes("guard")) color = "rgb(var(--c-danger))";
      else if (lower.includes("bull")) color = "rgb(var(--c-success))";
      else if (lower.includes("bear")) color = "rgb(var(--c-warning))";
      else if (lower.includes("debate") || lower.includes("moderator")) color = "rgb(var(--c-brand))";

      // 按名称匹配对应的 StepTrace，取后端真实耗时；匹配不到就不显示耗时（不再按序号编造）
      const key = lower.split(/[\s\/·:]/)[0];
      const trace = key ? traces.find((t) => (t.step_name || '').toLowerCase().includes(key)) : null;
      const ms = numOrNull(trace?.latency_ms ?? trace?.duration_ms);

      return {
        agent: agentName,
        time: recordTime,
        latency: ms === null ? '' : `${Math.round(ms)}ms`,
        color,
        text,
      };
    });
  }, [decisionRecord]);

  // 2. 控制台的 status / risk / nav 回复全部取自最近一轮推演记录；没有记录就如实说明
  const buildStatusReply = (now) => ({
    type: "info",
    time: now,
    text: t('console.status', { botId: activeBotId, time: decisionRecord?.timestamp ? recordTime : t('console.none'), consensus: decisionRecord?.debate_consensus || '--', stream: isWsConnected ? t('console.wsConnected') : t('console.wsPolling') }),
  });

  const buildRiskReply = (now) => {
    if (!decisionRecord?.timestamp) {
      return { type: "info", time: now, text: t('console.riskNoRecord') };
    }
    const rv = decisionRecord.risk_review || null;
    const rules = rv?.checked_rules || {};
    const total = Object.keys(rules).length;
    const passedCount = Object.values(rules).filter(Boolean).length;
    const lev = numOrNull(decisionRecord.trade_proposal?.trade_action?.leverage);
    let verdict = t('console.verdict.none');
    if (rv && rv.approved === true) verdict = t('console.verdict.passed');
    else if (rv && rv.approved === false) verdict = t('console.verdict.blocked', { reason: rv.rejection_reason || t('console.reasonMissing') });
    else if (!decisionRecord.proposal_generated) verdict = t('console.verdict.noProposal');
    return {
      type: rv && rv.approved === false ? "critical" : "success",
      time: now,
      text: t('console.risk', { time: recordTime, daily: fmtPct(decisionRecord.daily_drawdown_pct), total: fmtPct(decisionRecord.drawdown_pct), leverage: lev === null ? '--' : `${lev}x`, rules: total ? t('console.rulesPassed', { passed: passedCount, total }) : t('console.rulesMissing'), verdict }),
    };
  };

  const buildNavReply = (now) => {
    const nav = numOrNull(decisionRecord?.nav);
    if (!decisionRecord?.timestamp || nav === null) {
      return { type: "info", time: now, text: t('console.navNoRecord') };
    }
    const cum = (nav - 1) * 100;
    return {
      type: "highlight",
      time: now,
      text: t('console.nav', { time: recordTime, nav: fmt.number(nav, 4), cum: `${cum >= 0 ? '+' : ''}${fmt.number(cum, 2)}%`, equity: fmtUsd(decisionRecord.equity) }),
    };
  };

  // 监听 decisionRecord 变化，自动写入执行控制台
  useEffect(() => {
    if (!decisionRecord) return;
    
    // 避免重复写入同一轮推演数据
    const currentKey = decisionRecord.decision_id ||
      decisionRecord.timestamp ||
      (decisionRecord.thinking_logs && decisionRecord.thinking_logs.join('|'));
    if (currentKey && currentKey === lastProcessedKeyRef.current) return;

    const now = fmt.time(Date.now());
    const newItems = [];

    // 1. 打印大模型多智能体决策思维链条目
    const thinkingLogs = decisionRecord.thinking_logs || [];
    if (thinkingLogs.length > 0) {
      newItems.push({
        type: "highlight",
        time: now,
        text: t('console.cotHeader', { symbol: decisionRecord.symbol || symbol || '--', nodes: thinkingLogs.length })
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
        text: t('console.decision', { consensus: decisionRecord.debate_consensus, symbol: decisionRecord.symbol || symbol || '--' })
      });
    }

    if (decisionRecord.trade_proposal) {
      const p = decisionRecord.trade_proposal;
      newItems.push({
        type: "cmd",
        time: now,
        text: t('console.proposal', { price: p.trade_action?.target_price || '-', stop: p.trade_action?.stop_loss_price || '-', size: p.trade_action?.size_in_usd || '-' })
      });
    }

    const idemKey = decisionRecord.trade_proposal?.idempotency_key;
    if (idemKey) {
      newItems.push({
        type: "sys",
        time: now,
        text: t('console.idempotency', { id: `${idemKey.slice(0, 18)}...` })
      });
    }

    // 风控结论读取后端真实字段 risk_review.approved（旧代码读的 risk_evaluation.passed 后端从不返回）
    const rv = decisionRecord.risk_review;
    if (rv && typeof rv.approved === 'boolean') {
      newItems.push({
        type: rv.approved ? "success" : "critical",
        time: now,
        text: t('console.riskAudit', { verdict: rv.approved ? t('console.verdict.passed') : t('console.verdict.blocked', { reason: rv.rejection_reason || t('console.reasonMissing') }) })
      });
    }

    // 成交只报告后端真实返回的成交编号，不再用 1.5 ETH @ $3120 之类的默认值填充
    if (decisionRecord.execution_id) {
      newItems.push({
        type: "highlight",
        time: now,
        text: t('console.execution', { id: decisionRecord.execution_id })
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

    const now = fmt.time(Date.now());
    const logCmd = { type: "cmd", time: now, text: `$ ${cmd}` };

    let reply = null;
    const lower = cmd.toLowerCase();

    if (lower === "help") {
      reply = {
        type: "info",
        time: now,
        text: t('console.help')
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
            text: t('console.logsHeader', { n: logs.length })
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
          text: t('console.noLogs')
        };
      }
    } else if (lower === "status") {
      reply = buildStatusReply(now);
    } else if (lower === "step") {
      reply = {
        type: "highlight",
        time: now,
        text: t('console.dispatch', { botId: activeBotId })
      };
      if (onExecuteTrade) onExecuteTrade();
    } else if (lower === "kill") {
      reply = {
        type: "critical",
        time: now,
        text: t('console.emergency')
      };
      if (onEmergencyKill) onEmergencyKill();
    } else if (lower === "risk") {
      reply = buildRiskReply(now);
    } else if (lower === "nav") {
      reply = buildNavReply(now);
    } else if (lower === "clear") {
      setConsoleLogs([]);
      setCommandInput("");
      return;
    } else {
      reply = {
        type: "critical",
        time: now,
        text: t('console.unknown', { cmd })
      };
    }

    setConsoleLogs((prev) => [...prev, logCmd, reply]);
    setCommandInput("");
  };

  return (
    <aside className="w-64 bg-panel border-l border-line flex flex-col shrink-0">
      
      {/* 顶栏标签切换 */}
      <div className="h-10 px-2.5 border-b border-line flex items-center justify-between">
        <div className="flex space-x-1">
          <button
            onClick={() => setActiveTab("stream")}
            className={`px-2 py-1 rounded-md text-xs font-bold transition flex items-center space-x-1 ${
              activeTab === "stream"
                ? "bg-surface text-brand shadow-xs"
                : "text-ink-3 hover:text-ink"
            }`}
          >
            <span>{t('tabs.stream')}</span>
            <span className="text-xs bg-muted px-1 rounded-full text-ink-3">
              {activeThinkingLogs.length}
            </span>
          </button>
          <button
            onClick={() => setActiveTab("console")}
            className={`px-2 py-1 rounded-md text-xs font-bold transition flex items-center space-x-1 ${
              activeTab === "console"
                ? "bg-surface text-brand shadow-xs"
                : "text-ink-3 hover:text-ink"
            }`}
          >
            <span>{t('tabs.console')}</span>
            <span className={`w-1.5 h-1.5 rounded-full ${isWsConnected ? 'bg-success-solid animate-pulse' : 'bg-warning-solid'}`}></span>
          </button>
        </div>

        <span className={`text-xs font-mono ${isWsConnected ? 'text-success' : 'text-warning'}`}>
          {isWsConnected ? 'WS: LIVE' : 'WS: POLL'}
        </span>
      </div>

      {/* 主体切换区 */}
      {activeTab === "stream" ? (
        <div className="flex-1 flex flex-col min-h-0">
          
          {/* 角色过滤标签 */}
          <div className="px-2.5 py-1.5 border-b border-line-soft flex items-center space-x-1 overflow-x-auto text-xs font-medium text-ink-3 bg-canvas">
            <span>{t('filter.label')}</span>
            {[
              { key: "ALL", label: t('filter.all') },
              { key: "Analyst", label: t('filter.analyst') },
              { key: "Sentiment", label: t('filter.sentiment') },
              { key: "Debate", label: t('filter.debate') },
              { key: "Risk", label: t('filter.risk') }
            ].map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setFilterAgent(key)}
                className={`px-1.5 py-0.5 rounded transition text-xs whitespace-nowrap ${
                  filterAgent === key
                    ? "bg-brand text-white font-bold"
                    : "bg-surface text-ink-3 hover:bg-muted"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* 思维链条目滚动列表 */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-2 text-xs font-mono">
            {activeThinkingLogs.length === 0 && (
              <div className="p-3 rounded-xl border border-dashed border-line-strong bg-surface text-xs text-ink-4 leading-relaxed font-sans">
                {t('stream.empty')}
              </div>
            )}
            {activeThinkingLogs
              .filter((item) => filterAgent === "ALL" || item.agent.toLowerCase().includes(filterAgent.toLowerCase()))
              .map((item, index) => (
                <div 
                  key={index} 
                  className="bg-surface p-2 rounded-xl border border-line shadow-xs hover:border-warning transition"
                >
                  <div className="flex items-center justify-between text-xs font-bold mb-1">
                    <span style={{ color: item.color }} className="truncate max-w-[120px]">[{item.agent}]</span>
                    <span className="text-ink-4 text-xs shrink-0">{item.latency ? `${item.time} · ${item.latency}` : item.time}</span>
                  </div>
                  <p className="text-ink-2 text-xs leading-relaxed break-words">
                    {item.text}
                  </p>
                </div>
              ))}
            <div ref={streamEndRef} />
          </div>
        </div>
      ) : (
        /* 交互式命令行控制台 */
        <div className="flex-1 flex flex-col min-h-0 bg-[#1E1B18] text-[#EFE8DE] font-mono text-xs">
          
          {/* 控制台日志打印区 */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-1.5">
            <div className="text-xs text-[#8C8071] pb-1 border-b border-[#352F2A]">
              Heimdall Trading Engine v0.1.0 · Interactive Shell
              <br />{t('console.shellHint')}
            </div>

            {consoleLogs.map((log, i) => (
              <div key={i} className="leading-snug break-all text-xs">
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
          <div className="px-2 py-1 bg-[#26221E] border-t border-[#352F2A] flex items-center space-x-1 overflow-x-auto text-xs">
            <button
              onClick={() => { if (onExecuteTrade) onExecuteTrade(); }}
              className="px-1.5 py-0.5 rounded bg-[#4D835C] hover:bg-[#3E6B4A] text-white font-bold transition flex items-center space-x-0.5 shrink-0"
              title={t('quick.stepTitle')}
            >
              <span>{t('quick.step')}</span>
            </button>
            <button
              onClick={() => {
                const now = fmt.time(Date.now());
                const logs = decisionRecord?.thinking_logs || [];
                if (logs.length > 0) {
                  setConsoleLogs(prev => [
                    ...prev,
                    { type: "highlight", time: now, text: t('console.logsSnapshot', { n: logs.length }) },
                    ...logs.map(l => ({ type: "info", time: now, text: l }))
                  ]);
                } else {
                  setConsoleLogs(prev => [...prev, { type: "info", time: now, text: t('console.noLogsQuick') }]);
                }
                setTimeout(() => consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
              title={t('quick.logsTitle')}
            >
              {t('quick.logs')}
            </button>
            <button
              onClick={() => {
                const now = fmt.time(Date.now());
                setConsoleLogs(prev => [...prev, buildRiskReply(now)]);
                setTimeout(() => consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
            >
              {t('quick.risk')}
            </button>
            <button
              onClick={() => {
                const now = fmt.time(Date.now());
                setConsoleLogs(prev => [...prev, buildNavReply(now)]);
                setTimeout(() => consoleEndRef.current?.scrollIntoView({ behavior: 'smooth' }), 50);
              }}
              className="px-1.5 py-0.5 rounded bg-[#3A332C] hover:bg-[#4A4237] text-[#D8CEBE] transition shrink-0"
            >
              {t('quick.nav')}
            </button>
            <button
              onClick={() => { if (onEmergencyKill) onEmergencyKill(); }}
              className="px-1.5 py-0.5 rounded bg-[#C24D4D] hover:bg-[#A83838] text-white font-bold transition shrink-0"
            >
              {t('quick.kill')}
            </button>
          </div>

          {/* 命令行输入条 */}
          <form onSubmit={handleRunCommand} className="p-1.5 bg-[#1A1715] border-t border-[#352F2A] flex items-center space-x-1">
            <span className="text-[#DE6438] font-bold text-xs">&gt;</span>
            <input
              type="text"
              value={commandInput}
              onChange={(e) => setCommandInput(e.target.value)}
              placeholder={t('input.placeholder')}
              className="flex-1 bg-transparent text-[#EFE8DE] text-xs outline-none placeholder-[#6E6458]"
            />
            <button 
              type="submit" 
              className="px-1.5 py-0.5 bg-[#DE6438] text-white text-xs font-bold rounded hover:bg-[#C8542A] transition"
            >
              {t('input.run')}
            </button>
          </form>

        </div>
      )}

      {/* 底部执行守护进程迷你指示器 */}
      <div className="h-9 px-2.5 bg-[#231F1C] flex items-center justify-between font-mono text-xs text-[#9E917F] border-t border-[#3A332C]">
        <div className="flex items-center space-x-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${isWsConnected ? 'bg-[#62C554]' : 'bg-[#C28B38]'}`}></span>
          <span className="text-[#D8CEBE]">{isWsConnected ? t('footer.wsConnected') : t('footer.wsPolling')}</span>
        </div>
        <span className="text-[#9E917F]">{t('footer.lastDecision', { time: decisionRecord?.timestamp ? recordTime : '--' })}</span>
      </div>

    </aside>
  );
}
