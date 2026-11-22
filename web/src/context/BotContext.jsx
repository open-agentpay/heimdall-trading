/**
 * ================================================================================
 * Heimdall 交易平台 - 机器人集群全局状态上下文 (BotContext)
 * ================================================================================
 */

import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import i18n from '../i18n';
import { listBots, getBotLatestState, getBotNav, getBotTrades, stepBot } from '../api/endpoints';

const BotContext = createContext(null);

export function BotProvider({ children }) {
  const [bots, setBots] = useState([]);
  const [activeBotId, setActiveBotId] = useState("bot_eth_debate_01");
  const [activeState, setActiveState] = useState(null);
  const [navData, setNavData] = useState([]);
  const [trades, setTrades] = useState([]);
  const [isLoading, setIsLoading] = useState(false);

  // 1. 获取所有机器人列表
  const fetchBots = useCallback(async () => {
    try {
      const data = await listBots();
      if (Array.isArray(data) && data.length > 0) {
        setBots(data);
      }
    } catch (e) {
      console.warn('[BotContext] 拉取机器人列表告警:', e);
    }
  }, []);

  // 2. 获取当前机器人的最新推演状态
  const fetchActiveState = useCallback(async (botId = activeBotId, refresh = false) => {
    if (!botId) return;
    try {
      const res = await getBotLatestState(botId, refresh);
      if (res && res.latest_decision) {
        setActiveState(res.latest_decision);
      }
    } catch (e) {
      console.warn('[BotContext] 拉取对弈台最新状态告警:', e);
    }
  }, [activeBotId]);

  // 3. 获取当前机器人的净值与流水
  const fetchActiveAnalytics = useCallback(async (botId = activeBotId) => {
    if (!botId) return;
    try {
      const [navRes, tradesRes] = await Promise.all([
        getBotNav(botId).catch(() => ({ nav_history: [] })),
        getBotTrades(botId).catch(() => []),
      ]);
      if (navRes && navRes.nav_history) {
        setNavData(navRes.nav_history);
      }
      if (Array.isArray(tradesRes)) {
        setTrades(tradesRes);
      }
    } catch (e) {
      console.warn('[BotContext] 拉取分析数据告警:', e);
    }
  }, [activeBotId]);

  // 4. 手动单步触发推演
  const triggerStep = useCallback(async (botId = activeBotId) => {
    if (!botId) return;
    setIsLoading(true);
    try {
      await stepBot(botId);
      await Promise.all([
        fetchActiveState(botId, false),
        fetchActiveAnalytics(botId),
      ]);
    } finally {
      setIsLoading(false);
    }
  }, [activeBotId, fetchActiveState, fetchActiveAnalytics]);

  useEffect(() => {
    fetchBots();
  }, [fetchBots]);

  useEffect(() => {
    fetchActiveState(activeBotId);
    fetchActiveAnalytics(activeBotId);
  }, [activeBotId, fetchActiveState, fetchActiveAnalytics]);

  const activeBot = bots.find(b => b.bot_id === activeBotId) || bots[0] || null;

  return (
    <BotContext.Provider
      value={{
        bots,
        setBots,
        activeBotId,
        setActiveBotId,
        activeBot,
        activeState,
        setActiveState,
        navData,
        trades,
        isLoading,
        fetchBots,
        fetchActiveState,
        fetchActiveAnalytics,
        triggerStep,
      }}
    >
      {children}
    </BotContext.Provider>
  );
}

export function useBot() {
  const context = useContext(BotContext);
  if (!context) {
    throw new Error(i18n.t('app:context.useBotOutside'));
  }
  return context;
}
