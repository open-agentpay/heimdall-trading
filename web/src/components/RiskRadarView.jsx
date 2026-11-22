/**
 * ================================================================================
 * Heimdall 交易平台 - 七大极端行情避险雷达与对账/心跳监控看板 (RiskRadarView)
 * ================================================================================
 *
 * 核心功能：
 * 1. 实时对接 `/api/v1/market/radar` 展示 7 大极端行情硬风控规则与真实触发状态（消除静态写死健康假象）；
 * 2. 新增「对账引擎（ExchangeReconciler）与外部死信心跳（DeadManSwitch）」监控区，支持手动即时对账与幽灵仓恢复；
 * 3. 【全屏红线一键急停 (Emergency Kill)】总控制台：真实调用后端接口撤销全挂单。
 */

import React, { useState, useEffect } from 'react';
import { showAlert, showConfirm } from '../utils/dialog';
import {
  ShieldAlert, ShieldCheck, AlertTriangle, Power, RefreshCw, AlertOctagon, Radio, CheckCircle2
} from 'lucide-react';
import {
  getRiskRadarTelemetry,
  triggerEmergencyKill,
  getReconcileStatus,
  triggerBotReconcile,
} from '../api/endpoints';

export default function RiskRadarView({
  onEmergencyKill,
  symbol = 'ETH',
  activeBotId = 'bot_eth_debate_01',
}) {
  const [isKilling, setIsKilling] = useState(false);
  const [killSuccessMessage, setKillSuccessMessage] = useState(null);
  const [radarData, setRadarData] = useState(null);
  const [reconcileData, setReconcileData] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isReconciling, setIsReconciling] = useState(false);
  const [reconcileMsg, setReconcileMsg] = useState(null);
  const [radarError, setRadarError] = useState(null);

  const fetchRadarAndReconcile = async () => {
    setIsLoading(true);
    try {
      const [radarRes, reconRes] = await Promise.allSettled([
        getRiskRadarTelemetry(symbol || 'ETH'),
        getReconcileStatus(activeBotId),
      ]);
      if (radarRes.status === 'fulfilled' && radarRes.value && radarRes.value.rules) {
        setRadarData(radarRes.value);
        setRadarError(null);
      } else {
        // 遥测失败时清空旧数据，避免继续展示过期的“健康”结论
        setRadarData(null);
        setRadarError(radarRes.status === 'rejected' ? (radarRes.reason?.message || '雷达接口请求失败') : '雷达接口未返回规则数据');
      }
      if (reconRes.status === 'fulfilled' && reconRes.value) {
        setReconcileData(reconRes.value);
      }
    } catch (err) {
      console.warn('[RiskRadarView] 拉取雷达与对账状态告警:', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchRadarAndReconcile();
    const interval = setInterval(fetchRadarAndReconcile, 8000);
    return () => clearInterval(interval);
  }, [symbol, activeBotId]);

  const rules = radarData?.rules || [];
  const triggeredCount = rules.filter((r) => r.status === 'TRIGGERED').length;
  const warningCount = rules.filter((r) => r.status === 'WARNING').length;
  // 没有遥测数据时状态为 UNKNOWN，不再默认显示“全防线健康”
  const overallStatus = rules.length === 0
    ? 'UNKNOWN'
    : (radarData?.overall_status || (triggeredCount > 0 ? 'TRIGGERED' : warningCount > 0 ? 'WARNING' : 'HEALTHY'));
  const isUnknown = overallStatus === 'UNKNOWN';

  const handleManualReconcile = async (resumeIfClean = false) => {
    setIsReconciling(true);
    setReconcileMsg(null);
    try {
      const res = await triggerBotReconcile(activeBotId, resumeIfClean);
      const isClean = res?.report?.is_clean;
      setReconcileMsg({
        ok: Boolean(isClean),
        text: isClean
          ? (res.resumed ? '对账一致，机器人已从暂停/熔断状态恢复运行！' : '交易所与本地持仓对账完全一致，无幽灵持仓偏差。')
          : `对账发现${Array.isArray(res?.report?.discrepancies) ? ` ${res.report.discrepancies.length} 项` : ''}偏差${res?.report?.bot_paused ? '，系统已自动暂停机器人保护本金！' : ''}`,
      });
      await fetchRadarAndReconcile();
    } catch (err) {
      setReconcileMsg({ ok: false, text: `对账失败: ${err.message}` });
    } finally {
      setIsReconciling(false);
    }
  };

  const handleKillTrigger = async () => {
    if (!(await showConfirm('确定要触发全局紧急熔断急停吗？\n这将立即向所有交易所广播全撤单指令，并强制关停所有策略后台协程。', { title: '全局紧急熔断', confirmText: '确认急停', danger: true }))) {
      return;
    }

    setIsKilling(true);
    try {
      const res = await triggerEmergencyKill();
      setKillSuccessMessage(res.message || '紧急急停成功触发！所有挂单已撤销。');
      if (onEmergencyKill) onEmergencyKill();
      fetchRadarAndReconcile();
    } catch (err) {
      showAlert(err.message || '未知错误', { title: '急停失败', tone: 'error' });
    } finally {
      setIsKilling(false);
    }
  };

  const dms = reconcileData?.dead_man_switch || {};
  const lastRecon = reconcileData?.last_reconciliation;
  const botPausedByRecon = reconcileData?.bot_status === 'PAUSED' || reconcileData?.bot_status === 'CIRCUIT_BROKEN';

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      {/* 顶部控制栏 */}
      <div className="h-14 bg-panel border-b border-line px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-muted border border-line-strong flex items-center justify-center text-brand">
            <ShieldAlert className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-ink tracking-tight">七大极端行情避险雷达 &amp; 对账守卫</h1>
            <p className="text-xs text-ink-3">
              代码确定性毫秒级拦截守卫 · 交易所持仓对账与外部死信心跳实时监控
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={fetchRadarAndReconcile}
            disabled={isLoading}
            className="flex items-center space-x-1.5 px-3 py-1.5 text-xs text-ink-2 hover:text-ink bg-surface border border-line-strong rounded shadow-xs hover:border-ink transition-all cursor-pointer"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            <span>实时探测</span>
          </button>

          <button
            onClick={handleKillTrigger}
            disabled={isKilling}
            className="flex items-center space-x-2 px-4 py-1.5 bg-brand hover:bg-brand-hover text-white text-xs font-semibold rounded shadow-sm hover:shadow transition-all cursor-pointer active:scale-95"
          >
            <Power className="w-3.5 h-3.5" />
            <span>{isKilling ? '正在拔网线急停...' : '一键全局紧急急停'}</span>
          </button>
        </div>
      </div>

      {/* 成功急停提示 Banner */}
      {killSuccessMessage && (
        <div className="bg-brand/10 border-b border-brand/30 px-6 py-2.5 flex items-center justify-between text-xs text-brand">
          <div className="flex items-center space-x-2">
            <AlertOctagon className="w-4 h-4 shrink-0" />
            <span className="font-medium">{killSuccessMessage}</span>
          </div>
          <button
            onClick={() => setKillSuccessMessage(null)}
            className="text-brand font-bold hover:underline cursor-pointer"
          >
            关闭提示
          </button>
        </div>
      )}

      {/* 主面板内容区 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* 1. 全局防御状态综述卡（严格基于后端真实遥测动态计算） */}
        <div
          className={`p-5 rounded-xl border shadow-xs flex items-center justify-between ${
            overallStatus === 'TRIGGERED'
              ? 'border-danger-line bg-danger-soft'
              : overallStatus === 'WARNING'
              ? 'border-warning-line bg-warning-soft'
              : isUnknown
              ? 'border-line bg-canvas'
              : 'border-line-strong bg-panel'
          }`}
        >
          <div className="flex items-center space-x-4">
            <div
              className={`w-12 h-12 rounded-full border flex items-center justify-center ${
                overallStatus === 'TRIGGERED'
                  ? 'bg-danger-solid/15 border-danger/30 text-danger'
                  : overallStatus === 'WARNING'
                  ? 'bg-warning-solid/15 border-warning/30 text-warning'
                  : isUnknown
                  ? 'bg-ink-4/10 border-ink-4/30 text-ink-4'
                  : 'bg-success-solid/10 border-success/30 text-success'
              }`}
            >
              {overallStatus === 'TRIGGERED' ? (
                <AlertOctagon className="w-6 h-6" />
              ) : overallStatus === 'WARNING' || isUnknown ? (
                <AlertTriangle className="w-6 h-6" />
              ) : (
                <ShieldCheck className="w-6 h-6" />
              )}
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-base font-semibold text-ink">
                  {overallStatus === 'TRIGGERED'
                    ? '极端行情硬风控已触发拦截'
                    : overallStatus === 'WARNING'
                    ? '防线处于警戒状态（请留意波动）'
                    : isUnknown
                    ? (radarError ? '风控雷达遥测不可用' : '正在获取风控雷达遥测…')
                    : '全部规则正常'}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full font-medium border ${
                    overallStatus === 'TRIGGERED'
                      ? 'bg-danger-solid/15 text-danger border-danger/30'
                      : overallStatus === 'WARNING'
                      ? 'bg-warning-solid/15 text-warning border-warning/30'
                      : isUnknown
                      ? 'bg-ink-4/15 text-ink-3 border-ink-4/20'
                      : 'bg-success-solid/15 text-success border-success/20'
                  }`}
                >
                  {rules.length > 0 ? `${rules.length - triggeredCount}/${rules.length} 规则未触发` : '等待遥测'}
                </span>
              </div>
              <p className="text-xs text-ink-3 mt-0.5">
                {isUnknown && radarError
                  ? `标的对象: ${symbol} · ${radarError}`
                  : `标的对象: ${symbol} · 任何分钟级剧烈插针、跨所基差隔离或宏观静默窗口，系统将在毫秒内硬驳回开仓提案`}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-6 text-right">
            <div>
              <div className="text-xs text-ink-3 uppercase tracking-wider">最近探测时间</div>
              <div className="text-xs font-mono font-medium text-ink mt-0.5">
                {radarData?.timestamp ? new Date(radarData.timestamp).toLocaleTimeString() : '--:--:--'}
              </div>
            </div>
            <div>
              <div className="text-xs text-ink-3 uppercase tracking-wider">当前拦截状态</div>
              <div
                className={`text-xs font-bold mt-0.5 ${
                  isUnknown ? 'text-ink-4' : triggeredCount > 0 ? 'text-danger' : warningCount > 0 ? 'text-warning' : 'text-success'
                }`}
              >
                {isUnknown
                  ? '--'
                  : triggeredCount > 0
                  ? `${triggeredCount} 项拦截中`
                  : warningCount > 0
                  ? `${warningCount} 项告警`
                  : '0 触发 (放行中)'}
              </div>
            </div>
          </div>
        </div>

        {/* 2. 对账引擎 (ExchangeReconciler) & 外部死信心跳 (DeadManSwitch) 面板 */}
        <div className="p-5 rounded-xl border border-line-strong bg-surface shadow-xs space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center space-x-2.5">
              <Radio className="w-4 h-4 text-brand" />
              <h2 className="text-xs font-bold text-ink">
                交易所持仓对账引擎 (ExchangeReconciler) &amp; 外部死信心跳
              </h2>
              <span className="text-xs font-mono px-2 py-0.5 rounded-full bg-muted text-ink-2">
                Bot: {activeBotId} ({reconcileData?.market_type || '--'} · {reconcileData?.execution_mode || '--'})
              </span>
            </div>

            <div className="flex items-center space-x-2">
              <button
                onClick={() => handleManualReconcile(false)}
                disabled={isReconciling}
                className="px-3 py-1.5 rounded-lg bg-panel hover:bg-muted border border-line-strong text-xs font-semibold text-ink transition cursor-pointer"
              >
                {isReconciling ? '对账中...' : '🔍 立即执行对账'}
              </button>
              {botPausedByRecon && (
                <button
                  onClick={() => handleManualReconcile(true)}
                  disabled={isReconciling}
                  className="px-3 py-1.5 rounded-lg bg-success-solid hover:bg-success-solid-hover text-white text-xs font-semibold transition cursor-pointer"
                >
                  ✅ 核查无误并恢复运行
                </button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">对账一致性状态</div>
              <div className="mt-1 font-bold flex items-center space-x-1.5">
                {lastRecon ? (
                  lastRecon.is_clean ? (
                    <span className="text-success flex items-center space-x-1">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>CLEAN (账实完全一致)</span>
                    </span>
                  ) : (
                    <span className="text-danger">
                      ⚠️ 存在差异 ({lastRecon.discrepancies?.length || 0} 项)
                    </span>
                  )
                ) : (
                  <span className="text-ink-3">尚未执行对账（点击右侧按钮即时核对）</span>
                )}
              </div>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">机器人状态 &amp; 持仓数</div>
              <div className="mt-1 font-mono font-bold text-ink">
                {reconcileData?.bot_status || 'UNKNOWN'} · 活动持仓 {reconcileData?.open_positions_count ?? 0} 笔
              </div>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">外部死信心跳 (DeadManSwitch)</div>
              <div className="mt-1 font-mono font-bold">
                {dms.running ? (
                  <span className="text-success">● 在线 ({dms.ping_count || 0} 次心跳)</span>
                ) : (
                  <span className="text-warning">○ 未启用 (开发模式)</span>
                )}
              </div>
            </div>
          </div>

          {reconcileMsg && (
            <div
              className={`px-3 py-2 rounded-lg text-xs font-medium border ${
                reconcileMsg.ok
                  ? 'bg-success-soft text-success border-success-line'
                  : 'bg-danger-soft text-danger border-danger-line'
              }`}
            >
              {reconcileMsg.text}
            </div>
          )}
        </div>

        {/* 3. 7 大规则网格卡片 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {rules.length === 0 ? (
            <div className="col-span-full p-8 text-center text-xs text-ink-3 bg-surface rounded-xl border border-line-strong">
              {radarError
                ? `风控雷达遥测不可用：${radarError}`
                : '正在连接后端 /api/v1/market/radar 获取风控遥测数据…'}
            </div>
          ) : (
            rules.map((rule) => {
              const isHealthy = rule.status === 'HEALTHY';
              const isTriggered = rule.status === 'TRIGGERED';
              return (
                <div
                  key={rule.id}
                  className={`p-4 rounded-xl border transition-all flex flex-col justify-between shadow-xs ${
                    isTriggered
                      ? 'border-danger-line bg-danger-soft'
                      : 'border-line-strong bg-panel hover:border-line-strong'
                  }`}
                >
                  <div>
                    <div className="flex items-start justify-between">
                      <div className="flex items-center space-x-2">
                        <span className="text-lg">{rule.icon}</span>
                        <span className="text-xs font-bold text-ink line-clamp-1">
                          规则 {rule.id}
                        </span>
                      </div>
                      <span
                        className={`text-xs font-mono px-2 py-0.5 rounded-full border ${
                          isHealthy
                            ? 'bg-success-solid/10 text-success border-success/20'
                            : isTriggered
                            ? 'bg-danger-solid/15 text-danger border-danger/30 font-bold'
                            : 'bg-warning-solid/15 text-warning border-warning/30'
                        }`}
                      >
                        {rule.status}
                      </span>
                    </div>

                    <h3 className="text-xs font-semibold text-ink mt-2 mb-1">
                      {rule.name}
                    </h3>
                    <p className="text-xs text-ink-3 leading-relaxed">
                      {rule.desc}
                    </p>
                  </div>

                  <div className="mt-4 pt-3 border-t border-line-soft flex items-center justify-between text-xs">
                    <span className="text-xs text-ink-3">当前实时遥测</span>
                    <span className={`font-mono font-semibold ${rule.valueColor || 'text-ink-3'}`}>
                      {rule.value}
                    </span>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
