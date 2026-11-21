/**
 * ================================================================================
 * Heimdall 交易平台 - WebSocket 实时流式通信 Hook (useWebSocket)
 * ================================================================================
 *
 * 核心特性：
 * 1. 单例长连接保护：彻底防止 React 重新渲染引发的雪崩式重复建连；
 * 2. 回调 Ref 化 (Callback Ref Pattern)：onStep / onRiskAlert 变更不触发重连；
 * 3. 正常销毁与异常断开隔离：组件卸载时不触发重连计时器；
 * 4. 指数退避重连与 15s 心跳保活 (Ping/Pong)。
 */

import { useEffect, useRef, useState, useCallback } from 'react';

export function useWebSocket(options = {}) {
  const {
    url,
    topics = ['klines', 'steps', 'orders', 'risk', 'radar'],
    onMessage,
    onStep,
    onOrder,
    onRiskAlert,
    reconnectInterval = 2000,
    maxReconnectAttempts = 10,
  } = options;

  const [isConnected, setIsConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState(null);

  // 使用 Ref 固化回调函数，避免父组件每轮 render 生成新函数导致重复建连
  const callbacksRef = useRef({});
  callbacksRef.current = { onMessage, onStep, onOrder, onRiskAlert };

  // 固化配置与连接控制
  const topicsRef = useRef(topics);
  topicsRef.current = topics;

  const wsRef = useRef(null);
  const isIntentionalCloseRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  const pingTimerRef = useRef(null);

  // 计算 WebSocket 服务端地址（Docker 生产环境走同源 Nginx /ws/stream 反代；本地开发默认连接 :8000）
  const getWsUrl = useCallback(() => {
    const base = url || import.meta.env.VITE_WS_URL || (() => {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      if (import.meta.env.VITE_API_BASE_URL === '') {
        return `${protocol}//${window.location.host}/ws/stream`;
      }
      const host = window.location.hostname || 'localhost';
      const port = '8000';
      return `${protocol}//${host}:${port}/ws/stream`;
    })();
    const token = localStorage.getItem('heimdall_session_token');
    if (token) {
      const sep = base.includes('?') ? '&' : '?';
      return `${base}${sep}token=${encodeURIComponent(token)}`;
    }
    return base;
  }, [url]);

  useEffect(() => {
    let isMounted = true;
    isIntentionalCloseRef.current = false;

    const clearTimers = () => {
      if (pingTimerRef.current) {
        clearInterval(pingTimerRef.current);
        pingTimerRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (!isMounted) return;

      // 如果已有连接处于连接中或已打开，直接复用
      if (wsRef.current && (wsRef.current.readyState === WebSocket.CONNECTING || wsRef.current.readyState === WebSocket.OPEN)) {
        return;
      }

      clearTimers();
      const wsUrl = getWsUrl();

      try {
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
          if (!isMounted) return;
          setIsConnected(true);
          reconnectAttemptsRef.current = 0;

          // 发送订阅
          const currentTopics = topicsRef.current;
          if (currentTopics && currentTopics.length > 0) {
            ws.send(JSON.stringify({
              action: 'subscribe',
              topics: currentTopics,
            }));
          }

          // 启动 15 秒心跳保活
          pingTimerRef.current = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ action: 'ping' }));
            }
          }, 15000);
        };

        ws.onmessage = (event) => {
          if (!isMounted) return;
          try {
            const payload = JSON.parse(event.data);
            setLastEvent(payload);

            const { onMessage: cbMsg, onStep: cbStep, onOrder: cbOrder, onRiskAlert: cbAlert } = callbacksRef.current;
            if (cbMsg) cbMsg(payload);
            if (payload.event === 'AGENT_STEP' && cbStep) {
              cbStep(payload.data, payload.bot_id);
            }
            if ((payload.event === 'ORDER_FILLED' || payload.event === 'ORDER_UPDATE' || payload.event === 'OMS_UPDATE') && cbOrder) {
              cbOrder(payload.data, payload.bot_id);
            }
            if (payload.event === 'RISK_ALERT' && cbAlert) {
              cbAlert(payload.data, payload.bot_id);
            }
          } catch (e) {
            console.warn('[useWebSocket] 解析报文告警:', e);
          }
        };

        ws.onclose = (event) => {
          if (!isMounted) return;
          setIsConnected(false);
          clearTimers();

          // 若非组件卸载或主动关闭，则执行指数退避重连
          if (!isIntentionalCloseRef.current && reconnectAttemptsRef.current < maxReconnectAttempts) {
            const delay = Math.min(reconnectInterval * Math.pow(1.5, reconnectAttemptsRef.current), 30000);
            reconnectAttemptsRef.current += 1;
            reconnectTimerRef.current = setTimeout(() => {
              if (isMounted) connect();
            }, delay);
          }
        };

        ws.onerror = (err) => {
          console.warn('[useWebSocket] 连接遇到波动:', err);
          if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
            ws.close();
          }
        };
      } catch (err) {
        console.warn('[useWebSocket] 初始化异常:', err);
      }
    };

    connect();

    return () => {
      isMounted = false;
      isIntentionalCloseRef.current = true;
      clearTimers();
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch (e) {}
        wsRef.current = null;
      }
    };
  }, [getWsUrl, reconnectInterval, maxReconnectAttempts]);

  const send = useCallback((data) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }, []);

  return {
    isConnected,
    lastEvent,
    send,
  };
}
