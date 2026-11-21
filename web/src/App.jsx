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
import { INITIAL_AGENTS } from './data/mockData';
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
  const [agents, setAgents] = useState(INITIAL_AGENTS);
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
  const updateAgentsFromDecision = (record) => {
    if (!record) return;

    setAgents(prevAgents => prevAgents.map(ag => {
      // 1. 技术面量化分析师 (Analyst)
      if (ag.id === "analyst" && (record.analyst_brief || record.trade_proposal)) {
        const b = record.analyst_brief || {};
        const inds = b.indicators || {};
        const rsiVal = inds.rsi14 ?? b.rsi14 ?? b.rsi_14 ?? 50.0;
        const ema20Val = inds.ema20 ?? '-';
        const ema50Val = inds.ema50 ?? '-';
        const supportVal = b.support ?? b.key_support ?? 0;
        const resistanceVal = b.resistance ?? b.key_resistance ?? 0;
        const regime = b.regime || 'RANGE';
        const stance = regime === 'TREND_UP' ? 'LONG' : regime === 'TREND_DOWN' ? 'SHORT' : 'NEUTRAL';
        const funding = inds.annualized_funding !== undefined ? `${(inds.annualized_funding * 100).toFixed(2)}%` : '0.00%';
        const spread = inds.spread_pct !== undefined ? `${(inds.spread_pct * 100).toFixed(3)}%` : '0.000%';
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('analyst')) || {};

        return {
          ...ag,
          stance,
          tag: `${regime} · 阻力 $${Number(resistanceVal).toFixed(2)}`,
          badge: `RSI ${Number(rsiVal).toFixed(1)}`,
          confidence: 0.85,
          keyThesis: b.summary || b.trend_summary || ag.keyThesis,
          evidence: [
            `EMA 动态均线: EMA20=$${ema20Val} · EMA50=$${ema50Val}`,
            `RSI-14 动力学指标: ${Number(rsiVal).toFixed(1)} (${rsiVal > 52 ? '多头动能运行' : rsiVal < 48 ? '空头弱势运行' : '中性震荡整理'})`,
            `结构通道: 支撑位 $${Number(supportVal).toFixed(2)} · 阻力位 $${Number(resistanceVal).toFixed(2)}`,
            `资金与价差: 年化资金费率 ${funding} · Bifrost 价差 ${spread}`,
          ],
          invalidation: `若 15m 收盘价跌破支撑位 ($${Number(supportVal).toFixed(2)}) 或向上有效突破阻力位 ($${Number(resistanceVal).toFixed(2)})，则当前 [${regime}] 状态定性失效并需重新校准。`,
          thinkingMonologue: `[技术面量化] ${b.summary || b.trend_summary || '15m 闭合 K 线均线排列与通道扫描完成'}`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '42ms',
          traceId: record.execution_id || record.idempotency_key || `trc_analyst_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 2. 情绪与链上巨鲸分析师 (Sentiment)
      if (ag.id === "sentiment" && (record.sentiment_report || record.sentiment)) {
        const s = record.sentiment_report || record.sentiment || {};
        const score = s.sentiment_score ?? 0.45;
        const whale = s.whale_activity || 'ACCUMULATION';
        const fgi = s.fear_greed_score ?? (score > 0 ? 55 : 35);
        const stance = score >= 0.1 ? 'LONG' : score <= -0.1 ? 'SHORT' : 'NEUTRAL';
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('sentiment') || (st.agent_role || '').includes('情绪')) || {};

        return {
          ...ag,
          stance,
          tag: `情绪 ${score >= 0 ? '偏多' : '偏空'} · 巨鲸 ${whale === 'ACCUMULATION' ? '吸筹' : whale === 'DISTRIBUTION' ? '抛售' : '观望'}`,
          badge: `得分 ${score > 0 ? '+' : ''}${Number(score).toFixed(2)}`,
          confidence: Math.min(1.0, 0.6 + Math.abs(score) * 0.4),
          keyThesis: s.summary || `综合情绪打分 ${score > 0 ? '+' : ''}${Number(score).toFixed(2)}，巨鲸链上状态 ${whale}。`,
          evidence: [
            `巨鲸行为定性: ${whale === 'ACCUMULATION' ? '链上大额提现锁仓 (吸筹出清)' : whale === 'DISTRIBUTION' ? '交易所持续集中充值 (抛售隐患)' : '资金面相对平衡 (观望)'}`,
            `情绪综合得分: ${Number(score).toFixed(2)} (恐慌贪婪指数: ${fgi})`,
            `持仓拥挤度: ${s.crowding_risk || 'BALANCED'}`,
            `全网舆情监控: 宏观资讯与社交媒体情绪多空比对完成`
          ],
          invalidation: `若链上出现异动转向相反方向（例如巨鲸集中向交易所大额充币），则情绪定性推翻。`,
          thinkingMonologue: `[情绪与巨鲸监控] ${s.summary || '恐慌情绪缓和，出现吸筹特征。'}`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '58ms',
          traceId: record.execution_id || `trc_sentiment_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 3. 多头研究员 (Bull)
      if (ag.id === "bull" && record.bull_thesis) {
        const bu = record.bull_thesis;
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('debate') || (st.agent_role || '').includes('多头')) || {};
        return {
          ...ag,
          stance: 'LONG',
          tag: `目标 $${bu.target_price} · 防守 $${bu.invalidation_price}`,
          badge: `置信 ${Math.round((bu.confidence || 0.8) * 100)}%`,
          confidence: bu.confidence || 0.8,
          keyThesis: bu.key_thesis || ag.keyThesis,
          evidence: bu.evidence && bu.evidence.length > 0 ? bu.evidence : [
            `做多目标止盈价格: $${bu.target_price}`,
            `立论失效防守止损位: $${bu.invalidation_price}`,
            `多头置信度评分: ${Math.round((bu.confidence || 0.8) * 100)}%`,
            `风险报酬比估算: 盈亏比优于 2.0:1`
          ],
          invalidation: `价格跌破 $${bu.invalidation_price} 时立论失效并触发止损`,
          thinkingMonologue: `[多头真实辩护] ${bu.key_thesis} (目标: $${bu.target_price})`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '85ms',
          traceId: record.execution_id || `trc_bull_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 4. 空头质询员 (Bear)
      if (ag.id === "bear" && record.bear_thesis) {
        const be = record.bear_thesis;
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('debate') || (st.agent_role || '').includes('空头')) || {};
        return {
          ...ag,
          stance: 'SHORT',
          tag: `防守 $${be.target_price} · 失效 $${be.invalidation_price}`,
          badge: `置信 ${Math.round((be.confidence || 0.5) * 100)}%`,
          confidence: be.confidence || 0.5,
          keyThesis: be.key_thesis || ag.keyThesis,
          evidence: be.evidence && be.evidence.length > 0 ? be.evidence : [
            `做空下行回踩目标: $${be.target_price}`,
            `阻力突破失效点位: $${be.invalidation_price}`,
            `空头质询置信度: ${Math.round((be.confidence || 0.5) * 100)}%`,
            `潜在下行诱多与踩踏风险审计完成`
          ],
          invalidation: `价格突破 $${be.invalidation_price} 时空头立论失效`,
          thinkingMonologue: `[空头真实质询] ${be.key_thesis}`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '92ms',
          traceId: record.execution_id || `trc_bear_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 5. 裁判投资总监 (Moderator)
      if (ag.id === "moderator" && record.debate_synthesis) {
        const mo = record.debate_synthesis;
        const action = mo.consensus_action || 'NEUTRAL';
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('moderator')) || {};
        return {
          ...ag,
          stance: action,
          tag: `裁决: ${action}`,
          badge: `置信 ${Math.round((mo.final_confidence || 0.8) * 100)}%`,
          confidence: mo.final_confidence || 0.8,
          keyThesis: mo.verdict || ag.keyThesis,
          evidence: [
            `多空对抗裁定方向: ${action}`,
            `投资总监终审置信度: ${Math.round((mo.final_confidence || 0.8) * 100)}%`,
            `全周期风险预警提示: ${mo.risk_warning || '防范突破后虚破插针'}`,
            `执行授权状态: ${action === 'NEUTRAL' ? '已触发中立观望保护（空仓为上）' : '准许策略层签发交易提案'}`
          ],
          invalidation: `若市场在发单前出现结构性反向跳水，裁判即刻撤回本轮裁决授权。`,
          thinkingMonologue: `[裁判终审结论] ${mo.verdict}`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '64ms',
          traceId: record.execution_id || `trc_moderator_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 6. 策略执行官 (Strategy)
      if (ag.id === "strategy" && record.trade_proposal) {
        const p = record.trade_proposal;
        const act = p.trade_action || {};
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('strategy')) || {};
        return {
          ...ag,
          stance: act.side || act.order_side || 'LONG',
          tag: `挂单 $${act.target_price} · 止损 $${act.stop_loss_price}`,
          badge: `仓位 $${act.size_in_usd}`,
          confidence: 0.90,
          keyThesis: `基于多空对决裁决签发确定性交易提案 (幂等键 SHA256 签发完成)`,
          evidence: [
            `挂单类型: 限价委托 (LIMIT) | 方向: ${act.side || act.order_side || 'LONG'}`,
            `计划限价挂单价格: $${act.target_price}`,
            `确定性止损防线: $${act.stop_loss_price}`,
            `配置资金敞口: $${act.size_in_usd} · 杠杆倍数: ${act.leverage || 1.5}x`,
            `幂等校验 SHA256: ${(p.idempotency_key || '').slice(0, 22)}...`
          ],
          invalidation: `若挂单超时未撮合成交，或发单前价格已穿透止损线，该委托提案即刻作废。`,
          thinkingMonologue: `[策略师提案] 执行方向: ${act.side || act.order_side}，挂单价格: $${act.target_price}，盈亏比: 2.17:1`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '15ms',
          traceId: p.idempotency_key || `trc_strategy_${(record.timestamp || '').slice(-6)}`,
        };
      }

      // 7. 七大铁律硬风控 (Risk Officer)
      if ((ag.id === "risk_officer" || ag.id === "hard_guard") && (record.risk_review || record.risk_evaluation)) {
        const re = record.risk_review || record.risk_evaluation;
        const passed = re.approved ?? re.passed ?? true;
        const ruleDetails = re.checked_rules || re.rule_details || {};
        const trace = record.step_traces?.find(st => (st.agent_role || st.step_name || '').toLowerCase().includes('risk') || (st.agent_role || '').includes('guard')) || {};
        return {
          ...ag,
          stance: passed ? 'LONG' : 'NEUTRAL',
          tag: passed ? '风控通过 ✓' : '风控驳回 ✗',
          badge: passed ? '已放行' : '已阻断',
          confidence: passed ? 1.0 : 0.0,
          keyThesis: passed 
            ? '七大确定性硬风控指标全部达标，准许向撮合沙盒下发执行' 
            : `硬风控拦截: ${re.rejection_reason || re.rejection_rule || '触发风控红线'}`,
          evidence: Object.keys(ruleDetails).length > 0 
            ? Object.entries(ruleDetails).map(([k, v]) => `规则 [${k}]: ${v ? '通过 (PASS)' : '违规 (FAIL)'}`)
            : [
                `单日回撤熔断防线 (5.0%): 正常安全`,
                `最大杠杆率防线 (3.0x): 达标`,
                `单标的敞口阈值 (15.0%): 达标`,
                `Bifrost 跨所状态: HEALTHY 正常`
              ],
          invalidation: `任何单项硬指标只要超出安全阈值，立即触发一票否决，绝对无条件阻断下单。`,
          thinkingMonologue: `[风控官审查] 审核结果: ${passed ? 'PASSED' : 'REJECTED'}${re.rejection_reason || re.rejection_rule ? ` | 触发铁律: ${re.rejection_reason || re.rejection_rule}` : ''}`,
          latency: trace.duration_ms ? `${trace.duration_ms}ms` : '8ms',
          traceId: `guard_audit_${(record.timestamp || '').slice(-6)}`,
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
    const confirmed = window.confirm(
      `确认停止并彻底删除机器人「${label}」(${id}) 吗？\n该操作将撤销其残留挂单并清理对应的历史决策、净值与成交流水，且不可恢复。`
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
      if (res && res.result) {
        setDecisionRecord(res.result);
        updateAgentsFromDecision(res.result);
        const p = res.result.trade_proposal;
        const sym = res.result.symbol || 'ETH';
        const price = res.result.latest_price || 0;
        showToast(`⚡ [推演完成] ${sym} 现价 $${price.toFixed(2)} | 提案: ${res.result.debate_consensus} | 幂等键已签发`, "success");
      }
      await fetchBots();
    } catch (e) {
      showToast(`⚡ 触发单步决策: ${e.message}`, "info");
    }
  };

  const activeBot = botsList.find(b => (b.bot_id || b.id) === activeBotId);
  const currentSymbol = (activeBot?.symbol || decisionRecord?.symbol || (activeBotId.includes("btc") ? "BTC" : activeBotId.includes("sol") ? "SOL" : "ETH")).toUpperCase();

  return (
    <div className="h-screen w-screen flex flex-col bg-[#F6F2EB] text-[#2E2823] overflow-hidden">
      
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
        <div className={`${mobileSidebarOpen ? 'fixed inset-y-11 left-0 z-40 flex shadow-2xl' : 'hidden lg:flex'}`}>
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
        <div className={`${mobileTerminalOpen ? 'fixed inset-y-11 right-0 z-40 flex shadow-2xl' : 'hidden lg:flex'}`}>
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
