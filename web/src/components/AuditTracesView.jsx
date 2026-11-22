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
import { getBotDecisions } from '../api/endpoints';
import { numOrNull } from '../data/agentRoles';

export default function AuditTracesView({ activeBotId = "bot_eth_debate_01" }) {
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
    const verdict = synthesis.verdict || findLog('Moderator', 'Verdict', '裁判') || null;
    const bullThesis = bull.key_thesis || findLog('Bull', '多头') || null;
    const bearThesis = bear.key_thesis || findLog('Bear', '空头') || null;
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
    <div className="flex-1 flex flex-col bg-[#F6F2EB] overflow-hidden">
      
      {/* 顶部控制栏 */}
      <div className="h-14 bg-[#FAF7F2] border-b border-[#E6DFD5] px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-[#EFE9DF] border border-[#DFD6C7] flex items-center justify-center text-[#DE6438]">
            <History className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-[#2E2823] flex items-center space-x-2">
              <span>决策痕迹溯源与审计轨迹 (Audit & Observability Traces)</span>
              <span className="text-xs font-mono bg-[#EFE9DF] text-[#7E7469] border border-[#DFD6C7] px-2 py-0.5 rounded-full font-semibold">
                数据来自后端决策记录
              </span>
            </h1>
            <p className="text-xs text-[#7E7469]">
              全链路记录多 Agent 辩论思维链 (COT)、StepTrace 耗时、幂等键与双轨硬风控一票否决依据
            </p>
          </div>
        </div>

        {/* 筛选与刷新 */}
        <div className="flex items-center space-x-2">
          <div className="flex bg-[#EFE9DF] p-0.5 rounded-lg border border-[#DFD6C7] text-xs">
            {['ALL', 'LONG', 'SHORT', 'NEUTRAL'].map((stance) => (
              <button
                key={stance}
                onClick={() => setFilterStance(stance)}
                className={`px-2.5 py-1 rounded-md font-semibold transition ${
                  filterStance === stance
                    ? "bg-white text-[#2E2823] shadow-xs"
                    : "text-[#7E7469] hover:text-[#2E2823]"
                }`}
              >
                {stance === 'ALL' ? '全部方向' : stance}
              </button>
            ))}
          </div>

          <button 
            onClick={fetchTraces}
            disabled={loading}
            className="p-1.5 bg-white border border-[#D8CEBE] hover:border-[#DE6438] rounded-lg text-xs font-semibold text-[#5A5046] flex items-center space-x-1.5 shadow-xs transition"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
        </div>
      </div>

      {/* 痕迹时间轴列表 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {!loading && filtered.length === 0 && (
          <div className="p-8 text-center text-xs text-[#8C8275] bg-white rounded-xl border border-dashed border-[#DFD6C7]">
            {decisions.length === 0
              ? '该机器人暂无决策审计记录。完成首轮推演后，这里会显示真实的决策轨迹。'
              : '当前筛选条件下没有记录。'}
          </div>
        )}
        {filtered.map((item) => {
          const isExpanded = expandedTraceId === item.rowKey;
          const isApproved = item.status === "COMPLETED";
          const isHalted = item.status === "HALTED";
          const isRejected = item.status === "REJECTED";
          const statusStyle = isApproved
            ? 'bg-[#EBF5EE] text-[#3D784E] border-[#D1E8D7]'
            : isRejected
            ? 'bg-[#FDECEC] text-[#C24D4D] border-[#F7CDCD]'
            : 'bg-[#F5F2EB] text-[#7A6F62] border-[#E3DDD1]';
          const statusText = isApproved ? '风控通过' : isRejected ? '风控拦截' : isHalted ? '本轮无提案' : '结论未知';

          return (
            <div 
              key={item.rowKey}
              className="bg-white rounded-xl border border-[#E6DFD5] shadow-xs overflow-hidden transition hover:border-[#D1C6B4]"
            >
              {/* 卡片摘要首行 */}
              <div 
                onClick={() => setExpandedTraceId(isExpanded ? null : item.rowKey)}
                className="p-4 flex items-center justify-between cursor-pointer hover:bg-[#FAF8F5] transition"
              >
                <div className="flex items-center space-x-4">
                  {/* 方向图标 */}
                  <div className={`w-9 h-9 rounded-xl flex items-center justify-center font-bold text-base border ${
                    item.action === 'LONG' 
                      ? 'bg-[#EBF5EE] text-[#3D784E] border-[#D1E8D7]' 
                      : item.action === 'SHORT'
                      ? 'bg-[#FBEFEA] text-[#DE6438] border-[#F2D6CA]'
                      : 'bg-[#F2EDE4] text-[#7A6F62] border-[#DFD6C7]'
                  }`}>
                    {item.action === 'LONG' ? <ArrowUpRight className="w-5 h-5" /> : item.action === 'SHORT' ? <ArrowDownRight className="w-5 h-5" /> : <Minus className="w-5 h-5" />}
                  </div>

                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-bold text-[#2E2823] font-mono">{item.symbol}</span>
                      {item.timeframe && (
                        <span className="text-xs bg-[#EFE9DF] text-[#6E6458] px-1.5 py-0.5 rounded font-mono font-medium">{item.timeframe}</span>
                      )}
                      <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${
                        item.action === 'LONG'
                          ? 'bg-[#EBF5EE] text-[#3D784E] border-[#D1E8D7]'
                          : item.action === 'SHORT'
                          ? 'bg-[#FBEFEA] text-[#DE6438] border-[#F2D6CA]'
                          : 'bg-[#F5F2EB] text-[#7A6F62] border-[#E3DDD1]'
                      }`}>
                        裁决: {item.action || '--'}{item.confidence !== null ? ` (置信度 ${Math.round(item.confidence * 100)}%)` : ''}
                      </span>
                      {item.regime && <span className="text-xs text-[#9E9486] font-mono">[{item.regime}]</span>}
                    </div>

                    <div className="flex items-center space-x-3 text-xs text-[#7E7469] mt-1 font-mono">
                      <span>决策 ID: {item.decisionId || '--'}</span>
                      <span>时间: {item.timestamp}</span>
                      <span>耗时: {item.totalLatencyMs !== null ? `${item.totalLatencyMs}ms` : '--'}</span>
                    </div>
                  </div>
                </div>

                {/* 状态指示与折叠箭头 */}
                <div className="flex items-center space-x-3">
                  <div className={`px-2.5 py-1 rounded-full text-xs font-bold border flex items-center space-x-1.5 ${statusStyle}`}>
                    {isApproved ? <CheckCircle2 className="w-3.5 h-3.5" /> : isRejected ? <XCircle className="w-3.5 h-3.5" /> : <Minus className="w-3.5 h-3.5" />}
                    <span>{statusText}</span>
                  </div>

                  <div className="text-[#9E9486]">
                    {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </div>
                </div>
              </div>

              {/* 展开的深度细节 */}
              {isExpanded && (
                <div className="px-6 py-4 bg-[#FAF7F2] border-t border-[#E6DFD5] space-y-4 text-xs animate-in fade-in duration-100">
                  
                  {/* 1. 裁判裁决书与幂等控制 */}
                  <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] space-y-2">
                    <div className="flex items-center justify-between text-[#2E2823] font-bold">
                      <span className="flex items-center space-x-1.5">
                        <span>⚖️</span>
                        <span>Debate Moderator 投资总监终审裁决</span>
                      </span>
                      <span className="font-mono text-xs text-[#7E7469] bg-[#EFE9DF] px-2 py-0.5 rounded border border-[#DFD6C7]">
                        IdempotencyKey: {item.idempotencyKey || '--'}
                      </span>
                    </div>
                    <p className="text-[#4E443B] leading-relaxed bg-[#FAF8F5] p-2.5 rounded-lg border border-[#F0EBE1]">
                      {item.verdict || '本轮未返回裁决说明'}
                    </p>
                  </div>

                  {/* 2. 多空辩论两派立论对照 */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="bg-white p-3.5 rounded-xl border border-[#D1E8D7] space-y-1.5">
                      <div className="font-bold text-[#3D784E] flex items-center space-x-1.5">
                        <span>🐂</span>
                        <span>多头研究员 (Bull Researcher)</span>
                      </div>
                      <p className="text-[#55695B] leading-relaxed text-xs">
                        {item.bullThesis || '本轮未返回多头论点'}
                      </p>
                    </div>

                    <div className="bg-white p-3.5 rounded-xl border border-[#F2D6CA] space-y-1.5">
                      <div className="font-bold text-[#DE6438] flex items-center space-x-1.5">
                        <span>🐻</span>
                        <span>空头研究员 (Bear Researcher)</span>
                      </div>
                      <p className="text-[#7A5447] leading-relaxed text-xs">
                        {item.bearThesis || '本轮未返回空头论点'}
                      </p>
                    </div>
                  </div>

                  {/* 3. 风控核验规则清单 */}
                  <div className="bg-white p-3.5 rounded-xl border border-[#E6DFD5] space-y-2">
                    <div className="font-bold text-[#2E2823] flex items-center justify-between">
                      <span className="flex items-center space-x-1.5">
                        <ShieldCheck className="w-4 h-4 text-[#DE6438]" />
                        <span>双轨风控核验审计 (Risk Enforcement Check)</span>
                      </span>
                      {item.rejectionReason && (
                        <span className="text-[#C24D4D] font-medium text-xs">
                          拦截原因: {item.rejectionReason}
                        </span>
                      )}
                    </div>

                    {Object.keys(item.checkedRules || {}).length === 0 && (
                      <div className="text-xs text-[#9E917F]">本轮未返回逐条规则明细</div>
                    )}
                    <div className="grid grid-cols-3 gap-2 pt-1 font-mono text-xs">
                      {item.checkedRules && Object.entries(item.checkedRules).map(([ruleName, passed]) => (
                        <div 
                          key={ruleName}
                          className={`p-2 rounded-lg border flex items-center justify-between ${
                            passed 
                              ? 'bg-[#EBF5EE] border-[#D1E8D7] text-[#3D784E]' 
                              : 'bg-[#FDECEC] border-[#F7CDCD] text-[#C24D4D]'
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
                    <div className="p-3 rounded-xl border border-dashed border-[#DFD6C7] bg-white text-xs text-[#9E917F]">
                      该记录没有 StepTrace 明细（数据库历史记录只保存思维链、提案与风控结论）。
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
                            title="点击展开查看向大模型提交的真实提示词与原始输出"
                          >
                            <span className="text-[#DFD6C7] flex items-center space-x-2">
                              <span className="text-[#8E8476]">{idx + 1}.</span>
                              <span className="group-hover:text-[#DE6438] font-medium">{st.name}</span>
                              <span className="text-[#7A6F62] text-xs">({st.agent})</span>
                              <span className="text-xs text-[#A69784] opacity-0 group-hover:opacity-100 transition flex items-center space-x-1">
                                <span>🔍 审查 Prompt</span>
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
            className="w-full max-w-3xl bg-[#FAF7F2] border-2 border-[#D8CEBE] rounded-3xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 顶栏 */}
            <div className="p-4 bg-white border-b border-[#EBE3D7] flex items-center justify-between">
              <div className="flex items-center space-x-3">
                <div className="w-10 h-10 rounded-xl bg-[#2E2823] text-[#DE6438] flex items-center justify-center font-mono font-bold text-base">
                  ⚡
                </div>
                <div>
                  <div className="flex items-center space-x-2">
                    <h3 className="font-bold text-base text-[#2E2823] font-mono">{selectedStepDetail.name}</h3>
                    <span className="text-xs bg-[#EFE9DF] text-[#7E7469] font-mono px-2 py-0.5 rounded font-bold">
                      {selectedStepDetail.agent}
                    </span>
                  </div>
                  <p className="text-xs text-[#7E7469] font-mono">
                    决策 ID: {selectedStepDetail.trace?.decisionId || '--'} · 耗时: {selectedStepDetail.latency !== null ? `${selectedStepDetail.latency}ms` : '--'}
                  </p>
                </div>
              </div>

              <button 
                onClick={() => setSelectedStepDetail(null)}
                className="w-8 h-8 rounded-full bg-[#EFE9DF] hover:bg-[#E5DACB] text-[#5E554B] flex items-center justify-center text-sm font-bold transition"
              >
                ✕
              </button>
            </div>

            {/* 核心内容区 */}
            <div className="p-5 overflow-y-auto space-y-4 text-xs font-mono">
              
              {/* 1. 提交给大模型的真实系统提示词与输入上下文 */}
              <div className="bg-white p-4 rounded-xl border border-[#E6DFD5] space-y-2 shadow-xs">
                <div className="flex items-center justify-between text-[#2E2823] font-bold text-xs">
                  <span className="flex items-center space-x-1.5 text-[#3E6B89]">
                    <span>📥</span>
                    <span>提交给 LLM 的真实提示词与行情入参 (Prompt & Context)</span>
                  </span>
                  <span className="text-xs bg-[#E9F2F7] text-[#3E6B89] px-2 py-0.5 rounded">
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
                      该步骤未记录完整提示词（可能未调用大模型，或使用了本地算法降级）。
                      {selectedStepDetail.inputSummary && (
                        <div className="mt-2 text-[#D8CEBE]">输入摘要: {selectedStepDetail.inputSummary}</div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* 2. 大模型返回的结构化推理结果 */}
              <div className="bg-white p-4 rounded-xl border border-[#E6DFD5] space-y-2 shadow-xs">
                <div className="flex items-center justify-between text-[#2E2823] font-bold text-xs">
                  <span className="flex items-center space-x-1.5 text-[#4D835C]">
                    <span>📤</span>
                    <span>大模型处理输出 (LLM Response & Feature Schema)</span>
                  </span>
                  <span className="text-xs bg-[#EBF5EE] text-[#4D835C] px-2 py-0.5 rounded font-bold">
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
                    <div className="text-[#D8CEBE]">输出摘要: {selectedStepDetail.outputSummary}</div>
                  ) : (
                    <div className="text-[#9E917F]">该步骤未记录输出。</div>
                  )}
                </div>
              </div>

              {/* 3. 运行指标与检查点 */}
              <div className="p-3 bg-[#FAF4EB] rounded-xl border border-[#DFD6C7] flex items-center justify-between text-xs text-[#7E7469]">
                <span>本地降级: {selectedStepDetail.fallback ? '是' : '否'}</span>
                <span>错误信息: {selectedStepDetail.errorMessage || '无'}</span>
                <span>幂等键: <code className="bg-white px-1.5 py-0.5 rounded border border-[#DFD6C7]">{selectedStepDetail.trace?.idempotencyKey || '--'}</code></span>
              </div>

            </div>

            {/* 底栏 */}
            <div className="p-3 bg-white border-t border-[#EBE3D7] flex items-center justify-between">
              <span className="text-xs text-[#9E917F]">
                数据来源: 后端 StepTrace 记录
              </span>
              <button 
                onClick={() => setSelectedStepDetail(null)}
                className="px-4 py-1.5 rounded-xl bg-[#DE6438] hover:bg-[#C8542A] text-white text-xs font-semibold shadow-xs transition"
              >
                完成阅知
              </button>
            </div>

          </div>
        </div>
      )}

    </div>
  );
}
