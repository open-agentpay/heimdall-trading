/**
 * 把后端一轮决策记录（decision record）映射到 9 个席位的展示数据。
 * 纯函数：不读写组件状态，只返回新的席位数组；只展示后端真实返回的字段，缺失数值一律显示 “--”
 * （不用写死的默认值替代，如默认情绪得分、默认巨鲸吸筹、写死的置信度与盈亏比、风控默认放行、伪造的耗时等）。
 */
import { numOrNull, fmtNum, fmtUsd, fmtPct } from './agentRoles';

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
      const summary = b.summary || b.trend_summary || '本轮未返回技术面摘要';
      const rsiLabel = rsi === null ? '' : rsi > 52 ? ' (多头动能运行)' : rsi < 48 ? ' (空头弱势运行)' : ' (中性震荡整理)';

      return {
        ...ag,
        pending: false,
        stance,
        tag: `${regime} · 阻力 ${fmtUsd(resistance)}`,
        badge: `RSI ${fmtNum(rsi, 1)}`,
        confidence: numOrNull(b.confidence),
        keyThesis: summary,
        evidence: [
          `EMA 动态均线: EMA20=${fmtUsd(inds.ema20)} · EMA50=${fmtUsd(inds.ema50)}`,
          `RSI-14 动力学指标: ${fmtNum(rsi, 1)}${rsiLabel}`,
          `结构通道: 支撑位 ${fmtUsd(support)} · 阻力位 ${fmtUsd(resistance)}`,
          `资金与价差: 年化资金费率 ${fmtPct(inds.annualized_funding, 2)} · Bifrost 价差 ${fmtPct(inds.spread_pct, 3)}`,
        ],
        invalidation: (support !== null || resistance !== null)
          ? `若 15m 收盘价跌破支撑位 (${fmtUsd(support)}) 或向上有效突破阻力位 (${fmtUsd(resistance)})，则当前 [${regime}] 状态定性失效并需重新校准。`
          : '本轮未返回支撑 / 阻力位',
        thinkingMonologue: `[技术面量化] ${summary}`,
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
      const whaleShort = whale === 'ACCUMULATION' ? '吸筹' : whale === 'DISTRIBUTION' ? '抛售' : whale ? '观望' : '--';
      const whaleLong = whale === 'ACCUMULATION'
        ? '链上大额提现锁仓 (吸筹)'
        : whale === 'DISTRIBUTION'
        ? '交易所持续集中充值 (抛售隐患)'
        : whale ? '资金面相对平衡 (观望)' : '未返回';

      return {
        ...ag,
        pending: false,
        stance,
        tag: `情绪 ${score === null ? '--' : score >= 0 ? '偏多' : '偏空'} · 巨鲸 ${whaleShort}`,
        badge: `得分 ${scoreTxt}`,
        confidence: score === null ? null : Math.min(1.0, 0.6 + Math.abs(score) * 0.4),
        keyThesis: s.summary || (score === null ? '本轮未返回情绪得分' : `综合情绪打分 ${scoreTxt}，巨鲸链上状态 ${whale || '未知'}。`),
        evidence: [
          `巨鲸行为定性: ${whaleLong}`,
          `情绪综合得分: ${scoreTxt} (恐慌贪婪指数: ${fgi === null ? '--' : fgi})`,
          `持仓拥挤度: ${s.crowding_risk || '--'}`,
        ],
        invalidation: '若链上出现异动转向相反方向（例如巨鲸集中向交易所大额充币），则情绪定性推翻。',
        thinkingMonologue: `[情绪与巨鲸监控] ${s.summary || '本轮未返回情绪摘要'}`,
        latency: traceLatency(findTrace('sentiment', '情绪')),
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
      const thesis = th.key_thesis || (isBull ? '本轮未返回多头论点' : '本轮未返回空头论点');

      return {
        ...ag,
        pending: false,
        stance: isBull ? 'LONG' : 'SHORT',
        tag: isBull ? `目标 ${fmtUsd(target)} · 防守 ${fmtUsd(inval)}` : `防守 ${fmtUsd(target)} · 失效 ${fmtUsd(inval)}`,
        badge: `置信 ${fmtPct(conf, 0)}`,
        confidence: conf,
        keyThesis: thesis,
        evidence: Array.isArray(th.evidence) && th.evidence.length > 0 ? th.evidence : [
          isBull ? `做多目标止盈价格: ${fmtUsd(target)}` : `做空下行回踩目标: ${fmtUsd(target)}`,
          isBull ? `立论失效防守止损位: ${fmtUsd(inval)}` : `阻力突破失效点位: ${fmtUsd(inval)}`,
          `${isBull ? '多头' : '空头'}置信度评分: ${fmtPct(conf, 0)}`,
        ],
        invalidation: inval === null
          ? '本轮未返回失效价位'
          : isBull ? `价格跌破 ${fmtUsd(inval)} 时立论失效并触发止损` : `价格突破 ${fmtUsd(inval)} 时空头立论失效`,
        thinkingMonologue: isBull
          ? `[多头真实辩护] ${thesis}${target !== null ? ` (目标: ${fmtUsd(target)})` : ''}`
          : `[空头真实质询] ${thesis}`,
        latency: traceLatency(isBull ? findTrace('bull', '多头', 'debate') : findTrace('bear', '空头', 'debate')),
        traceId: record.execution_id || `trc_${ag.id}_${ts}`,
      };
    }

    // 5. 裁判投资总监 (Moderator)
    if (ag.id === "moderator" && record.debate_synthesis) {
      const mo = record.debate_synthesis;
      const action = mo.consensus_action || 'UNKNOWN';
      const conf = numOrNull(mo.final_confidence);
      const verdict = mo.verdict || '本轮未返回裁决说明';
      const authorized = action === 'LONG' || action === 'SHORT';

      return {
        ...ag,
        pending: false,
        stance: action,
        tag: `裁决: ${action}`,
        badge: `置信 ${fmtPct(conf, 0)}`,
        confidence: conf,
        keyThesis: verdict,
        evidence: [
          `多空对抗裁定方向: ${action}`,
          `投资总监终审置信度: ${fmtPct(conf, 0)}`,
          ...(mo.risk_warning ? [`风险预警提示: ${mo.risk_warning}`] : []),
          `执行授权状态: ${authorized ? '准许策略层签发交易提案' : '中立观望，不签发交易提案'}`,
        ],
        invalidation: '若市场在发单前出现结构性反向跳水，裁判即刻撤回本轮裁决授权。',
        thinkingMonologue: `[裁判终审结论] ${verdict}`,
        latency: traceLatency(findTrace('moderator', '裁判')),
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
          tag: `${act.order_type || '委托'} ${fmtUsd(entry)} · 止损 ${fmtUsd(sl)}`,
          badge: `仓位 ${fmtUsd(act.size_in_usd)}`,
          confidence: null,
          keyThesis: '基于多空对决裁决签发确定性交易提案（含幂等键）',
          evidence: [
            `委托类型: ${act.order_type || '--'} | 方向: ${act.order_side || '--'} (${stance})`,
            `委托价格: ${fmtUsd(entry)}`,
            `确定性止损防线: ${fmtUsd(sl)} · 止盈目标: ${fmtUsd(tp)}`,
            `配置资金敞口: ${fmtUsd(act.size_in_usd)} · 杠杆倍数: ${lev === null ? '--' : `${lev}x`}`,
            `幂等键: ${p.idempotency_key ? `${p.idempotency_key.slice(0, 22)}...` : '--'}`,
          ],
          invalidation: '若挂单超时未撮合成交，或发单前价格已穿透止损线，该委托提案即刻作废。',
          thinkingMonologue: `[策略师提案] 方向: ${act.order_side || '--'}，委托价格: ${fmtUsd(entry)}，盈亏比: ${rr === null ? '--' : `${rr.toFixed(2)}:1`}`,
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
        tag: action === 'NEUTRAL' ? '空仓防守 · 暂无挂单' : '等待信号 · 暂无提案',
        badge: action === 'NEUTRAL' ? '防守观望' : '待命',
        confidence: null,
        keyThesis: `裁判终审定性为 ${action}，本轮策略层未签发任何买卖委托单。`,
        evidence: [
          `终审裁决: ${action}`,
          '本轮未签发交易提案',
        ],
        invalidation: '下一轮若多空博弈形成高置信度突破并获裁判授权，策略师将立即计算入场价与止损。',
        thinkingMonologue: `[策略精算师] 裁判裁决为 ${action}，本轮不签发委托。`,
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
        tag: !decided ? '风控结果未知' : passed ? '风控通过 ✓' : '风控驳回 ✗',
        badge: !decided ? '--' : passed ? '已放行' : '已阻断',
        confidence: !decided ? null : passed ? 1.0 : 0.0,
        keyThesis: !decided
          ? '本轮未返回风控结论'
          : passed
          ? '本轮硬风控规则全部通过，准许向执行层下发'
          : `硬风控拦截: ${reason || '触发风控红线'}`,
        evidence: Object.keys(ruleDetails).length > 0
          ? Object.entries(ruleDetails).map(([k, v]) => `规则 [${k}]: ${v ? '通过 (PASS)' : '违规 (FAIL)'}`)
          : ['本轮未返回逐条规则明细'],
        invalidation: '任何单项硬指标只要超出安全阈值，立即触发一票否决，绝对无条件阻断下单。',
        thinkingMonologue: `[风控官审查] 审核结果: ${!decided ? 'UNKNOWN' : passed ? 'PASSED' : 'REJECTED'}${reason ? ` | 触发规则: ${reason}` : ''}`,
        latency: traceLatency(findTrace('risk', 'guard')),
        traceId: `guard_audit_${ts}`,
      };
    }
    return ag;
  });
}
