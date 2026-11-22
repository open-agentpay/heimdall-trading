/**
 * ================================================================================
 * Heimdall 交易平台 - 前端统一 API 客户端 (HTTP Client)
 * ================================================================================
 *
 * 核心特性：
 * 1. 统一基地址与超时控制；
 * 2. 自动从 localStorage 提取 Web3 钱包鉴权生成的 session_token 注入 Authorization: Bearer 头；
 * 3. 全局拦截 HTTP 401 会话过期/未授权响应，自动清理失效会话并广播 `heimdall:auth-expired` 事件；
 * 4. 统一错误捕获、HTTP 状态码解析与友好的中文异常消息抛出。
 */

import i18n from '../i18n';

// 使用空值合并运算符 (??)，确保 Docker 生产构建传入 VITE_API_BASE_URL="" 时走同源 Nginx 反向代理
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

export class ApiError extends Error {
  constructor(status, message, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

/**
 * 封装通用 HTTP 请求方法
 * @param {string} endpoint 接口相对路径 (例如 "/api/v1/bots")
 * @param {object} options fetch 选项
 * @returns {Promise<any>} 返回 JSON 解析数据
 */
export async function apiRequest(endpoint, options = {}) {
  const url = `${API_BASE_URL}${endpoint}`;

  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  // 自动从本地持久化中附加 Web3 Bearer 会话鉴权 Token（不再使用不安全的 URL query 传参）
  const token = localStorage.getItem('heimdall_session_token');
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const config = {
    ...options,
    headers,
  };

  // 默认 15 秒超时控制（回测类长耗时请求可通过 options.timeout 覆盖）
  const timeoutMs = options.timeout || 15000;
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...config,
      signal: controller.signal,
    });
    clearTimeout(id);

    const contentType = response.headers.get('content-type');
    let responseData = null;
    if (contentType && contentType.includes('application/json')) {
      responseData = await response.json();
    } else {
      responseData = await response.text();
    }

    if (!response.ok) {
      const errorMsg = (responseData && responseData.detail) || response.statusText || i18n.t('app:api.requestFailed');

      // P0 安全对齐：收到 401 说明 Bearer Token 缺失、无效或已过期，立即清理本地失效会话并通知顶层唤起重新签名
      if (response.status === 401 && !options.skipAuthExpiredEvent) {
        if (token) {
          console.warn('[API 客户端] Bearer 会话已过期或失效，自动清理本地 Token 并触发重新鉴权');
          localStorage.removeItem('heimdall_session_token');
          localStorage.removeItem('heimdall_wallet_connected');
          localStorage.removeItem('heimdall_wallet_state');
        }
        if (typeof window !== 'undefined') {
          window.dispatchEvent(
            new CustomEvent('heimdall:auth-expired', {
              detail: { endpoint, message: errorMsg },
            })
          );
        }
      }

      throw new ApiError(response.status, errorMsg, responseData);
    }

    return responseData;
  } catch (err) {
    clearTimeout(id);
    if (err.name === 'AbortError') {
      throw new ApiError(408, i18n.t('app:api.timeout'));
    }
    if (err instanceof ApiError) {
      throw err;
    }
    throw new ApiError(0, err.message || i18n.t('app:api.network'));
  }
}
