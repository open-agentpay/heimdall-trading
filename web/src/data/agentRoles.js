/**
 * ================================================================================
 * Heimdall 交易平台 - 智能体角色定义 (Agent Roles)
 * ================================================================================
 * M5（2026-11-21 代码审计）：替代原 mockData.js。
 * 本文件只保存各智能体的静态角色说明（名称、头像、配色、席位、职责介绍），
 * 不包含任何行情、价格、置信度或分析结论。机器人产生首轮真实推演之前，
 * 席位一律显示“等待推演”，杜绝界面展示编造的看多结论与过期价格。
 */

export const AGENT_ROLES = [
  {
    id: "analyst",
    name: "AnalystAgent",
    title: "技术面宏观分析师",
    avatar: "📊",
    color: "#3E6B89",
    seatPosition: "top-center",
    roleDescription: "【小白速懂】：负责阅读纯 K 线价格走势，计算均线（EMA）、强弱指标（RSI）、真实波幅（ATR）等量化数学指标，判断当前大盘是单边暴涨、单边暴跌还是横盘拉锯。",
  },
  {
    id: "sentiment",
    name: "SentimentWhaleAnalyst",
    title: "情绪与链上巨鲸分析师",
    avatar: "🐋",
    color: "#4D835C",
    seatPosition: "top-left",
    roleDescription: "【小白速懂】：专门抓取交易所大户充提币动向（筹码锁仓还是砸盘抛售）、全网恐慌贪婪指数、未平仓合约量 (OI) 与稳定币印钞弹药，防止掉入诱多踩踏陷阱。",
  },
  {
    id: "bull",
    name: "BullResearcher",
    title: "多头先锋研究员",
    avatar: "🐂",
    color: "#4D835C",
    seatPosition: "mid-left",
    roleDescription: "【小白速懂】：多空辩论里的“正方辩友”。拼命寻找上涨的所有蛛丝马迹，给出坚定的做多买入理由、预估止盈目标价，并测算出合理的风险报酬比（盈亏比）。",
  },
  {
    id: "bear",
    name: "BearResearcher",
    title: "空头质询研究员",
    avatar: "🐻",
    color: "#C24D4D",
    seatPosition: "mid-right",
    roleDescription: "【小白速懂】：多空辩论里的“反方辩友”与找茬专家。无论形势多好，它总能挑出潜在风险，比如上方抛压、宏观美联储议息会议、主力假突破诱多，逼迫团队做好防守。",
  },
  {
    id: "moderator",
    name: "DebateModerator",
    title: "多空博弈首席裁判",
    avatar: "⚖️",
    color: "#DE6438",
    seatPosition: "bottom-center",
    roleDescription: "【小白速懂】：法官与终审大裁判。它不偏袒任何一方，主持多头研究员与空头研究员的 3 轮激辩，综合双方证据得出最终结论（买、卖还是空仓观望）。",
  },
  {
    id: "strategy",
    name: "StrategyAgent",
    title: "定量执行策略师",
    avatar: "🎯",
    color: "#D48B38",
    seatPosition: "bottom-left",
    roleDescription: "【小白速懂】：精算师。它拿到裁判的批文后，不盲目市价追高追跌，而是计算最佳挂单价、利用凯利公式（Kelly Criterion）精算头寸与止损，无明确信号时严守空仓防守。",
  },
  {
    id: "risk_officer",
    name: "RiskOfficerAgent",
    title: "合规风控官",
    avatar: "🛡️",
    color: "#8C7A6B",
    seatPosition: "top-right",
    roleDescription: "【小白速懂】：大管家。审查策略师拿来的订单提案，核对盈亏比是否划算、止损是否设得太宽导致暴亏、以及是不是违背了账户的投资组合纪律。",
  },
  {
    id: "hard_guard",
    name: "HardRiskGuard",
    title: "七大铁律确定性硬风控",
    avatar: "🔒",
    color: "#4D835C",
    seatPosition: "bottom-right",
    roleDescription: "【小白速懂】：绝对零妥协的物理断路器！用纯 Python 代码写死，哪怕大模型疯了、幻觉了、想做空地球，这道防火墙只要检测到 1 条不合格，一微秒内直接拔插头拦截！",
  },
  {
    id: "alert_notifier",
    name: "AlertNotifier",
    title: "全天候事件与熔断通知官",
    avatar: "📢",
    color: "#DE6438",
    seatPosition: "sub-control",
    roleDescription: "【小白速懂】：全天候情报通讯兵。一旦系统触发熔断锁仓、订单撮合成交、或者跨所行情插针异动，0.5 秒内推送到你的手机微信、飞书或 Telegram 上！",
  },
];

/** 尚无推演结果时的席位占位状态（中性、无数值） */
export function createPendingAgents() {
  return AGENT_ROLES.map((role) => ({
    ...role,
    pending: true,
    stance: 'PENDING',
    tag: '等待推演',
    badge: '--',
    confidence: null,
    keyThesis: '该机器人尚未产生推演结果，等待首轮决策周期完成后展示真实论据。',
    evidence: [],
    invalidation: '',
    thinkingMonologue: '',
    latency: '',
    traceId: '',
  }));
}

/** 数值归一：非有限数字（undefined / null / NaN / 非数字字符串）一律返回 null */
export function numOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

/** 缺失数值统一显示为 '--'，不再用默认值替代 */
export function fmtNum(value, digits = 2) {
  const num = numOrNull(value);
  return num === null ? '--' : num.toFixed(digits);
}

export function fmtUsd(value, digits = 2) {
  const num = numOrNull(value);
  return num === null ? '--' : `$${num.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

/** 小数比例转百分比（0.0123 -> 1.23%） */
export function fmtPct(fraction, digits = 2) {
  const num = numOrNull(fraction);
  return num === null ? '--' : `${(num * 100).toFixed(digits)}%`;
}
