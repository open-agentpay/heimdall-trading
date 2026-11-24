/**
 * ================================================================================
 * Heimdall 交易平台 - 后端 REST API 端点函数库 (Endpoints)
 * ================================================================================
 *
 * 【P0 安全对齐】：
 * 全面移除所有接口 URL 上残留的 `?wallet_address=...` 查询参数拼接，
 * 所有鉴权统一由 `api/client.js` 通过 `Authorization: Bearer <session_token>` 请求头携带。
 */

import { apiRequest } from './client';

// ==============================================================================
// 1. Web3 钱包登录鉴权 (SIWE Auth)
// ==============================================================================

/** 获取防重放挑战明文 */
export async function getAuthNonce(address, chainId = 1) {
  return apiRequest(`/api/v1/auth/nonce?address=${encodeURIComponent(address)}&chain_id=${chainId}`, {
    method: 'GET',
    skipAuthExpiredEvent: true,
  });
}

/** 提交签名真实验签并换取 Session Token */
export async function verifySignature(address, signature, message) {
  return apiRequest('/api/v1/auth/verify', {
    method: 'POST',
    body: JSON.stringify({ address, signature, message }),
    skipAuthExpiredEvent: true,
  });
}

/** 本地退出登录并清空所有会话缓存 */
export function clearAuthSession() {
  localStorage.removeItem('heimdall_session_token');
  localStorage.removeItem('heimdall_wallet_address');
  localStorage.removeItem('heimdall_wallet_connected');
  localStorage.removeItem('heimdall_wallet_state');
}

// ==============================================================================
// 2. 敏感密钥与凭证安全金库 (Key Vault)
// ==============================================================================

/** 获取当前已鉴权钱包托管的脱敏凭证列表 */
export async function listVaultKeys() {
  return apiRequest('/api/v1/vault/keys', { method: 'GET' });
}

/** 提交新增凭证 (密文入库，明文即刻销毁) */
export async function createVaultKey(data) {
  return apiRequest('/api/v1/vault/keys', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

/** 撤销并彻底物理销毁指定的凭证 (由 Bearer Token 校验归属) */
export async function deleteVaultKey(keyId) {
  return apiRequest(`/api/v1/vault/keys/${encodeURIComponent(keyId)}`, {
    method: 'DELETE',
  });
}

/** 切换已有币安凭证的网络环境（测试网/模拟盘 <-> 正式网） */
export async function updateVaultKeyNetwork(keyId, isTestnet) {
  return apiRequest(`/api/v1/vault/keys/${encodeURIComponent(keyId)}/network`, {
    method: 'PATCH',
    body: JSON.stringify({ is_testnet: Boolean(isTestnet) }),
  });
}

/** 更新指定凭据的配置信息 (model, base_url, label 及可选密钥内容) */
export async function updateVaultKey(keyId, data) {
  return apiRequest(`/api/v1/vault/keys/${encodeURIComponent(keyId)}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

/** 设为当前默认激活密钥（多密钥中切换活跃大模型） */
export async function activateVaultKey(keyId) {
  return apiRequest(`/api/v1/vault/keys/${encodeURIComponent(keyId)}/activate`, {
    method: 'POST',
  });
}

/** 测试已保存凭证的真实连通性探针 */
export async function testVaultKey(keyId) {
  return apiRequest(`/api/v1/vault/keys/${encodeURIComponent(keyId)}/test`, {
    method: 'POST',
  });
}

/** 保存前直接发起连通性探针预检 */
export async function testDirectVaultKey(provider, payload) {
  return apiRequest('/api/v1/vault/test-direct', {
    method: 'POST',
    body: JSON.stringify({ provider, payload }),
  });
}

// ==============================================================================
// 3. 交易机器人管理与调度控制 (Bot Management & Control)
// ==============================================================================

/** 获取所有机器人的运行状态概览及绩效数据 */
export async function listBots() {
  return apiRequest('/api/v1/bots', { method: 'GET' });
}

/** 获取指定机器人最新的全量多 Agent 决策状态 (refresh=true 需要有效 Bearer Token) */
export async function getBotLatestState(botId, refresh = false) {
  const query = refresh ? '?refresh=true' : '';
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/latest-state${query}`, { method: 'GET' });
}

/** 创建新的交易机器人并持久化 (支持 strategy_config 与 execution_mode) */
export async function createBot(botData) {
  return apiRequest('/api/v1/bots', {
    method: 'POST',
    body: JSON.stringify(botData),
  });
}

/** 修改机器人的分配资金并平滑重算份额 */
export async function updateBotCapital(botId, allocatedCapital) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/capital`, {
    method: 'PATCH',
    body: JSON.stringify({ allocated_capital: Number(allocatedCapital) }),
  });
}

/** 查询全部机器人的组合资金分布及占比权重 */
export async function getPortfolioCapitalDistribution() {
  return apiRequest('/api/v1/portfolio/capital-distribution', { method: 'GET' });
}

/** 网页启动机器人时使用的决策周期（秒），与后端 StartBotRequest 默认值一致：15 分钟一轮 */
export const BOT_DECISION_INTERVAL_SECONDS = 900;

/** 启动指定机器人的 24 小时后台守护协程 */
export async function startBot(botId, intervalSeconds = BOT_DECISION_INTERVAL_SECONDS) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/start`, {
    method: 'POST',
    body: JSON.stringify({ interval_seconds: intervalSeconds }),
  });
}

/** 停止指定机器人 */
export async function stopBot(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/stop`, { method: 'POST' });
}

/** 停止并彻底删除指定机器人（含其历史决策、净值与成交流水） */
export async function deleteBot(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}`, { method: 'DELETE' });
}

/** 手动单步触发单次多智能体决策循环 */
export async function stepBot(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/step`, { method: 'POST' });
}

/** 全局紧急熔断急停总开关 (全撤单 + 强制关停调度) */
export async function triggerEmergencyKill(botId = null) {
  const query = botId ? `?bot_id=${encodeURIComponent(botId)}` : '';
  return apiRequest(`/api/v1/emergency/kill${query}`, { method: 'POST' });
}

// ==============================================================================
// 4. 量化绩效核算与可解释性审计溯源 (Analytics & Audit)
// ==============================================================================

/** 获取指定机器人的单位净值走势时序数据与量化指标 */
export async function getBotNav(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/nav`, { method: 'GET' });
}

/** 获取思维链决策与辩论历史记录（支持分页与多维筛选） */
export async function getBotDecisions(botId, params = 20) {
  if (typeof params === 'number') {
    return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/decisions?limit=${params}`, { method: 'GET' });
  }
  const query = new URLSearchParams();
  if (params.page !== undefined) query.set('page', params.page);
  if (params.pageSize !== undefined) query.set('page_size', params.pageSize);
  if (params.limit !== undefined) query.set('limit', params.limit);
  if (params.action && params.action !== 'ALL') query.set('action', params.action);
  if (params.status && params.status !== 'ALL') query.set('status', params.status);
  if (params.executionStatus && params.executionStatus !== 'ALL') query.set('execution_status', params.executionStatus);
  if (params.search && params.search.trim()) query.set('search', params.search.trim());
  if (params.paginate !== undefined) {
    query.set('paginate', params.paginate ? 'true' : 'false');
  } else {
    query.set('paginate', 'true');
  }
  const qs = query.toString();
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/decisions${qs ? `?${qs}` : ''}`, { method: 'GET' });
}

/** 查询成交流水记录 */
export async function getBotTrades(botId, limit = 50) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/trades?limit=${limit}`, { method: 'GET' });
}

/** 提交沙盒模拟交易下单 (已接入后端 HardRiskGuard 硬风控校验) */
export async function placePaperOrder(botId, orderData) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/orders/paper`, {
    method: 'POST',
    body: JSON.stringify(orderData),
  });
}

/** 获取沙盒下单报价：跨所实时参考价、价格来源与指定价偏差容差（只读，与下单接口同一取价规则） */
export async function getPaperOrderQuote(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/orders/paper/quote`, { method: 'GET' });
}

/** 获取 7 大极端行情避险雷达遥测数据 */
export async function getRiskRadarTelemetry(symbol = 'ETH') {
  return apiRequest(`/api/v1/market/radar?symbol=${encodeURIComponent(symbol)}`, { method: 'GET' });
}

/** 系统健康状态检查 */
export async function checkHealth() {
  return apiRequest('/health', { method: 'GET' });
}

// ==============================================================================
// 5. OMS 订单状态机、对账引擎与实盘就绪自检 (OMS & Live Readiness)
// ==============================================================================

/** 查询指定机器人的 OMS 托管订单、原生 SL/TP 保护单及标的精度规格 */
export async function listBotOmsOrders(botId, limit = 50) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/oms/orders?limit=${limit}`, { method: 'GET' });
}

/** 人工触发 UNKNOWN 超时挂起订单的交易所查单对账恢复 */
export async function recoverOmsOrder(botId, clientOrderId) {
  return apiRequest(
    `/api/v1/bots/${encodeURIComponent(botId)}/oms/orders/${encodeURIComponent(clientOrderId)}/recover`,
    { method: 'POST' }
  );
}

/** 查询指定机器人的最近对账状态、幽灵仓熔断与外部死信心跳快照 */
export async function getReconcileStatus(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/reconcile/status`, { method: 'GET' });
}

/** 手动触发一次即时交易所对账（可选在对账一致后恢复运行） */
export async function triggerBotReconcile(botId, resumeIfClean = false) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/reconcile`, {
    method: 'POST',
    body: JSON.stringify({ resume_if_clean: Boolean(resumeIfClean) }),
  });
}

/** 实盘就绪 5 项预检清单查询（传递 exchange / market_type / execution_mode） */
export async function checkLiveReadiness({
  exchange = 'BINANCE',
  marketType = 'USDM_FUTURES',
  executionMode = 'TESTNET',
  symbol = 'ETH',
  botId = null,
} = {}) {
  const params = new URLSearchParams({
    exchange,
    market_type: marketType,
    execution_mode: executionMode,
    symbol,
  });
  if (botId) params.append('bot_id', botId);
  return apiRequest(`/api/v1/system/live-readiness?${params.toString()}`, { method: 'GET' });
}

// ==============================================================================
// 6. StrategyRouter 策略目录、MacroBias 旁路门控与零前视回测实验室
// ==============================================================================

/** 获取 StrategyRouter 已注册策略目录及可调超参 Schema */
export async function getStrategiesCatalog() {
  return apiRequest('/api/v1/strategies/catalog', { method: 'GET' });
}

/** 查询机器人当前低频 LLM 宏观旁路门控状态 */
export async function getBotMacroBias(botId) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/macro-bias`, { method: 'GET' });
}

/** 下发或清除机器人低频 LLM 宏观旁路门控 */
export async function setBotMacroBias(botId, payload) {
  return apiRequest(`/api/v1/bots/${encodeURIComponent(botId)}/macro-bias`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/** 运行严格零前视单段事件驱动回测 */
export async function runZeroLookaheadBacktest(payload) {
  return apiRequest('/api/v1/backtest/run', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeout: 60000,
  });
}

/** 运行 Walk-Forward 样本外滚动验证 */
export async function runWalkForwardBacktest(payload) {
  return apiRequest('/api/v1/backtest/walk-forward', {
    method: 'POST',
    body: JSON.stringify(payload),
    timeout: 90000,
  });
}
