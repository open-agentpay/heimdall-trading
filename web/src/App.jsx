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
import { createPendingAgents, numOrNull, fmtNum, fmtUsd, fmtPct } from './data/agentRoles';
import { 
  triggerEmergencyKill, 
  stepBot, 
  listBots, 
  deleteBot,
  getBotLatestState,
  getBotTrades,
  clearAuthSession
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
  const [notification, setNotification] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Web3 钱包状态与密钥金库弹窗状态
  const [isWalletModalOpen, setIsWalletModalOpen] = useState(false);
  const [isKeyVaultModalOpen, setIsKeyVaultModalOpen] = useState(false);
  const [isCreateBotModalOpen, setIsCreateBotModalOpen] = useState(false);
  const [prefillBotConfig, setPrefillBotConfig] = useState(null);
  const [theme, setTheme] = useState(() => localStorage.getItem('heimdall_theme') || 'light');
  const [soundMuted, setSoundMutedState] = useState(() => isSoundMuted());
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [mobileTerminalOpen, setMobileTerminalOpen] = useState(false);

  // 同步暗黑/暖沙主题至 html[data-theme]
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('heimdall_theme', theme);
  }, [theme]);

  const handleToggleTheme = () => {
    setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'));
  };

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
  const [walletState, setWalletState] = useState(() => {
    const isConn = localStorage.getItem("heimdall_wallet_connected") === "true";
    if (isConn) {
      try {
        const saved = localStorage.getItem("heimdall_wallet_state");
        if (saved) return JSON.parse(saved);
      } catch (e) {}
    }
    return {
      isConnected: false,
      address: '',
      shortAddress: '',
      network: 'Arbitrum One',
      balance: '',
      provider: '',
    };
  });

  // 1. 显示操作浮窗提示
  const showToast = (msg, type = "info") => {
    setNotification({ msg, type });
    setTimeout(() => setNotification(null), 3500);
  };

  // 2. 根据后端真实决策动态更新 Agent 席位元数据
  //    M5：只展示后端真实返回的字段，缺失数值一律显示 "--"；不再用写死的默认值替代
  //    （如默认情绪得分、默认巨鲸吸筹、写死的置信度与盈亏比、风控默认放行、伪造的耗时等）
  const updateAgentsFromDecision = (record) => {
    if (!record || record.error) return;

    const ts = (record.timestamp || '').slice(-6);
    const findTrace = (...keys) => (record.step_traces || []).find((st) => {
      const name = `${st.agent_role || ''} ${st.step_name || ''} ${st.agent_id || ''}`.toLowerCase();
      return keys.some((k) => name.includes(k));
    }) || null;
    const traceLatency = (trace) => {
      const ms = numOrNull(trace?.latency_ms ?? trace?.duration_ms);
      return ms === null ? '--' : `${Math.round(ms)}ms`;
    };

    setAgents(prevAgents => prevAgents.map(ag => {
      // 1. 技术面量化分析师 (Analyst)
      if (ag.id === "analyst" && (record.analyst_brief || record.trade_proposal)) {
        const b = record.analyst_brief || {};
        const inds = b.indicators || {};
        const rsi = numOrNull(inds.rsi14 ?? b.rsi14 ?? b.rsi_14);
        const support = numOrNull(b.support ?? b.key_support);
        const resistance = numOrNull(b.resistance ?? b.key_resistance);
        const regime = b.regime || 'UNKNOWN';
        const stance = regime === 'TREND_UP' ? 'LONG' : regime === 'TREND_DOWN' ? 'SHORT' : 'NEUTRAL';
        const summary = b.summary || b.trend_summary || '本轮未返回技术面摘要';
        const rsiLabel = rsi === null ? '' : rsi > 52 ? ' (多头动能运行)' : rsi < 48 ? ' (空头弱势运行)' : ' (中性震荡整理)';

        return {
          ...ag,
          pending: false,
          stance,
          tag: `${regime} · 阻力 ${fmtUsd(resistance)}`,
          badge: `RSI ${fmtNum(rsi, 1)}`,
          confidence: numOrNull(b.confidence),
          keyThesis: summary,
          evidence: [
            `EMA 动态均线: EMA20=${fmtUsd(inds.ema20)} · EMA50=${fmtUsd(inds.ema50)}`,
            `RSI-14 动力学指标: ${fmtNum(rsi, 1)}${rsiLabel}`,
            `结构通道: 支撑位 ${fmtUsd(support)} · 阻力位 ${fmtUsd(resistance)}`,
            `资金与价差: 年化资金费率 ${fmtPct(inds.annualized_funding, 2)} · Bifrost 价差 ${fmtPct(inds.spread_pct, 3)}`,
          ],
          invalidation: (support !== null || resistance !== null)
            ? `若 15m 收盘价跌破支撑位 (${fmtUsd(support)}) 或向上有效突破阻力位 (${fmtUsd(resistance)})，则当前 [${regime}] 状态定性失效并需重新校准。`
            : '本轮未返回支撑 / 阻力位',
          thinkingMonologue: `[技术面量化] ${summary}`,
          latency: traceLatency(findTrace('analyst')),
          traceId: record.execution_id || record.idempotency_key || `trc_analyst_${ts}`,
        };
      }

      // 2. 情绪与链上巨鲸分析师 (Sentiment)
      if (ag.id === "sentiment" && (record.sentiment_report || record.sentiment)) {
        const s = record.sentiment_report || record.sentiment || {};
        const score = numOrNull(s.sentiment_score);
        const whale = s.whale_activity || null;
        const fgi = numOrNull(s.fear_greed_score);
        const stance = score === null ? 'NEUTRAL' : score >= 0.1 ? 'LONG' : score <= -0.1 ? 'SHORT' : 'NEUTRAL';
        const scoreTxt = score === null ? '--' : `${score > 0 ? '+' : ''}${score.toFixed(2)}`;
        const whaleShort = whale === 'ACCUMULATION' ? '吸筹' : whale === 'DISTRIBUTION' ? '抛售' : whale ? '观望' : '--';
        const whaleLong = whale === 'ACCUMULATION'
          ? '链上大额提现锁仓 (吸筹)'
          : whale === 'DISTRIBUTION'
          ? '交易所持续集中充值 (抛售隐患)'
          : whale ? '资金面相对平衡 (观望)' : '未返回';

        return {
          ...ag,
          pending: false,
          stance,
          tag: `情绪 ${score === null ? '--' : score >= 0 ? '偏多' : '偏空'} · 巨鲸 ${whaleShort}`,
          badge: `得分 ${scoreTxt}`,
          confidence: score === null ? null : Math.min(1.0, 0.6 + Math.abs(score) * 0.4),
          keyThesis: s.summary || (score === null ? '本轮未返回情绪得分' : `综合情绪打分 ${scoreTxt}，巨鲸链上状态 ${whale || '未知'}。`),
          evidence: [
            `巨鲸行为定性: ${whaleLong}`,
            `情绪综合得分: ${scoreTxt} (恐慌贪婪指数: ${fgi === null ? '--' : fgi})`,
            `持仓拥挤度: ${s.crowding_risk || '--'}`,
          ],
          invalidation: '若链上出现异动转向相反方向（例如巨鲸集中向交易所大额充币），则情绪定性推翻。',
          thinkingMonologue: `[情绪与巨鲸监控] ${s.summary || '本轮未返回情绪摘要'}`,
          latency: traceLatency(findTrace('sentiment', '情绪')),
          traceId: record.execution_id || `trc_sentiment_${ts}`,
        };
      }

      // 3 & 4. 多头研究员 (Bull) / 空头质询员 (Bear)
      if ((ag.id === "bull" && record.bull_thesis) || (ag.id === "bear" && record.bear_thesis)) {
        const isBull = ag.id === "bull";
        const th = isBull ? record.bull_thesis : record.bear_thesis;
        const conf = numOrNull(th.confidence);
        const target = numOrNull(th.target_price);
        const inval = numOrNull(th.invalidation_price);
        const thesis = th.key_thesis || (isBull ? '本轮未返回多头论点' : '本轮未返回空头论点');

        return {
          ...ag,
          pending: false,
          stance: isBull ? 'LONG' : 'SHORT',
          tag: isBull ? `目标 ${fmtUsd(target)} · 防守 ${fmtUsd(inval)}` : `防守 ${fmtUsd(target)} · 失效 ${fmtUsd(inval)}`,
          badge: `置信 ${fmtPct(conf, 0)}`,
          confidence: conf,
          keyThesis: thesis,
          evidence: Array.isArray(th.evidence) && th.evidence.length > 0 ? th.evidence : [
            isBull ? `做多目标止盈价格: ${fmtUsd(target)}` : `做空下行回踩目标: ${fmtUsd(target)}`,
            isBull ? `立论失效防守止损位: ${fmtUsd(inval)}` : `阻力突破失效点位: ${fmtUsd(inval)}`,
            `${isBull ? '多头' : '空头'}置信度评分: ${fmtPct(conf, 0)}`,
          ],
          invalidation: inval === null
            ? '本轮未返回失效价位'
            : isBull ? `价格跌破 ${fmtUsd(inval)} 时立论失效并触发止损` : `价格突破 ${fmtUsd(inval)} 时空头立论失效`,
          thinkingMonologue: isBull
            ? `[多头真实辩护] ${thesis}${target !== null ? ` (目标: ${fmtUsd(target)})` : ''}`
            : `[空头真实质询] ${thesis}`,
          latency: traceLatency(isBull ? findTrace('bull', '多头', 'debate') : findTrace('bear', '空头', 'debate')),
          traceId: record.execution_id || `trc_${ag.id}_${ts}`,
        };
      }

      // 5. 裁判投资总监 (Moderator)
      if (ag.id === "moderator" && record.debate_synthesis) {
        const mo = record.debate_synthesis;
        const action = mo.consensus_action || 'UNKNOWN';
        const conf = numOrNull(mo.final_confidence);
        const verdict = mo.verdict || '本轮未返回裁决说明';
        const authorized = action === 'LONG' || action === 'SHORT';

        return {
          ...ag,
          pending: false,
          stance: action,
          tag: `裁决: ${action}`,
          badge: `置信 ${fmtPct(conf, 0)}`,
          confidence: conf,
          keyThesis: verdict,
          evidence: [
            `多空对抗裁定方向: ${action}`,
            `投资总监终审置信度: ${fmtPct(conf, 0)}`,
            ...(mo.risk_warning ? [`风险预警提示: ${mo.risk_warning}`] : []),
            `执行授权状态: ${authorized ? '准许策略层签发交易提案' : '中立观望，不签发交易提案'}`,
          ],
          invalidation: '若市场在发单前出现结构性反向跳水，裁判即刻撤回本轮裁决授权。',
          thinkingMonologue: `[裁判终审结论] ${verdict}`,
          latency: traceLatency(findTrace('moderator', '裁判')),
          traceId: record.execution_id || `trc_moderator_${ts}`,
        };
      }

      // 6. 策略执行官 (Strategy)
      if (ag.id === "strategy") {
        const latency = traceLatency(findTrace('strategy'));
        if (record.trade_proposal) {
          const p = record.trade_proposal;
          const act = p.trade_action || {};
          const stance = act.position_side
            || (act.order_side === 'BUY' ? 'LONG' : act.order_side === 'SELL' ? 'SHORT' : 'UNKNOWN');
          const entry = numOrNull(act.target_price);
          const sl = numOrNull(act.stop_loss_price);
          const tp = numOrNull(act.take_profit_price);
          const lev = numOrNull(act.leverage);
          const rr = (entry !== null && sl !== null && tp !== null && Math.abs(entry - sl) > 0)
            ? Math.abs(tp - entry) / Math.abs(entry - sl)
            : null;

          return {
            ...ag,
            pending: false,
            stance,
            tag: `${act.order_type || '委托'} ${fmtUsd(entry)} · 止损 ${fmtUsd(sl)}`,
            badge: `仓位 ${fmtUsd(act.size_in_usd)}`,
            confidence: null,
            keyThesis: '基于多空对决裁决签发确定性交易提案（含幂等键）',
            evidence: [
              `委托类型: ${act.order_type || '--'} | 方向: ${act.order_side || '--'} (${stance})`,
              `委托价格: ${fmtUsd(entry)}`,
              `确定性止损防线: ${fmtUsd(sl)} · 止盈目标: ${fmtUsd(tp)}`,
              `配置资金敞口: ${fmtUsd(act.size_in_usd)} · 杠杆倍数: ${lev === null ? '--' : `${lev}x`}`,
              `幂等键: ${p.idempotency_key ? `${p.idempotency_key.slice(0, 22)}...` : '--'}`,
            ],
            invalidation: '若挂单超时未撮合成交，或发单前价格已穿透止损线，该委托提案即刻作废。',
            thinkingMonologue: `[策略师提案] 方向: ${act.order_side || '--'}，委托价格: ${fmtUsd(entry)}，盈亏比: ${rr === null ? '--' : `${rr.toFixed(2)}:1`}`,
            latency,
            traceId: p.idempotency_key || `trc_strategy_${ts}`,
          };
        }
        const mo = record.debate_synthesis || {};
        const action = mo.consensus_action || record.debate_consensus || 'NEUTRAL';
        return {
          ...ag,
          pending: false,
          stance: action === 'NEUTRAL' ? 'NEUTRAL' : 'WAIT',
          tag: action === 'NEUTRAL' ? '空仓防守 · 暂无挂单' : '等待信号 · 暂无提案',
          badge: action === 'NEUTRAL' ? '防守观望' : '待命',
          confidence: null,
          keyThesis: `裁判终审定性为 ${action}，本轮策略层未签发任何买卖委托单。`,
          evidence: [
            `终审裁决: ${action}`,
            '本轮未签发交易提案',
          ],
          invalidation: '下一轮若多空博弈形成高置信度突破并获裁判授权，策略师将立即计算入场价与止损。',
          thinkingMonologue: `[策略精算师] 裁判裁决为 ${action}，本轮不签发委托。`,
          latency,
          traceId: `trc_strategy_neutral_${ts}`,
        };
      }

      // 7. 软风控官 / 确定性硬风控 (Risk Officer / HardRiskGuard)
      if ((ag.id === "risk_officer" || ag.id === "hard_guard") && (record.risk_review || record.risk_evaluation)) {
        const re = record.risk_review || record.risk_evaluation;
        const approvedRaw = re.approved ?? re.passed;
        const decided = typeof approvedRaw === 'boolean';
        const passed = approvedRaw === true;
        const ruleDetails = re.checked_rules || re.rule_details || {};
        const reason = re.rejection_reason || re.rejection_rule;

        return {
          ...ag,
          pending: false,
          stance: !decided ? 'UNKNOWN' : passed ? 'LONG' : 'NEUTRAL',
          tag: !decided ? '风控结果未知' : passed ? '风控通过 ✓' : '风控驳回 ✗',
          badge: !decided ? '--' : passed ? '已放行' : '已阻断',
          confidence: !decided ? null : passed ? 1.0 : 0.0,
          keyThesis: !decided
            ? '本轮未返回风控结论'
            : passed
            ? '本轮硬风控规则全部通过，准许向执行层下发'
            : `硬风控拦截: ${reason || '触发风控红线'}`,
          evidence: Object.keys(ruleDetails).length > 0
            ? Object.entries(ruleDetails).map(([k, v]) => `规则 [${k}]: ${v ? '通过 (PASS)' : '违规 (FAIL)'}`)
            : ['本轮未返回逐条规则明细'],
          invalidation: '任何单项硬指标只要超出安全阈值，立即触发一票否决，绝对无条件阻断下单。',
          thinkingMonologue: `[风控官审查] 审核结果: ${!decided ? 'UNKNOWN' : passed ? 'PASSED' : 'REJECTED'}${reason ? ` | 触发规则: ${reason}` : ''}`,
          latency: traceLatency(findTrace('risk', 'guard')),
          traceId: `guard_audit_${ts}`,
        };
      }
      return ag;
    }));
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

  // 全局监听 401 会话过期事件，自动断开本地假态并唤起钱包签名弹窗
  useEffect(() => {
    const onAuthExpired = (evt) => {
      setWalletState({
        isConnected: false,
        address: '',
        shortAddress: '',
        network: 'Arbitrum One',
        balance: '',
        provider: '',
      });
      showToast(`🔐 ${evt?.detail?.message || '会话已过期，请重新连接钱包签名登录'}`, 'critical');
      setIsWalletModalOpen(true);
    };
    window.addEventListener('heimdall:auth-expired', onAuthExpired);
    return () => window.removeEventListener('heimdall:auth-expired', onAuthExpired);
  }, []);

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

  // 钱包连接成功回调 (持久化到本地存储)
  const handleConnectWallet = (newWalletData) => {
    const fullState = {
      isConnected: true,
      ...newWalletData,
    };
    setWalletState(fullState);
    localStorage.setItem("heimdall_wallet_connected", "true");
    localStorage.setItem("heimdall_wallet_state", JSON.stringify(fullState));
    if (newWalletData.address) {
      localStorage.setItem("heimdall_wallet_address", newWalletData.address);
    }
    showToast(`🎉 钱包连接成功！已授权: ${newWalletData.shortAddress || newWalletData.address}`, "success");
  };

  // 钱包断开连接回调 (彻底清空持久化状态与会话)
  const handleDisconnectWallet = () => {
    const disconnectedState = {
      isConnected: false,
      address: '',
      shortAddress: '',
      network: 'Arbitrum One',
      balance: '',
      provider: '',
    };
    setWalletState(disconnectedState);
    localStorage.removeItem("heimdall_wallet_connected");
    localStorage.removeItem("heimdall_wallet_state");
    clearAuthSession();
    showToast("👋 钱包已安全断开连接", "info");
  };

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
      {notification && (
        <div 
          className={`fixed bottom-6 right-6 z-50 px-4 py-2.5 rounded-xl shadow-lg border text-xs font-semibold flex items-center space-x-2 animate-bounce ${
            notification.type === "critical"
              ? "bg-danger-solid text-white border-danger"
              : notification.type === "success"
              ? "bg-success-solid text-white border-success"
              : "bg-panel text-ink border-line-strong"
          }`}
        >
          <span>{notification.msg}</span>
        </div>
      )}

    </div>
  );
}
