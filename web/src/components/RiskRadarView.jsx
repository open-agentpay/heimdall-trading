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
import { useTranslation } from 'react-i18next';
import { fmt } from '../i18n';
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
  const { t, i18n } = useTranslation('risk');
  const isZh = String(i18n.resolvedLanguage || i18n.language || '').startsWith('zh');
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
        setRadarError(radarRes.status === 'rejected' ? (radarRes.reason?.message || t('radar.requestFailed')) : t('radar.noRules'));
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
      // 对账一致但恢复被拒绝（凭证缺失 / 同账户冲突 / 实盘安全预检未通过）时如实提示，不显示“已恢复”
      const resumeRefused = isClean && resumeIfClean && !res.resumed && res.resume_error;
      setReconcileMsg({
        ok: Boolean(isClean) && !resumeRefused,
        text: resumeRefused
          ? t('reconcile.resumeRefused', { reason: res.resume_error })
          : isClean
          ? (res.resumed ? t('reconcile.resumed') : t('reconcile.clean'))
          : `${Array.isArray(res?.report?.discrepancies) ? t('reconcile.foundCount', { n: res.report.discrepancies.length }) : t('reconcile.found')}${res?.report?.bot_paused ? t('reconcile.autoPaused') : ''}`,
      });
      await fetchRadarAndReconcile();
    } catch (err) {
      setReconcileMsg({ ok: false, text: t('reconcile.failed', { message: err.message }) });
    } finally {
      setIsReconciling(false);
    }
  };

  const handleKillTrigger = async () => {
    if (!(await showConfirm(t('kill.confirmMessage'), { title: t('kill.confirmTitle'), confirmText: t('kill.confirmButton'), danger: true }))) {
      return;
    }

    setIsKilling(true);
    try {
      const res = await triggerEmergencyKill();
      // 后端提示目前只有中文；其他语言用前端文案
      setKillSuccessMessage((isZh && res.message) || t('kill.success'));
      if (onEmergencyKill) onEmergencyKill();
      fetchRadarAndReconcile();
    } catch (err) {
      showAlert(err.message || t('common:unknownError'), { title: t('kill.failedTitle'), tone: 'error' });
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
            <h1 className="text-sm font-semibold text-ink tracking-tight">{t('header.title')}</h1>
            <p className="text-xs text-ink-3">
              {t('header.subtitle')}
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
            <span>{t('header.probe')}</span>
          </button>

          <button
            onClick={handleKillTrigger}
            disabled={isKilling}
            className="flex items-center space-x-2 px-4 py-1.5 bg-brand hover:bg-brand-hover text-white text-xs font-semibold rounded shadow-sm hover:shadow transition-all cursor-pointer active:scale-95"
          >
            <Power className="w-3.5 h-3.5" />
            <span>{isKilling ? t('kill.killing') : t('kill.button')}</span>
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
            {t('common:dismiss')}
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
                    ? t('overall.triggered')
                    : overallStatus === 'WARNING'
                    ? t('overall.warning')
                    : isUnknown
                    ? (radarError ? t('overall.unavailable') : t('overall.loading'))
                    : t('overall.healthy')}
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
                  {rules.length > 0 ? t('overall.rulesClear', { clear: rules.length - triggeredCount, total: rules.length }) : t('overall.awaiting')}
                </span>
              </div>
              <p className="text-xs text-ink-3 mt-0.5">
                {isUnknown && radarError
                  ? t('overall.symbolError', { symbol, error: radarError })
                  : t('overall.symbolDesc', { symbol })}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-6 text-right">
            <div>
              <div className="text-xs text-ink-3 uppercase tracking-wider">{t('overall.lastProbe')}</div>
              <div className="text-xs font-mono font-medium text-ink mt-0.5">
                {radarData?.timestamp ? fmt.dateTime(radarData.timestamp) : '--:--:--'}
              </div>
            </div>
            <div>
              <div className="text-xs text-ink-3 uppercase tracking-wider">{t('overall.blockState')}</div>
              <div
                className={`text-xs font-bold mt-0.5 ${
                  isUnknown ? 'text-ink-4' : triggeredCount > 0 ? 'text-danger' : warningCount > 0 ? 'text-warning' : 'text-success'
                }`}
              >
                {isUnknown
                  ? '--'
                  : triggeredCount > 0
                  ? t('overall.blocking', { n: triggeredCount })
                  : warningCount > 0
                  ? t('overall.warnings', { n: warningCount })
                  : t('overall.noneTriggered')}
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
                {t('reconcile.title')}
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
                {isReconciling ? t('reconcile.running') : t('reconcile.runNow')}
              </button>
              {botPausedByRecon && (
                <button
                  onClick={() => handleManualReconcile(true)}
                  disabled={isReconciling}
                  className="px-3 py-1.5 rounded-lg bg-success-solid hover:bg-success-solid-hover text-white text-xs font-semibold transition cursor-pointer"
                >
                  {t('reconcile.resume')}
                </button>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">{t('reconcile.consistency')}</div>
              <div className="mt-1 font-bold flex items-center space-x-1.5">
                {lastRecon ? (
                  lastRecon.is_clean ? (
                    <span className="text-success flex items-center space-x-1">
                      <CheckCircle2 className="w-3.5 h-3.5" />
                      <span>{t('reconcile.cleanStatus')}</span>
                    </span>
                  ) : (
                    <span className="text-danger">
                      {t('reconcile.diffStatus', { n: lastRecon.discrepancies?.length || 0 })}
                    </span>
                  )
                ) : (
                  <span className="text-ink-3">{t('reconcile.notRun')}</span>
                )}
              </div>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">{t('reconcile.botStatus')}</div>
              <div className="mt-1 font-mono font-bold text-ink">
                {reconcileData?.bot_status || 'UNKNOWN'} · {t('reconcile.openPositions', { n: reconcileData?.open_positions_count ?? 0 })}
              </div>
            </div>

            <div className="p-3 rounded-lg bg-panel border border-line-soft">
              <div className="text-xs text-ink-3">{t('reconcile.deadManSwitch')}</div>
              <div className="mt-1 font-mono font-bold">
                {dms.running ? (
                  <span className="text-success">{t('reconcile.dmsOnline', { n: dms.ping_count || 0 })}</span>
                ) : (
                  <span className="text-warning">{t('reconcile.dmsOff')}</span>
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
                ? t('rules.unavailable', { error: radarError })
                : t('rules.connecting')}
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
                          {t('rules.ruleNo', { id: rule.id })}
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
                      {(isZh && rule.name) || t(`rules.items.r${rule.id}.name`, { defaultValue: rule.name })}
                    </h3>
                    <p className="text-xs text-ink-3 leading-relaxed">
                      {(isZh && rule.desc) || t(`rules.items.r${rule.id}.desc`, { defaultValue: rule.desc })}
                    </p>
                  </div>

                  <div className="mt-4 pt-3 border-t border-line-soft flex items-center justify-between text-xs">
                    <span className="text-xs text-ink-3">{t('rules.liveTelemetry')}</span>
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
