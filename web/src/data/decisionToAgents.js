/**
 * 把后端一轮决策记录（decision record）映射到 9 个席位的展示数据。
 * 纯函数：不读写组件状态，只返回新的席位数组；只展示后端真实返回的字段，缺失数值一律显示 “--”
 * （不用写死的默认值替代，如默认情绪得分、默认巨鲸吸筹、写死的置信度与盈亏比、风控默认放行、伪造的耗时等）。
 */
import i18n from '../i18n';
import { numOrNull, fmtNum, fmtUsd, fmtPct } from './agentRoles';

// 前端拼出来的文字按调用时的语言翻译（语言包 agents:*）；后端返回的摘要、论点、证据原样显示

export function applyDecisionToAgents(prevAgents, record) {
  if (!record || record.error) return prevAgents;

  const ts = (record.timestamp || '').slice(-6);
  const findTrace = (...keys) => (record.step_traces || []).find((st) => {
    const name = `${st.agent_role || ''} ${st.step_name || ''} ${st.agent_id || ''}`.toLowerCase();
    return keys.some((k) => name.includes(k));
  }) || null;
  const traceLatency = (trace) => {
    const ms = numOrNull(trace?.latency_ms ?? trace?.duration_ms);
    return ms === null ? '--' : `${Math.round(ms)}ms`;
  };

  return prevAgents.map(ag => {
    // 1. 技术面量化分析师 (Analyst)
    if (ag.id === "analyst" && (record.analyst_brief || record.trade_proposal)) {
      const b = record.analyst_brief || {};
      const inds = b.indicators || {};
      const rsi = numOrNull(inds.rsi14 ?? b.rsi14 ?? b.rsi_14);
      const support = numOrNull(b.support ?? b.key_support);
      const resistance = numOrNull(b.resistance ?? b.key_resistance);
      const regime = b.regime || 'UNKNOWN';
      const stance = regime === 'TREND_UP' ? 'LONG' : regime === 'TREND_DOWN' ? 'SHORT' : 'NEUTRAL';
      const summary = b.summary || b.trend_summary || i18n.t('agents:analyst.noSummary');
      const rsiLabel = rsi === null ? '' : rsi > 52 ? i18n.t('agents:analyst.rsiBull') : rsi < 48 ? i18n.t('agents:analyst.rsiBear') : i18n.t('agents:analyst.rsiNeutral');

      return {
        ...ag,
        pending: false,
        stance,
        tag: i18n.t('agents:analyst.tag', { regime, resistance: fmtUsd(resistance) }),
        badge: `RSI ${fmtNum(rsi, 1)}`,
        confidence: numOrNull(b.confidence),
        keyThesis: summary,
        evidence: [
          i18n.t('agents:analyst.evidenceEma', { ema20: fmtUsd(inds.ema20), ema50: fmtUsd(inds.ema50) }),
          i18n.t('agents:analyst.evidenceRsi', { rsi: fmtNum(rsi, 1), label: rsiLabel }),
          i18n.t('agents:analyst.evidenceLevels', { support: fmtUsd(support), resistance: fmtUsd(resistance) }),
          i18n.t('agents:analyst.evidenceFunding', { funding: fmtPct(inds.annualized_funding, 2), spread: fmtPct(inds.spread_pct, 3) }),
        ],
        invalidation: (support !== null || resistance !== null)
          ? i18n.t('agents:analyst.invalidation', { support: fmtUsd(support), resistance: fmtUsd(resistance), regime })
          : i18n.t('agents:analyst.noLevels'),
        thinkingMonologue: i18n.t('agents:analyst.monologue', { summary }),
        latency: traceLatency(findTrace('analyst')),
        traceId: record.execution_id || record.idempotency_key || `trc_analyst_${ts}`,
      };
    }

    // 2. 情绪与链上巨鲸分析师 (Sentiment)
    if (ag.id === "sentiment" && (record.sentiment_report || record.sentiment)) {
      const s = record.sentiment_report || record.sentiment || {};
      const score = numOrNull(s.sentiment_score);
      const whale = s.whale_activity || null;
      const fgi = numOrNull(s.fear_greed_score);
      const stance = score === null ? 'NEUTRAL' : score >= 0.1 ? 'LONG' : score <= -0.1 ? 'SHORT' : 'NEUTRAL';
      const scoreTxt = score === null ? '--' : `${score > 0 ? '+' : ''}${score.toFixed(2)}`;
      const whaleShort = whale === 'ACCUMULATION' ? i18n.t('agents:sentiment.whaleShortAccumulation') : whale === 'DISTRIBUTION' ? i18n.t('agents:sentiment.whaleShortDistribution') : whale ? i18n.t('agents:sentiment.whaleShortNeutral') : '--';
      const whaleLong = whale === 'ACCUMULATION'
        ? i18n.t('agents:sentiment.whaleLongAccumulation')
        : whale === 'DISTRIBUTION'
        ? i18n.t('agents:sentiment.whaleLongDistribution')
        : whale ? i18n.t('agents:sentiment.whaleLongNeutral') : i18n.t('agents:sentiment.whaleLongMissing');

      return {
        ...ag,
        pending: false,
        stance,
        tag: i18n.t('agents:sentiment.tag', { bias: score === null ? '--' : score >= 0 ? i18n.t('agents:sentiment.biasBull') : i18n.t('agents:sentiment.biasBear'), whale: whaleShort }),
        badge: i18n.t('agents:sentiment.badge', { score: scoreTxt }),
        confidence: score === null ? null : Math.min(1.0, 0.6 + Math.abs(score) * 0.4),
        keyThesis: s.summary || (score === null ? i18n.t('agents:sentiment.noScore') : i18n.t('agents:sentiment.thesis', { score: scoreTxt, whale: whale || i18n.t('agents:sentiment.unknown') })),
        evidence: [
          i18n.t('agents:sentiment.evidenceWhale', { value: whaleLong }),
          i18n.t('agents:sentiment.evidenceScore', { score: scoreTxt, fgi: fgi === null ? '--' : fgi }),
          i18n.t('agents:sentiment.evidenceCrowding', { value: s.crowding_risk || '--' }),
        ],
        invalidation: i18n.t('agents:sentiment.invalidation'),
        thinkingMonologue: i18n.t('agents:sentiment.monologue', { summary: s.summary || i18n.t('agents:sentiment.noSummary') }),
        latency: traceLatency(findTrace('sentiment', '情绪')), // i18n-ignore 匹配后端步骤名（中文）
        traceId: record.execution_id || `trc_sentiment_${ts}`,
      };
    }

    // 3 & 4. 多头研究员 (Bull) / 空头质询员 (Bear)
    if ((ag.id === "bull" && record.bull_thesis) || (ag.id === "bear" && record.bear_thesis)) {
      const isBull = ag.id === "bull";
      const th = isBull ? record.bull_thesis : record.bear_thesis;
      const conf = numOrNull(th.confidence);
      const target = numOrNull(th.target_price);
      const inval = numOrNull(th.invalidation_price);
      const thesis = th.key_thesis || (isBull ? i18n.t('agents:debate.noBullThesis') : i18n.t('agents:debate.noBearThesis'));

      return {
        ...ag,
        pending: false,
        stance: isBull ? 'LONG' : 'SHORT',
        tag: i18n.t(isBull ? 'agents:debate.tagBull' : 'agents:debate.tagBear', { target: fmtUsd(target), inval: fmtUsd(inval) }),
        badge: i18n.t('agents:common.confidenceBadge', { value: fmtPct(conf, 0) }),
        confidence: conf,
        keyThesis: thesis,
        evidence: Array.isArray(th.evidence) && th.evidence.length > 0 ? th.evidence : [
          i18n.t(isBull ? 'agents:debate.bullTarget' : 'agents:debate.bearTarget', { value: fmtUsd(target) }),
          i18n.t(isBull ? 'agents:debate.bullInval' : 'agents:debate.bearInval', { value: fmtUsd(inval) }),
          i18n.t(isBull ? 'agents:debate.bullConfidence' : 'agents:debate.bearConfidence', { value: fmtPct(conf, 0) }),
        ],
        invalidation: inval === null
          ? i18n.t('agents:debate.noInvalidation')
          : i18n.t(isBull ? 'agents:debate.invalidationBull' : 'agents:debate.invalidationBear', { value: fmtUsd(inval) }),
        thinkingMonologue: isBull
          ? i18n.t('agents:debate.monologueBull', { thesis, target: target !== null ? i18n.t('agents:debate.targetSuffix', { value: fmtUsd(target) }) : '' })
          : i18n.t('agents:debate.monologueBear', { thesis }),
        latency: traceLatency(isBull ? findTrace('bull', '多头', 'debate') : findTrace('bear', '空头', 'debate')), // i18n-ignore 匹配后端步骤名（中文）
        traceId: record.execution_id || `trc_${ag.id}_${ts}`,
      };
    }

    // 5. 裁判投资总监 (Moderator)
    if (ag.id === "moderator" && record.debate_synthesis) {
      const mo = record.debate_synthesis;
      const action = mo.consensus_action || 'UNKNOWN';
      const conf = numOrNull(mo.final_confidence);
      const verdict = mo.verdict || i18n.t('agents:moderator.noVerdict');
      const authorized = action === 'LONG' || action === 'SHORT';

      return {
        ...ag,
        pending: false,
        stance: action,
        tag: i18n.t('agents:moderator.tag', { action }),
        badge: i18n.t('agents:common.confidenceBadge', { value: fmtPct(conf, 0) }),
        confidence: conf,
        keyThesis: verdict,
        evidence: [
          i18n.t('agents:moderator.evidenceDirection', { action }),
          i18n.t('agents:moderator.evidenceConfidence', { value: fmtPct(conf, 0) }),
          ...(mo.risk_warning ? [i18n.t('agents:moderator.evidenceRiskWarning', { value: mo.risk_warning })] : []),
          i18n.t('agents:moderator.evidenceAuthorization', { status: authorized ? i18n.t('agents:moderator.authorized') : i18n.t('agents:moderator.notAuthorized') }),
        ],
        invalidation: i18n.t('agents:moderator.invalidation'),
        thinkingMonologue: i18n.t('agents:moderator.monologue', { verdict }),
        latency: traceLatency(findTrace('moderator', '裁判')), // i18n-ignore 匹配后端步骤名（中文）
        traceId: record.execution_id || `trc_moderator_${ts}`,
      };
    }

    // 6. 策略执行官 (Strategy)
    if (ag.id === "strategy") {
      const latency = traceLatency(findTrace('strategy'));
      if (record.trade_proposal) {
        const p = record.trade_proposal;
        const act = p.trade_action || {};
        const stance = act.position_side
          || (act.order_side === 'BUY' ? 'LONG' : act.order_side === 'SELL' ? 'SHORT' : 'UNKNOWN');
        const entry = numOrNull(act.target_price);
        const sl = numOrNull(act.stop_loss_price);
        const tp = numOrNull(act.take_profit_price);
        const lev = numOrNull(act.leverage);
        const rr = (entry !== null && sl !== null && tp !== null && Math.abs(entry - sl) > 0)
          ? Math.abs(tp - entry) / Math.abs(entry - sl)
          : null;

        return {
          ...ag,
          pending: false,
          stance,
          tag: i18n.t('agents:strategy.tag', { orderType: act.order_type || i18n.t('agents:strategy.orderTypeFallback'), entry: fmtUsd(entry), sl: fmtUsd(sl) }),
          badge: i18n.t('agents:strategy.badge', { value: fmtUsd(act.size_in_usd) }),
          confidence: null,
          keyThesis: i18n.t('agents:strategy.thesis'),
          evidence: [
            i18n.t('agents:strategy.evidenceOrder', { type: act.order_type || '--', side: act.order_side || '--', stance }),
            i18n.t('agents:strategy.evidencePrice', { value: fmtUsd(entry) }),
            i18n.t('agents:strategy.evidenceStops', { sl: fmtUsd(sl), tp: fmtUsd(tp) }),
            i18n.t('agents:strategy.evidenceSize', { size: fmtUsd(act.size_in_usd), leverage: lev === null ? '--' : `${lev}x` }),
            i18n.t('agents:strategy.evidenceIdempotency', { value: p.idempotency_key ? `${p.idempotency_key.slice(0, 22)}...` : '--' }),
          ],
          invalidation: i18n.t('agents:strategy.invalidation'),
          thinkingMonologue: i18n.t('agents:strategy.monologue', { side: act.order_side || '--', entry: fmtUsd(entry), rr: rr === null ? '--' : `${fmtNum(rr, 2)}:1` }),
          latency,
          traceId: p.idempotency_key || `trc_strategy_${ts}`,
        };
      }
      const mo = record.debate_synthesis || {};
      const action = mo.consensus_action || record.debate_consensus || 'NEUTRAL';
      return {
        ...ag,
        pending: false,
        stance: action === 'NEUTRAL' ? 'NEUTRAL' : 'WAIT',
        tag: action === 'NEUTRAL' ? i18n.t('agents:strategy.tagNeutral') : i18n.t('agents:strategy.tagWait'),
        badge: action === 'NEUTRAL' ? i18n.t('agents:strategy.badgeNeutral') : i18n.t('agents:strategy.badgeWait'),
        confidence: null,
        keyThesis: i18n.t('agents:strategy.thesisNoProposal', { action }),
        evidence: [
          i18n.t('agents:strategy.evidenceVerdict', { action }),
          i18n.t('agents:strategy.evidenceNoProposal'),
        ],
        invalidation: i18n.t('agents:strategy.invalidationNoProposal'),
        thinkingMonologue: i18n.t('agents:strategy.monologueNoProposal', { action }),
        latency,
        traceId: `trc_strategy_neutral_${ts}`,
      };
    }

    // 7. 软风控官 / 确定性硬风控 (Risk Officer / HardRiskGuard)
    if ((ag.id === "risk_officer" || ag.id === "hard_guard") && (record.risk_review || record.risk_evaluation)) {
      const re = record.risk_review || record.risk_evaluation;
      const approvedRaw = re.approved ?? re.passed;
      const decided = typeof approvedRaw === 'boolean';
      const passed = approvedRaw === true;
      const ruleDetails = re.checked_rules || re.rule_details || {};
      const reason = re.rejection_reason || re.rejection_rule;

      return {
        ...ag,
        pending: false,
        stance: !decided ? 'UNKNOWN' : passed ? 'LONG' : 'NEUTRAL',
        tag: !decided ? i18n.t('agents:risk.tagUnknown') : passed ? i18n.t('agents:risk.tagPassed') : i18n.t('agents:risk.tagRejected'),
        badge: !decided ? '--' : passed ? i18n.t('agents:risk.badgePassed') : i18n.t('agents:risk.badgeBlocked'),
        confidence: !decided ? null : passed ? 1.0 : 0.0,
        keyThesis: !decided
          ? i18n.t('agents:risk.thesisUnknown')
          : passed
          ? i18n.t('agents:risk.thesisPassed')
          : i18n.t('agents:risk.thesisRejected', { reason: reason || i18n.t('agents:risk.reasonFallback') }),
        evidence: Object.keys(ruleDetails).length > 0
          ? Object.entries(ruleDetails).map(([k, v]) => i18n.t('agents:risk.evidenceRule', { rule: k, result: v ? i18n.t('agents:risk.rulePass') : i18n.t('agents:risk.ruleFail') }))
          : [i18n.t('agents:risk.noRules')],
        invalidation: i18n.t('agents:risk.invalidation'),
        thinkingMonologue: i18n.t('agents:risk.monologue', { result: !decided ? 'UNKNOWN' : passed ? 'PASSED' : 'REJECTED', rule: reason ? i18n.t('agents:risk.ruleSuffix', { reason }) : '' }),
        latency: traceLatency(findTrace('risk', 'guard')),
        traceId: `guard_audit_${ts}`,
      };
    }
    return ag;
  });
}
