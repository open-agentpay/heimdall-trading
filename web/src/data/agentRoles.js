import i18n, { fmt } from '../i18n';
/**
 * ================================================================================
 * Heimdall 交易平台 - 智能体角色定义 (Agent Roles)
 * ================================================================================
 * M5（2026-11-21 代码审计）：替代原 mockData.js。
 * 本文件只保存各智能体的静态角色说明（名称、头像、配色、席位、职责介绍），
 * 不包含任何行情、价格、置信度或分析结论。机器人产生首轮真实推演之前，
 * 席位一律显示“等待推演”，杜绝界面展示编造的看多结论与过期价格。
 */

/**
 * 角色的 title / roleDescription 是 getter：每次读取时按当前语言翻译（语言包 agents:roles.<id>.*），
 * 所以展开（{ ...role }）得到的是调用时刻语言的文字。
 */
const defineRole = (id, name, avatar, color, seatPosition) => ({
  id,
  name,
  avatar,
  color,
  seatPosition,
  get title() { return i18n.t(`agents:roles.${id}.title`); },
  get roleDescription() { return i18n.t(`agents:roles.${id}.description`); },
});

export const AGENT_ROLES = [
  defineRole("analyst", "AnalystAgent", "📊", "#3E6B89", "top-center"),
  defineRole("sentiment", "SentimentWhaleAnalyst", "🐋", "#4D835C", "top-left"),
  defineRole("bull", "BullResearcher", "🐂", "#4D835C", "mid-left"),
  defineRole("bear", "BearResearcher", "🐻", "#C24D4D", "mid-right"),
  defineRole("moderator", "DebateModerator", "⚖️", "#DE6438", "bottom-center"),
  defineRole("strategy", "StrategyAgent", "🎯", "#D48B38", "bottom-left"),
  defineRole("risk_officer", "RiskOfficerAgent", "🛡️", "#8C7A6B", "top-right"),
  defineRole("hard_guard", "HardRiskGuard", "🔒", "#4D835C", "bottom-right"),
  defineRole("alert_notifier", "AlertNotifier", "📢", "#DE6438", "sub-control"),
];

/** 尚无推演结果时的席位占位状态（中性、无数值）；每次调用按当前语言生成文字 */
export function createPendingAgents() {
  return AGENT_ROLES.map((role) => ({
    ...role,
    pending: true,
    stance: 'PENDING',
    tag: i18n.t('agents:pending.tag'),
    badge: '--',
    confidence: null,
    keyThesis: i18n.t('agents:pending.keyThesis'),
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
  return num === null ? '--' : fmt.number(num, digits);
}

export function fmtUsd(value, digits = 2) {
  const num = numOrNull(value);
  return num === null ? '--' : fmt.usd(num, digits);
}

/** 小数比例转百分比（0.0123 -> 1.23%） */
export function fmtPct(fraction, digits = 2) {
  const num = numOrNull(fraction);
  return num === null ? '--' : fmt.pct(num, digits);
}
