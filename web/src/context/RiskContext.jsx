/**
 * ================================================================================
 * Heimdall 交易平台 - 极端风控与熔断避险上下文 (RiskContext)
 * ================================================================================
 */

import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { getRiskRadarTelemetry, triggerEmergencyKill } from '../api/endpoints';

const RiskContext = createContext(null);

export function RiskProvider({ children }) {
  const [radarData, setRadarData] = useState(null);
  const [isLoadingRadar, setIsLoadingRadar] = useState(false);
  const [isEmergencyKilling, setIsEmergencyKilling] = useState(false);
  const [riskAlerts, setRiskAlerts] = useState([]);

  // 1. 获取 7 大极端行情避险雷达状态
  const fetchRadar = useCallback(async (symbol = 'ETH') => {
    setIsLoadingRadar(true);
    try {
      const res = await getRiskRadarTelemetry(symbol);
      if (res && res.rules) {
        setRadarData(res);
      }
    } catch (e) {
      console.warn('[RiskContext] 拉取雷达遥测告警:', e);
    } finally {
      setIsLoadingRadar(false);
    }
  }, []);

  // 2. 触发一键紧急拔网线急停
  const executeEmergencyKill = useCallback(async (botId = null) => {
    setIsEmergencyKilling(true);
    try {
      const res = await triggerEmergencyKill(botId);
      // 记录本地告警流水
      setRiskAlerts(prev => [
        {
          id: `kill_${Date.now()}`,
          rule: 'EMERGENCY_KILL',
          message: res?.message || '已触发全屏紧急急停，撤销所有在途订单',
          timestamp: new Date().toLocaleTimeString(),
          level: 'CRITICAL',
        },
        ...prev,
      ]);
      return res;
    } finally {
      setIsEmergencyKilling(false);
    }
  }, []);

  // 3. 接收外部推送告警
  const pushAlert = useCallback((alert) => {
    setRiskAlerts(prev => [
      {
        id: `alert_${Date.now()}`,
        timestamp: new Date().toLocaleTimeString(),
        ...alert,
      },
      ...prev.slice(0, 49),
    ]);
  }, []);

  useEffect(() => {
    fetchRadar('ETH');
    const timer = setInterval(() => {
      fetchRadar('ETH');
    }, 10000);
    return () => clearInterval(timer);
  }, [fetchRadar]);

  return (
    <RiskContext.Provider
      value={{
        radarData,
        isLoadingRadar,
        isEmergencyKilling,
        riskAlerts,
        fetchRadar,
        executeEmergencyKill,
        pushAlert,
      }}
    >
      {children}
    </RiskContext.Provider>
  );
}

export function useRisk() {
  const context = useContext(RiskContext);
  if (!context) {
    throw new Error('useRisk 必须在 RiskProvider 内部调用');
  }
  return context;
}
