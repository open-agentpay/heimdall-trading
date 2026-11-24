/**
 * ================================================================================
 * Heimdall 交易平台 - 痕迹溯源与审计轨迹视图 (AuditTracesView)
 * ================================================================================
 * 
 * 核心功能：
 * 1. 完整展示历史任务时间轴（Task ID, Trace ID, 标的, 周期, 最终结论）；
 * 2. 深入审查每个决策的证据链：Analyst 研报、多空对抗辩论要点、裁判判词；
 * 3. 审查两道风控规则核验明细与物理硬拦截记录；
 * 4. 展示 StepTrace 耗时流水线（微秒级可观测性）与本地降级标记；
 * 5. 下单执行结果（2026-11-24）：与"风控结论"分开显示每轮是否真的下了单——已成交 / 已挂单 / 发单前拦截 /
 *    拒单 / 待对账，以及每笔订单"决策时"与"当前"的状态（后端 execution 字段）。
 */

import React, { useState, useEffect } from 'react';
import { 
  History, Search, Filter, ShieldCheck, CheckCircle2, XCircle, 
  Clock, ArrowUpRight, ArrowDownRight, Minus, ChevronDown, ChevronUp,
  Cpu, Activity, RefreshCw, FileText, Sparkles, Terminal
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { fmt } from '../i18n';
import { getBotDecisions } from '../api/endpoints';
import { numOrNull } from '../data/agentRoles';

export default function AuditTracesView({ activeBotId = "bot_eth_debate_01" }) {
  const { t } = useTranslation('audit');
  const [decisions, setDecisions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [filterStance, setFilterStance] = useState('ALL'); // ALL | LONG | SHORT | NEUTRAL
  const [filterStatus, setFilterStatus] = useState('ALL'); // ALL | COMPLETED | REJECTED | HALTED | FAILURE
  const [filterExecution, setFilterExecution] = useState('ALL'); // ALL | FILLED | SUBMITTED | BLOCKED | REJECTED | CANCELED
  const [searchKeyword, setSearchKeyword] = useState('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [filterCounts, setFilterCounts] = useState({});
  const [jumpPageInput, setJumpPageInput] = useState('');
  const [expandedTraceId, setExpandedTraceId] = useState(null);
  const [selectedStepDetail, setSelectedStepDetail] = useState(null); // 查看指定 Step 的 Prompt 与 LLM 输出详情

  // 归一化后端决策记录：只使用后端真实字段，缺失就标记为缺失（null），绝不补默认结论。
  // 兼容两种来源：内存决策记录（含 step_traces / risk_review 等完整字段）与数据库历史记录
  // （仅含 thinking_logs、proposal、risk_approved、rejection_reason、market_regime）。
  // 不再内置演示用的假历史轨迹，也不再推断“非观望即放行”、补全“全部规则 PASS”或编造 42ms 等耗时。
  const normalizeDecision = (item, idx) => {
    if (!item) return null;
    // 被拒绝（CYCLE_REFUSED）或出错（CYCLE_ERROR）的轮次：后端来自运行事件，只有时间与原因，没有决策内容
    if (item.record_type === 'CYCLE_REFUSED' || item.record_type === 'CYCLE_ERROR') {
      return {
        rowKey: item.event_id || `evt_${item.timestamp || idx}`,
        isFailure: true,
        decisionId: null,
        symbol: item.symbol || '--',
        timeframe: null,
        action: null,
        confidence: null,
        regime: null,
        verdict: null,
        bullThesis: null,
        bearThesis: null,
        checkedRules: {},
        riskApproved: null,
        status: item.record_type === 'CYCLE_ERROR' ? 'ERROR' : 'REFUSED',
        stepTraces: [],
        totalLatencyMs: null,
        idempotencyKey: null,
        rejectionReason: null,
        failureReason: item.error || null,
        execution: null,
        timestamp: item.timestamp ? String(item.timestamp).replace('T', ' ').slice(0, 19) : '--',
        timestampRaw: item.timestamp ?? null,
      };
    }
    const synthesis = item.debate_synthesis || {};
    const analyst = item.analyst_brief || {};
    const bull = item.bull_thesis || {};
    const bear = item.bear_thesis || {};
    const risk = item.risk_review || {};
    const proposal = item.trade_proposal || item.proposal || {};
    const logs = Array.isArray(item.thinking_logs) ? item.thinking_logs : [];
    const findLog = (...keys) => logs.find((l) => keys.some((k) => String(l).includes(k))) || null;

    const action = item.debate_consensus || synthesis.consensus_action || null;
    const confidence = numOrNull(synthesis.final_confidence);
    const regime = item.analyst_regime || item.market_regime || analyst.regime || null;
    const verdict = synthesis.verdict || findLog('Moderator', 'Verdict', '裁判') || null; // i18n-ignore 匹配后端中文思维链日志
    const bullThesis = bull.key_thesis || findLog('Bull', '多头') || null; // i18n-ignore 匹配后端中文思维链日志
    const bearThesis = bear.key_thesis || findLog('Bear', '空头') || null; // i18n-ignore 匹配后端中文思维链日志
    const checkedRules = risk.checked_rules && typeof risk.checked_rules === 'object' ? risk.checked_rules : {};
    const hasProposal = Boolean(item.proposal_generated || item.trade_proposal || item.proposal);

    // 风控结论：优先读 risk_review.approved；数据库记录读 risk_approved（1/0），但 0 只有在确有提案时才算“拦截”
    let riskApproved = null;
    if (typeof risk.approved === 'boolean') riskApproved = risk.approved;
    else if (item.risk_approved === true || item.risk_approved === 1) riskApproved = true;
    else if ((item.risk_approved === false || item.risk_approved === 0) && hasProposal) riskApproved = false;

    // COMPLETED: 提案通过风控 | REJECTED: 风控拦截 | HALTED: 本轮无提案 | UNKNOWN: 无法判断
    const status = riskApproved === true
      ? 'COMPLETED'
      : riskApproved === false
      ? 'REJECTED'
      : !hasProposal
      ? 'HALTED'
      : 'UNKNOWN';

    const stepTraces = (Array.isArray(item.step_traces) ? item.step_traces : []).map((st, i) => {
      const sName = st.step_name || st.name || `Step${i + 1}`;
      let outputData = null;
      if (sName.includes("Analyst")) outputData = item.analyst_brief || null;
      else if (sName.includes("Sentiment")) outputData = item.sentiment_report || null;
      else if (sName.includes("Moderator")) outputData = item.debate_synthesis || null;
      else if (sName.includes("Debate")) outputData = (item.bull_thesis || item.bear_thesis) ? { bull: item.bull_thesis, bear: item.bear_thesis } : null;
      else if (sName.includes("Strategy")) outputData = item.trade_proposal || null;
      else if (sName.includes("Risk") || sName.includes("Guard")) outputData = item.risk_review || null;
      const lat = numOrNull(st.latency_ms ?? st.duration_ms);
      return {
        name: sName,
        agent: st.agent_id || st.agent_role || st.agent || '--',
        latency: lat === null ? null : Math.round(lat * 10) / 10,
        tokensUsed: numOrNull(st.tokens_used),
        fallback: Boolean(st.fallback_used ?? st.fallback),
        status: st.status || '--',
        prompt: st.prompt || null,
        llmResponse: st.llm_response || null,
        inputSummary: st.input_summary || null,
        outputSummary: st.output_summary || null,
        errorMessage: st.error_message || null,
        realOutput: outputData,
      };
    });
    const knownLatencies = stepTraces.map((st) => st.latency).filter((v) => v !== null);
    const totalLatencyMs = knownLatencies.length > 0
      ? Math.round(knownLatencies.reduce((a, b) => a + b, 0) * 10) / 10
      : null;

    // 执行结果：后端 execution 字段（新记录由决策周期写入；升级前的记录由后端推断，带 inferred）。
    // 订单的 current_* 字段是接口返回前从 OMS 补上的"当前"状态（例如已挂单的限价单之后被超时撤销）。
    const exec = item.execution && typeof item.execution === 'object' ? item.execution : null;
    const execution = exec ? {
      status: exec.status || null,
      currentStatus: exec.current_status || null,
      reason: exec.reason || null,
      inferred: Boolean(exec.inferred),
      orders: (Array.isArray(exec.orders) ? exec.orders : []).map((o) => ({
        proposalId: o.proposal_id || null,
        clientOrderId: o.client_order_id || null,
        orderType: o.order_type || null,
        side: o.side || null,
        status: o.status || null,
        currentState: o.current_state || null,
        reason: o.current_reason || o.reason || null,
        requestedQty: o.requested_qty ?? null,
        filledQty: o.current_filled_qty ?? o.filled_qty ?? null,
        limitPrice: o.limit_price ?? null,
        avgFillPrice: o.avg_fill_price ?? null,
      })),
    } : null;

    return {
      rowKey: item.decision_id || item.timestamp || `row_${idx}`,
      decisionId: item.decision_id || null,
      symbol: item.symbol || '--',
      timeframe: item.timeframe || null,
      action,
      confidence,
      regime,
      verdict,
      bullThesis,
      bearThesis,
      checkedRules,
      riskApproved,
      status,
      stepTraces,
      totalLatencyMs,
      idempotencyKey: proposal.idempotency_key || null,
      rejectionReason: risk.rejection_reason || risk.rejection_rule || item.rejection_reason || null,
      execution,
      timestamp: item.timestamp ? String(item.timestamp).replace('T', ' ').slice(0, 19) : '--',
      timestampRaw: item.timestamp ?? null,
    };
  };

  // 执行结果状态 → 文案（键名写全，便于多语言静态检查核对）
  const execLabel = (s) => ({
    FILLED: t('execution.status.filled'),
    PARTIAL: t('execution.status.partial'),
    SUBMITTED: t('execution.status.submitted'),
    CANCELED: t('execution.status.canceled'),
    BLOCKED: t('execution.status.blocked'),
    REJECTED: t('execution.status.rejected'),
    UNKNOWN: t('execution.status.unknown'),
    NO_PROPOSAL: t('execution.status.noProposal'),
    RISK_REJECTED: t('execution.status.riskRejected'),
  }[s] || t('execution.status.unrecorded'));

  // 执行结果状态 → 配色：成交绿、挂单中蓝、没发出去 / 被拒红、待确认 / 已撤销 / 未记录黄
  const execStyle = (s) => (
    s === 'FILLED' ? 'bg-success-soft text-success border-success-line'
      : (s === 'PARTIAL' || s === 'SUBMITTED') ? 'bg-info-soft text-info border-info-line'
      : (s === 'BLOCKED' || s === 'REJECTED') ? 'bg-danger-soft text-danger border-danger-line'
      : (s === 'UNKNOWN' || s === 'CANCELED' || s === 'UNRECORDED') ? 'bg-warning-soft text-warning border-warning-line'
      : 'bg-canvas text-ink-3 border-line'
  );

  // 没有进入下单的两种结果（无提案 / 风控拦截）已由风控徽章表达，不再重复显示执行结果
  const NOT_SENT = ['NO_PROPOSAL', 'RISK_REJECTED'];

  const fetchTraces = async () => {
    setLoading(true);
    try {
      const res = await getBotDecisions(activeBotId, {
        page,
        pageSize,
        action: filterStance,
        status: filterStatus,
        executionStatus: filterExecution,
        search: searchKeyword,
        paginate: true,
      });
      if (res && Array.isArray(res.items)) {
        setDecisions(res.items.map((d, idx) => normalizeDecision(d, idx)).filter(Boolean));
        setTotal(res.total ?? res.items.length);
        setTotalPages(res.total_pages ?? Math.max(1, Math.ceil((res.total || 0) / pageSize)));
        setFilterCounts(res.counts || {});
      } else if (Array.isArray(res)) {
        setDecisions(res.map((d, idx) => normalizeDecision(d, idx)).filter(Boolean));
        setTotal(res.length);
        setTotalPages(Math.max(1, Math.ceil(res.length / pageSize)));
      } else {
        setDecisions([]);
        setTotal(0);
        setTotalPages(1);
      }
    } catch (err) {
      console.warn('[AuditTracesView] 加载真实决策审计记录异常:', err);
      setDecisions([]);
      setTotal(0);
      setTotalPages(1);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const timer = setTimeout(() => {
      fetchTraces();
    }, 200);
    return () => clearTimeout(timer);
  }, [activeBotId, page, pageSize, filterStance, filterStatus, filterExecution, searchKeyword]);

  useEffect(() => {
    setPage(1);
  }, [activeBotId]);

  const hasActiveFilter = filterStance !== 'ALL' || filterStatus !== 'ALL' || filterExecution !== 'ALL' || Boolean(searchKeyword.trim());

  const handleResetFilters = () => {
    setFilterStance('ALL');
    setFilterStatus('ALL');
    setFilterExecution('ALL');
    setSearchKeyword('');
    setPage(1);
  };

  const getPageNumbers = () => {
    if (totalPages <= 7) {
      return Array.from({ length: totalPages }, (_, i) => i + 1);
    }
    if (page <= 4) {
      return [1, 2, 3, 4, 5, '...', totalPages];
    }
    if (page >= totalPages - 3) {
      return [1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
    }
    return [1, '...', page - 1, page, page + 1, '...', totalPages];
  };

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      
      {/* 顶部控制栏 */}
      <div className="bg-panel border-b border-line px-6 py-3 shrink-0 space-y-3">
        {/* 第一行：标题与全局搜索、刷新 */}
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 rounded-lg bg-muted border border-line-strong flex items-center justify-center text-brand">
              <History className="w-4 h-4" />
            </div>
            <div>
              <h1 className="text-sm font-bold text-ink flex items-center space-x-2">
                <span>{t('header.title')}</span>
                <span className="text-xs font-mono bg-muted text-ink-3 border border-line-strong px-2 py-0.5 rounded-full font-semibold">
                  {t('header.sourceBadge')}
                </span>
              </h1>
              <p className="text-xs text-ink-3">
                {t('header.subtitle')}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-3">
            {/* 关键词搜索框 */}
            <div className="relative w-64">
              <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-ink-4" />
              <input
                type="text"
                value={searchKeyword}
                onChange={(e) => { setSearchKeyword(e.target.value); setPage(1); }}
                placeholder={t('filter.searchPlaceholder')}
                className="w-full pl-8 pr-7 py-1.5 text-xs bg-surface border border-line rounded-lg text-ink placeholder:text-ink-4 focus:outline-hidden focus:border-brand transition shadow-2xs font-mono"
              />
              {searchKeyword && (
                <button
                  onClick={() => { setSearchKeyword(''); setPage(1); }}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-4 hover:text-ink text-xs font-bold"
                  title={t('filter.searchClear')}
                >
                  ✕
                </button>
              )}
            </div>

            {/* 刷新按钮 */}
            <button 
              onClick={fetchTraces}
              disabled={loading}
              className="p-1.5 bg-surface border border-line-strong hover:border-brand rounded-lg text-xs font-semibold text-ink-2 flex items-center space-x-1.5 shadow-2xs transition"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin text-brand' : ''}`} />
              <span>{t('common:refresh')}</span>
            </button>
          </div>
        </div>

        {/* 第二行：多维筛选器 */}
        <div className="flex items-center justify-between text-xs pt-2 border-t border-line/60">
          <div className="flex items-center space-x-3 flex-wrap gap-y-2">
            {/* 1. 多空方向 */}
            <div className="flex bg-muted p-0.5 rounded-lg border border-line-strong text-xs">
              {[
                { id: 'ALL', label: t('filter.all') },
                { id: 'LONG', label: t('filter.long') },
                { id: 'SHORT', label: t('filter.short') },
                { id: 'NEUTRAL', label: t('filter.neutral') },
              ].map((stance) => (
                <button
                  key={stance.id}
                  onClick={() => { setFilterStance(stance.id); setPage(1); }}
                  className={`px-2.5 py-1 rounded-md font-semibold transition ${
                    filterStance === stance.id
                      ? "bg-surface text-ink shadow-2xs"
                      : "text-ink-3 hover:text-ink"
                  }`}
                >
                  {stance.label}
                  {filterCounts?.actions?.[stance.id] !== undefined && stance.id !== 'ALL' && (
                    <span className="ml-1 opacity-60 font-mono text-[10px]">({filterCounts.actions[stance.id]})</span>
                  )}
                </button>
              ))}
            </div>

            {/* 2. 结论状态下拉 */}
            <div className="flex items-center space-x-1.5">
              <span className="text-ink-4 font-medium">{t('filter.statusLabel')}:</span>
              <select
                value={filterStatus}
                onChange={(e) => { setFilterStatus(e.target.value); setPage(1); }}
                className="bg-surface border border-line text-ink rounded-lg px-2.5 py-1 text-xs focus:outline-hidden focus:border-brand font-medium shadow-2xs cursor-pointer"
              >
                <option value="ALL">{t('filter.statusAll')}</option>
                <option value="COMPLETED">{t('filter.statusCompleted')}</option>
                <option value="REJECTED">{t('filter.statusRejected')}</option>
                <option value="HALTED">{t('filter.statusHalted')}</option>
                <option value="FAILURE">{t('filter.statusFailure')}</option>
              </select>
            </div>

            {/* 3. 执行结果下拉 */}
            <div className="flex items-center space-x-1.5">
              <span className="text-ink-4 font-medium">{t('filter.execLabel')}:</span>
              <select
                value={filterExecution}
                onChange={(e) => { setFilterExecution(e.target.value); setPage(1); }}
                className="bg-surface border border-line text-ink rounded-lg px-2.5 py-1 text-xs focus:outline-hidden focus:border-brand font-medium shadow-2xs cursor-pointer"
              >
                <option value="ALL">{t('filter.execAll')}</option>
                <option value="FILLED">{t('filter.execFilled')}</option>
                <option value="SUBMITTED">{t('filter.execSubmitted')}</option>
                <option value="BLOCKED">{t('filter.execBlocked')}</option>
                <option value="REJECTED">{t('filter.execRejected')}</option>
                <option value="CANCELED">{t('filter.execCanceled')}</option>
              </select>
            </div>
          </div>

          {/* 筛选重置 */}
          {hasActiveFilter && (
            <button
              onClick={handleResetFilters}
              className="px-2.5 py-1 text-xs text-brand hover:text-brand-hover hover:bg-brand-soft rounded-lg font-semibold flex items-center space-x-1 transition"
            >
              <span>✕</span>
              <span>{t('filter.reset')}</span>
            </button>
          )}
        </div>
      </div>

      {/* 痕迹时间轴列表 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {loading && (
          <div className="p-3 text-center text-xs text-ink-3 bg-surface/70 rounded-xl border border-line flex items-center justify-center space-x-2">
            <RefreshCw className="w-3.5 h-3.5 animate-spin text-brand" />
            <span>{t('common:loading')}</span>
          </div>
        )}
        {!loading && decisions.length === 0 && (
          <div className="p-8 text-center text-xs text-ink-3 bg-surface rounded-xl border border-dashed border-line-strong">
            {total === 0 && !hasActiveFilter
              ? t('empty.noRecords')
              : t('empty.noMatch')}
          </div>
        )}
        {decisions.map((item) => {
          const isExpanded = expandedTraceId === item.rowKey;
          const isApproved = item.status === "COMPLETED";
          const isHalted = item.status === "HALTED";
          const isRejected = item.status === "REJECTED";
          const isFailed = item.status === "ERROR" || item.status === "REFUSED";
          const statusStyle = isApproved
            ? 'bg-success-soft text-success border-success-line'
            : (isRejected || isFailed)
            ? 'bg-danger-soft text-danger border-danger-line'
            : 'bg-canvas text-ink-3 border-line';
          const statusText = isApproved
            ? t('status.completed')
            : isRejected
            ? t('status.rejected')
            : item.status === 'ERROR'
            ? t('status.error')
            : item.status === 'REFUSED'
            ? t('status.refused')
            : isHalted
            ? t('status.halted')
            : t('status.unknown');
          // 执行结果徽章：优先显示订单"当前"状态，没有则显示决策时的状态
          const execShown = item.execution ? (item.execution.currentStatus || item.execution.status) : null;

          return (
            <div 
              key={item.rowKey}
              className="bg-surface rounded-xl border border-line shadow-xs overflow-hidden transition hover:border-line-strong"
            >
              {/* 卡片摘要首行 */}
              <div 
                onClick={() => setExpandedTraceId(isExpanded ? null : item.rowKey)}
                className="p-4 flex items-center justify-between cursor-pointer hover:bg-panel transition"
              >
                <div className="flex items-center space-x-4">
                  {/* 方向图标 */}
                  <div className={`w-9 h-9 rounded-xl flex items-center justify-center font-bold text-base border ${
                    item.action === 'LONG' 
                      ? 'bg-success-soft text-success border-success-line' 
                      : item.action === 'SHORT'
                      ? 'bg-brand-soft text-brand border-brand-line'
                      : 'bg-subtle text-ink-3 border-line-strong'
                  }`}>
                    {item.action === 'LONG' ? <ArrowUpRight className="w-5 h-5" /> : item.action === 'SHORT' ? <ArrowDownRight className="w-5 h-5" /> : <Minus className="w-5 h-5" />}
                  </div>

                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-bold text-ink font-mono">{item.symbol}</span>
                      {item.timeframe && (
                        <span className="text-xs bg-muted text-ink-3 px-1.5 py-0.5 rounded font-mono font-medium">{item.timeframe}</span>
                      )}
                      <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${
                        item.action === 'LONG'
                          ? 'bg-success-soft text-success border-success-line'
                          : item.action === 'SHORT'
                          ? 'bg-brand-soft text-brand border-brand-line'
                          : 'bg-canvas text-ink-3 border-line'
                      }`}>
                        {t('row.verdict', { action: item.action || '--' })}{item.confidence !== null ? ` ${t('row.confidence', { pct: Math.round(item.confidence * 100) })}` : ''}
                      </span>
                      {item.regime && <span className="text-xs text-ink-4 font-mono">[{item.regime}]</span>}
                    </div>

                    <div className="flex items-center space-x-3 text-xs text-ink-3 mt-1 font-mono">
                      <span>{t('row.decisionId', { id: item.decisionId || '--' })}</span>
                      <span>{t('row.time', { time: fmt.dateTime(item.timestampRaw) })}</span>
                      <span>{t('row.latency', { latency: item.totalLatencyMs !== null ? `${item.totalLatencyMs}ms` : '--' })}</span>
                    </div>
                  </div>
                </div>

                {/* 状态指示与折叠箭头 */}
                <div className="flex items-center space-x-3">
                  <div className={`px-2.5 py-1 rounded-full text-xs font-bold border flex items-center space-x-1.5 ${statusStyle}`}>
                    {isApproved ? <CheckCircle2 className="w-3.5 h-3.5" /> : (isRejected || isFailed) ? <XCircle className="w-3.5 h-3.5" /> : <Minus className="w-3.5 h-3.5" />}
                    <span>{statusText}</span>
                  </div>
                  {execShown && !item.isFailure && !NOT_SENT.includes(execShown) && (
                    <div className={`px-2.5 py-1 rounded-full text-xs font-bold border ${execStyle(execShown)}`}>
                      {t('execution.badge', { status: execLabel(execShown) })}
                    </div>
                  )}

                  <div className="text-ink-4">
                    {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </div>
                </div>
              </div>

              {/* 展开的深度细节 */}
              {isExpanded && (
                <div className="px-6 py-4 bg-panel border-t border-line space-y-4 text-xs animate-in fade-in duration-100">
                  {item.isFailure ? (
                    /* 失败轮次：只有原因，没有裁决、辩论、风控与步骤明细 */
                    <div className="bg-surface p-3.5 rounded-xl border border-danger-line space-y-2">
                      <div className="font-bold text-danger flex items-center space-x-1.5">
                        <XCircle className="w-4 h-4" />
                        <span>{item.status === 'ERROR' ? t('detail.cycleError') : t('detail.cycleRefused')}</span>
                      </div>
                      <p className="text-ink-2 leading-relaxed bg-panel p-2.5 rounded-lg border border-line-soft break-all">
                        {item.failureReason || t('detail.noFailureReason')}
                      </p>
                      <p className="text-xs text-ink-4">{t('detail.failureHint')}</p>
                    </div>
                  ) : (<>

                  {/* 1. 裁判裁决书与幂等控制 */}
                  <div className="bg-surface p-3.5 rounded-xl border border-line space-y-2">
                    <div className="flex items-center justify-between text-ink font-bold">
                      <span className="flex items-center space-x-1.5">
                        <span>⚖️</span>
                        <span>{t('detail.moderator')}</span>
                      </span>
                      <span className="font-mono text-xs text-ink-3 bg-muted px-2 py-0.5 rounded border border-line-strong">
                        IdempotencyKey: {item.idempotencyKey || '--'}
                      </span>
                    </div>
                    <p className="text-ink-2 leading-relaxed bg-panel p-2.5 rounded-lg border border-line-soft">
                      {item.verdict || t('detail.noVerdict')}
                    </p>
                  </div>

                  {/* 2. 多空辩论两派立论对照 */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="bg-surface p-3.5 rounded-xl border border-success-line space-y-1.5">
                      <div className="font-bold text-success flex items-center space-x-1.5">
                        <span>🐂</span>
                        <span>{t('detail.bull')}</span>
                      </div>
                      <p className="text-success leading-relaxed text-xs">
                        {item.bullThesis || t('detail.noBull')}
                      </p>
                    </div>

                    <div className="bg-surface p-3.5 rounded-xl border border-brand-line space-y-1.5">
                      <div className="font-bold text-brand flex items-center space-x-1.5">
                        <span>🐻</span>
                        <span>{t('detail.bear')}</span>
                      </div>
                      <p className="text-ink-2 leading-relaxed text-xs">
                        {item.bearThesis || t('detail.noBear')}
                      </p>
                    </div>
                  </div>

                  {/* 3. 风控核验规则清单 */}
                  <div className="bg-surface p-3.5 rounded-xl border border-line space-y-2">
                    <div className="font-bold text-ink flex items-center justify-between">
                      <span className="flex items-center space-x-1.5">
                        <ShieldCheck className="w-4 h-4 text-brand" />
                        <span>{t('detail.riskCheck')}</span>
                      </span>
                      {item.rejectionReason && (
                        <span className="text-danger font-medium text-xs">
                          {t('detail.rejectReason', { reason: item.rejectionReason })}
                        </span>
                      )}
                    </div>

                    {Object.keys(item.checkedRules || {}).length === 0 && (
                      <div className="text-xs text-ink-4">{t('detail.noRules')}</div>
                    )}
                    <div className="grid grid-cols-3 gap-2 pt-1 font-mono text-xs">
                      {item.checkedRules && Object.entries(item.checkedRules).map(([ruleName, passed]) => (
                        <div 
                          key={ruleName}
                          className={`p-2 rounded-lg border flex items-center justify-between ${
                            passed 
                              ? 'bg-success-soft border-success-line text-success' 
                              : 'bg-danger-soft border-danger-line text-danger'
                          }`}
                        >
                          <span className="truncate pr-1">{ruleName}</span>
                          <span className="font-bold">{passed ? "PASS" : "REJECT"}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* 3.5 下单执行结果：风控通过之后订单到底发没发出去、成交没有 */}
                  {item.execution && !NOT_SENT.includes(item.execution.status) && (
                    <div className="bg-surface p-3.5 rounded-xl border border-line space-y-2">
                      <div className="font-bold text-ink flex items-center justify-between">
                        <span className="flex items-center space-x-1.5">
                          <Activity className="w-4 h-4 text-brand" />
                          <span>{t('execution.title')}</span>
                        </span>
                        <span className="flex items-center space-x-1.5 text-xs font-medium">
                          <span className="text-ink-4">{t('execution.decisionTime')}</span>
                          <span className={`px-2 py-0.5 rounded-full border font-bold ${execStyle(item.execution.status)}`}>
                            {execLabel(item.execution.status)}
                          </span>
                          {item.execution.currentStatus && item.execution.currentStatus !== item.execution.status && (
                            <>
                              <span className="text-ink-4">→ {t('execution.now')}</span>
                              <span className={`px-2 py-0.5 rounded-full border font-bold ${execStyle(item.execution.currentStatus)}`}>
                                {execLabel(item.execution.currentStatus)}
                              </span>
                            </>
                          )}
                        </span>
                      </div>

                      {item.execution.reason && (
                        <p className="text-danger leading-relaxed bg-panel p-2.5 rounded-lg border border-line-soft break-all">
                          {t('execution.reason', { reason: item.execution.reason })}
                        </p>
                      )}
                      {execShown === 'BLOCKED' && <p className="text-xs text-ink-3">{t('execution.blockedHint')}</p>}
                      {execShown === 'SUBMITTED' && <p className="text-xs text-ink-3">{t('execution.submittedHint')}</p>}
                      {execShown === 'UNRECORDED' && <p className="text-xs text-ink-3">{t('execution.unrecordedHint')}</p>}
                      {item.execution.inferred && <p className="text-xs text-ink-4">{t('execution.inferred')}</p>}

                      {item.execution.orders.length === 0 && !item.execution.inferred && (
                        <div className="text-xs text-ink-4">{t('execution.noOrders')}</div>
                      )}
                      {item.execution.orders.length > 0 && (
                        <div className="space-y-1.5 font-mono text-xs">
                          {item.execution.orders.length > 1 && (
                            <div className="text-ink-4">{t('execution.orderCount', { n: item.execution.orders.length })}</div>
                          )}
                          <div className="grid grid-cols-12 gap-2 text-ink-4 px-2">
                            <span className="col-span-4">{t('execution.col.order')}</span>
                            <span className="col-span-2">{t('execution.col.type')}</span>
                            <span className="col-span-2">{t('execution.col.status')}</span>
                            <span className="col-span-2">{t('execution.col.qty')}</span>
                            <span className="col-span-2">{t('execution.col.price')}</span>
                          </div>
                          {item.execution.orders.map((o, oi) => {
                            const shown = o.currentState || o.status;
                            return (
                              <div key={o.clientOrderId || `${o.proposalId}_${oi}`} className="bg-panel rounded-lg border border-line-soft p-2 space-y-1">
                                <div className="grid grid-cols-12 gap-2 items-center">
                                  <span className="col-span-4 truncate" title={o.clientOrderId || ''}>{o.clientOrderId || t('execution.noOrderId')}</span>
                                  <span className="col-span-2">{[o.orderType, o.side].filter(Boolean).join(' · ') || '--'}</span>
                                  <span className="col-span-2">
                                    <span className={`px-1.5 py-0.5 rounded border ${execStyle(shown)}`}>{execLabel(shown)}</span>
                                  </span>
                                  <span className="col-span-2">{o.filledQty ?? '--'} / {o.requestedQty ?? '--'}</span>
                                  <span className="col-span-2">{o.limitPrice ?? '--'} / {o.avgFillPrice ?? '--'}</span>
                                </div>
                                {o.reason && (
                                  <div className="text-ink-3 break-all">{t('execution.reason', { reason: o.reason })}</div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  )}

                  {/* 4. 微秒级 StepTrace 可观测流水线 */}
                  {(!item.stepTraces || item.stepTraces.length === 0) && (
                    <div className="p-3 rounded-xl border border-dashed border-line-strong bg-surface text-xs text-ink-4">
                      {t('detail.noSteps')}
                    </div>
                  )}
                  {item.stepTraces && item.stepTraces.length > 0 && (
                    <div className="bg-[#2E2823] text-white p-3.5 rounded-xl space-y-2 font-mono text-xs">
                      <div className="flex items-center justify-between text-[#C2B7A8]">
                        <span className="flex items-center space-x-1.5 text-white font-bold">
                          <Terminal className="w-3.5 h-3.5 text-[#DE6438]" />
                          <span>Runtime StepTraces & Microsecond Latency</span>
                        </span>
                        <span>Total: {item.totalLatencyMs !== null ? `${item.totalLatencyMs}ms` : '--'}</span>
                      </div>

                      <div className="divide-y divide-[#423A33] pt-1">
                        {item.stepTraces.map((st, idx) => (
                          <div 
                            key={idx} 
                            onClick={() => setSelectedStepDetail({ ...st, trace: item })}
                            className="py-1.5 px-2 -mx-2 rounded hover:bg-[#3D352E] cursor-pointer transition flex items-center justify-between text-xs group"
                            title={t('steps.inspectHint')}
                          >
                            <span className="text-[#DFD6C7] flex items-center space-x-2">
                              <span className="text-[#8E8476]">{idx + 1}.</span>
                              <span className="group-hover:text-[#DE6438] font-medium">{st.name}</span>
                              <span className="text-[#7A6F62] text-xs">({st.agent})</span>
                              <span className="text-xs text-[#A69784] opacity-0 group-hover:opacity-100 transition flex items-center space-x-1">
                                <span>{t('steps.inspect')}</span>
                              </span>
                            </span>
                            <div className="flex items-center space-x-3">
                              {st.fallback && (
                                <span className="bg-[#4D3A2C] text-[#E0A86A] text-xs px-1.5 py-0.5 rounded">
                                  FALLBACK
                                </span>
                              )}
                              <span className="text-[#DE6438]">{st.latency !== null ? `${st.latency}ms` : '--'}</span>
                              <span className={st.status === 'SUCCESS' ? 'text-[#62C554]' : 'text-[#EC6A5E]'}>
                                {st.status}
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  </>)}

                </div>
              )}

            </div>
          );
        })}
      </div>

      {/* 底部翻页控制栏 */}
      <div className="h-14 bg-panel border-t border-line px-6 flex items-center justify-between shrink-0 text-xs text-ink-2 select-none">
        {/* 左侧：条数信息与每页大小切换 */}
        <div className="flex items-center space-x-3">
          <span className="text-ink-3">
            {t('pagination.showing', {
              from: total === 0 ? 0 : (page - 1) * pageSize + 1,
              to: Math.min(total, page * pageSize),
              total,
            })}
          </span>
          <div className="h-3.5 w-px bg-line" />
          <div className="flex items-center space-x-1 text-ink-3">
            <select
              value={pageSize}
              onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
              className="bg-surface border border-line text-ink rounded-md px-2 py-0.5 text-xs focus:outline-hidden focus:border-brand font-mono shadow-2xs cursor-pointer"
            >
              {[10, 20, 50, 100].map((sz) => (
                <option key={sz} value={sz}>
                  {t('pagination.pageSize', { size: sz })}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 右侧：页码切换与直达 */}
        <div className="flex items-center space-x-2">
          {/* 首页 */}
          <button
            onClick={() => setPage(1)}
            disabled={page <= 1 || loading}
            className="px-2 py-1 bg-surface border border-line rounded-md text-xs font-medium text-ink-3 hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed transition shadow-2xs"
            title={t('pagination.first')}
          >
            «
          </button>
          {/* 上一页 */}
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1 || loading}
            className="px-2.5 py-1 bg-surface border border-line rounded-md text-xs font-semibold text-ink hover:text-brand disabled:opacity-40 disabled:cursor-not-allowed transition shadow-2xs"
          >
            {t('pagination.prev')}
          </button>

          {/* 数字页码列表 */}
          <div className="flex items-center space-x-1">
            {getPageNumbers().map((pNum, idx) => {
              if (pNum === '...') {
                return (
                  <span key={`dots_${idx}`} className="px-1.5 py-0.5 text-ink-4 font-mono">
                    ...
                  </span>
                );
              }
              const isCur = pNum === page;
              return (
                <button
                  key={pNum}
                  onClick={() => setPage(pNum)}
                  disabled={loading}
                  className={`w-7 h-7 rounded-md font-mono text-xs font-bold transition flex items-center justify-center ${
                    isCur
                      ? 'bg-brand text-white shadow-xs'
                      : 'bg-surface border border-line text-ink-2 hover:border-brand hover:text-brand shadow-2xs'
                  }`}
                >
                  {pNum}
                </button>
              );
            })}
          </div>

          {/* 下一页 */}
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages || loading}
            className="px-2.5 py-1 bg-surface border border-line rounded-md text-xs font-semibold text-ink hover:text-brand disabled:opacity-40 disabled:cursor-not-allowed transition shadow-2xs"
          >
            {t('pagination.next')}
          </button>
          {/* 末页 */}
          <button
            onClick={() => setPage(totalPages)}
            disabled={page >= totalPages || loading}
            className="px-2 py-1 bg-surface border border-line rounded-md text-xs font-medium text-ink-3 hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed transition shadow-2xs"
            title={t('pagination.last')}
          >
            »
          </button>

          {/* 跳转至指定页 */}
          {totalPages > 3 && (
            <div className="flex items-center space-x-1 pl-2 text-ink-4">
              <span>{t('pagination.jumpTo')}</span>
              <input
                type="number"
                min="1"
                max={totalPages}
                value={jumpPageInput}
                onChange={(e) => setJumpPageInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    const target = parseInt(jumpPageInput, 10);
                    if (!isNaN(target) && target >= 1 && target <= totalPages) {
                      setPage(target);
                      setJumpPageInput('');
                    }
                  }
                }}
                className="w-11 px-1.5 py-0.5 bg-surface border border-line rounded-md text-ink text-center font-mono text-xs focus:outline-hidden focus:border-brand shadow-2xs"
              />
              <span>{t('pagination.page')}</span>
            </div>
          )}
        </div>
      </div>

      {/* StepTrace 深度审查抽屉 (Prompt & LLM Response Inspector) */}
      {selectedStepDetail && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 backdrop-blur-xs p-4 animate-in fade-in duration-150">
          <div 
            className="w-full max-w-3xl bg-panel border-2 border-line-strong rounded-3xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 顶栏 */}
            <div className="p-4 bg-surface border-b border-line flex items-center justify-between">
              <div className="flex items-center space-x-3">
                <div className="w-10 h-10 rounded-xl bg-[#2E2823] text-[#DE6438] flex items-center justify-center font-mono font-bold text-base">
                  ⚡
                </div>
                <div>
                  <div className="flex items-center space-x-2">
                    <h3 className="font-bold text-base text-ink font-mono">{selectedStepDetail.name}</h3>
                    <span className="text-xs bg-muted text-ink-3 font-mono px-2 py-0.5 rounded font-bold">
                      {selectedStepDetail.agent}
                    </span>
                  </div>
                  <p className="text-xs text-ink-3 font-mono">
                    {t('drawer.meta', { id: selectedStepDetail.trace?.decisionId || '--', latency: selectedStepDetail.latency !== null ? `${selectedStepDetail.latency}ms` : '--' })}
                  </p>
                </div>
              </div>

              <button 
                onClick={() => setSelectedStepDetail(null)}
                aria-label={t('drawer.close')}
                className="w-8 h-8 rounded-full bg-muted hover:bg-muted-strong text-ink-2 flex items-center justify-center text-sm font-bold transition"
              >
                ✕
              </button>
            </div>

            {/* 核心内容区 */}
            <div className="p-5 overflow-y-auto space-y-4 text-xs font-mono">
              
              {/* 1. 提交给大模型的真实系统提示词与输入上下文 */}
              <div className="bg-surface p-4 rounded-xl border border-line space-y-2 shadow-xs">
                <div className="flex items-center justify-between text-ink font-bold text-xs">
                  <span className="flex items-center space-x-1.5 text-info">
                    <span>📥</span>
                    <span>{t('drawer.promptTitle')}</span>
                  </span>
                  <span className="text-xs bg-info-soft text-info px-2 py-0.5 rounded">
                    Tokens: {selectedStepDetail.tokensUsed ?? '--'}
                  </span>
                </div>
                
                <div className="bg-[#241F1C] text-[#D8CEBE] p-3 rounded-lg overflow-x-auto text-xs leading-relaxed max-h-48 border border-[#3A332C]">
                  <span className="text-[#8C8071] block pb-1 border-b border-[#3A332C] mb-2 font-bold">
                    [Prompt]
                  </span>
                  {selectedStepDetail.prompt ? (
                    <pre className="whitespace-pre-wrap font-mono">{selectedStepDetail.prompt}</pre>
                  ) : (
                    <div className="text-[#9E917F]">
                      {t('drawer.noPrompt')}
                      {selectedStepDetail.inputSummary && (
                        <div className="mt-2 text-[#D8CEBE]">{t('drawer.inputSummary', { summary: selectedStepDetail.inputSummary })}</div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* 2. 大模型返回的结构化推理结果 */}
              <div className="bg-surface p-4 rounded-xl border border-line space-y-2 shadow-xs">
                <div className="flex items-center justify-between text-ink font-bold text-xs">
                  <span className="flex items-center space-x-1.5 text-success">
                    <span>📤</span>
                    <span>{t('drawer.responseTitle')}</span>
                  </span>
                  <span className="text-xs bg-success-soft text-success px-2 py-0.5 rounded font-bold">
                    Status: {selectedStepDetail.status}
                  </span>
                </div>

                <div className="bg-[#241F1C] text-[#62C554] p-3 rounded-lg overflow-x-auto text-xs leading-relaxed max-h-52 border border-[#3A332C]">
                  {selectedStepDetail.realOutput ? (
                    <pre className="whitespace-pre-wrap font-mono">
                      {JSON.stringify(selectedStepDetail.realOutput, null, 2)}
                    </pre>
                  ) : selectedStepDetail.llmResponse ? (
                    <pre className="whitespace-pre-wrap font-mono">{selectedStepDetail.llmResponse}</pre>
                  ) : selectedStepDetail.outputSummary ? (
                    <div className="text-[#D8CEBE]">{t('drawer.outputSummary', { summary: selectedStepDetail.outputSummary })}</div>
                  ) : (
                    <div className="text-ink-4">{t('drawer.noOutput')}</div>
                  )}
                </div>
              </div>

              {/* 3. 运行指标与检查点 */}
              <div className="p-3 bg-canvas rounded-xl border border-line-strong flex items-center justify-between text-xs text-ink-3">
                <span>{t('drawer.fallback', { value: selectedStepDetail.fallback ? t('drawer.yes') : t('drawer.no') })}</span>
                <span>{t('drawer.error', { message: selectedStepDetail.errorMessage || t('drawer.none') })}</span>
                <span>{t('drawer.idempotencyKey')} <code className="bg-surface px-1.5 py-0.5 rounded border border-line-strong">{selectedStepDetail.trace?.idempotencyKey || '--'}</code></span>
              </div>

            </div>

            {/* 底栏 */}
            <div className="p-3 bg-surface border-t border-line flex items-center justify-between">
              <span className="text-xs text-ink-4">
                {t('drawer.dataSource')}
              </span>
              <button 
                onClick={() => setSelectedStepDetail(null)}
                className="px-4 py-1.5 rounded-xl bg-brand hover:bg-brand-hover text-white text-xs font-semibold shadow-xs transition"
              >
                {t('drawer.done')}
              </button>
            </div>

          </div>
        </div>
      )}

    </div>
  );
}
