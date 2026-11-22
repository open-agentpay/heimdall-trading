/**
 * ================================================================================
 * Heimdall 交易平台 - 痕迹溯源与审计轨迹视图 (AuditTracesView)
 * ================================================================================
 * 
 * 核心功能：
 * 1. 完整展示历史任务时间轴（Task ID, Trace ID, 标的, 周期, 最终结论）；
 * 2. 深入审查每个决策的证据链：Analyst 研报、多空对抗辩论要点、裁判判词；
 * 3. 审查两道风控规则核验明细与物理硬拦截记录；
 * 4. 展示 StepTrace 耗时流水线（微秒级可观测性）与本地降级标记。
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
  const [expandedTraceId, setExpandedTraceId] = useState(null);
  const [selectedStepDetail, setSelectedStepDetail] = useState(null); // 查看指定 Step 的 Prompt 与 LLM 输出详情

  // 归一化后端决策记录：只使用后端真实字段，缺失就标记为缺失（null），绝不补默认结论。
  // 兼容两种来源：内存决策记录（含 step_traces / risk_review 等完整字段）与数据库历史记录
  // （仅含 thinking_logs、proposal、risk_approved、rejection_reason、market_regime）。
  // 不再内置演示用的假历史轨迹，也不再推断“非观望即放行”、补全“全部规则 PASS”或编造 42ms 等耗时。
  const normalizeDecision = (item, idx) => {
    if (!item) return null;
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
      timestamp: item.timestamp ? String(item.timestamp).replace('T', ' ').slice(0, 19) : '--',
      timestampRaw: item.timestamp ?? null,
    };
  };

  const fetchTraces = async () => {
    setLoading(true);
    try {
      const data = await getBotDecisions(activeBotId, 20);
      if (Array.isArray(data) && data.length > 0) {
        setDecisions(data.map((d, idx) => normalizeDecision(d, idx)).filter(Boolean));
      } else {
        setDecisions([]);
      }
    } catch (err) {
      console.warn('[AuditTracesView] 加载真实决策审计记录异常:', err);
      setDecisions([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTraces();
  }, [activeBotId]);

  const filtered = decisions.filter(item => {
    if (filterStance === 'ALL') return true;
    return item.action === filterStance;
  });

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      
      {/* 顶部控制栏 */}
      <div className="h-14 bg-panel border-b border-line px-6 flex items-center justify-between shrink-0">
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

        {/* 筛选与刷新 */}
        <div className="flex items-center space-x-2">
          <div className="flex bg-muted p-0.5 rounded-lg border border-line-strong text-xs">
            {['ALL', 'LONG', 'SHORT', 'NEUTRAL'].map((stance) => (
              <button
                key={stance}
                onClick={() => setFilterStance(stance)}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  filterStance === stance
                    ? "bg-surface text-ink shadow-xs"
                    : "text-ink-3 hover:text-ink"
                }`}
              >
                {stance === 'ALL' ? t('filter.all') : stance}
              </button>
            ))}
          </div>

          <button 
            onClick={fetchTraces}
            disabled={loading}
            className="p-1.5 bg-surface border border-line-strong hover:border-brand rounded-lg text-xs font-semibold text-ink-2 flex items-center space-x-1.5 shadow-xs transition"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>{t('common:refresh')}</span>
          </button>
        </div>
      </div>

      {/* 痕迹时间轴列表 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {!loading && filtered.length === 0 && (
          <div className="p-8 text-center text-xs text-ink-3 bg-surface rounded-xl border border-dashed border-line-strong">
            {decisions.length === 0
              ? t('empty.noRecords')
              : t('empty.noMatch')}
          </div>
        )}
        {filtered.map((item) => {
          const isExpanded = expandedTraceId === item.rowKey;
          const isApproved = item.status === "COMPLETED";
          const isHalted = item.status === "HALTED";
          const isRejected = item.status === "REJECTED";
          const statusStyle = isApproved
            ? 'bg-success-soft text-success border-success-line'
            : isRejected
            ? 'bg-danger-soft text-danger border-danger-line'
            : 'bg-canvas text-ink-3 border-line';
          const statusText = isApproved ? t('status.completed') : isRejected ? t('status.rejected') : isHalted ? t('status.halted') : t('status.unknown');

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
                    {isApproved ? <CheckCircle2 className="w-3.5 h-3.5" /> : isRejected ? <XCircle className="w-3.5 h-3.5" /> : <Minus className="w-3.5 h-3.5" />}
                    <span>{statusText}</span>
                  </div>

                  <div className="text-ink-4">
                    {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </div>
                </div>
              </div>

              {/* 展开的深度细节 */}
              {isExpanded && (
                <div className="px-6 py-4 bg-panel border-t border-line space-y-4 text-xs animate-in fade-in duration-100">
                  
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

                </div>
              )}

            </div>
          );
        })}
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
