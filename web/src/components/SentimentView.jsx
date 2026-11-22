/**
 * ================================================================================
 * Heimdall 交易平台 - 情绪与多源情报雷达视图 (SentimentView)
 * ================================================================================
 *
 * 【P1 真实性对齐】：
 * 彻底移除此前前端写死的“巨鲸净提现 -$12.5M / 稳定币增发 +$85M / 静态利好新闻”假数据，
 * 100% 绑定后端 `SentimentCollector` 输出的真实情绪评分、数据来源溯源 (provenance) 与新闻/异动字段；
 * 未接入付费链上数据源时诚实展示中性空态，避免向交易员传递虚假利好误导判断。
 */

import React from 'react';
import {
  Compass, Newspaper, TrendingUp, Activity, ShieldCheck
} from 'lucide-react';

export default function SentimentView({
  symbol = 'ETH',
  sentimentReport = null,
}) {
  const sym = (symbol || 'ETH').toUpperCase();
  const report = sentimentReport || {};

  const hasLiveReport = Boolean(sentimentReport && Object.keys(sentimentReport).length > 0);
  // 字段缺失时显示 “--”，不再把“没有数据”显示成 +0.00 中性 / NEUTRAL / BALANCED 这类看似真实的读数
  const sentimentScore = typeof report.sentiment_score === 'number' && Number.isFinite(report.sentiment_score) ? report.sentiment_score : null;
  const fearGreed = report.fear_greed_index ?? report.fear_greed_score ?? null;
  const whaleActivity = report.whale_activity || null;
  const crowdingRisk = report.crowding_risk || null;
  const provenance = report.provenance || report.data_source || (hasLiveReport ? 'live_collector' : 'awaiting_step');
  const summaryText = report.summary || '尚未收到最新一轮情绪感知报告，请在「对弈大厅」触发推演或等待后台巡检。';
  const newsItems = Array.isArray(report.news_items) ? report.news_items : (Array.isArray(report.headlines) ? report.headlines : []);
  const whaleTransfers = Array.isArray(report.whale_transfers) ? report.whale_transfers : [];

  const scoreLabel = sentimentScore === null ? '--' : sentimentScore >= 0.15 ? '偏多' : sentimentScore <= -0.15 ? '偏空' : '中性';
  const scoreColor = sentimentScore === null ? 'text-ink-4' : sentimentScore >= 0.15 ? 'text-success' : sentimentScore <= -0.15 ? 'text-danger' : 'text-ink-2';

  return (
    <div className="flex-1 flex flex-col bg-canvas overflow-hidden">
      {/* 顶部控制栏 */}
      <div className="h-14 bg-panel border-b border-line px-6 flex items-center justify-between shrink-0">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-muted border border-line-strong flex items-center justify-center text-brand">
            <Compass className="w-4 h-4" />
          </div>
          <div>
            <h1 className="text-sm font-bold text-ink flex items-center space-x-2">
              <span>{sym} 真实情绪与链上情报雷达 (Sentiment &amp; Provenance)</span>
              <span className="text-xs font-mono bg-brand-soft text-brand border border-brand-line px-2 py-0.5 rounded-full font-semibold">
                来源: {provenance}
              </span>
            </h1>
            <p className="text-xs text-ink-3">
              严格零编造原则：仅呈现后端真实采集的情绪指标与溯源情报，无外部数据源时保持中性兜底
            </p>
          </div>
        </div>
      </div>

      {/* 核心主看板 */}
      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* 摘要卡片 */}
        <div className="bg-surface p-4 rounded-xl border border-line shadow-xs flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <ShieldCheck className="w-5 h-5 text-success shrink-0" />
            <div className="text-xs text-ink-2 leading-relaxed">{summaryText}</div>
          </div>
          <span className="text-xs font-mono px-2 py-1 rounded bg-panel border border-line text-ink-3 shrink-0 ml-4">
            {hasLiveReport ? '已接入实时感知' : '等待首次推演'}
          </span>
        </div>

        {/* 1. 核心四大维度真实指标卡片 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>综合情绪打分</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{scoreLabel}</span>
            </div>
            <div className="flex items-baseline space-x-2">
              <span className={`text-3xl font-extrabold font-mono ${scoreColor}`}>
                {sentimentScore === null ? '--' : `${sentimentScore >= 0 ? '+' : ''}${sentimentScore.toFixed(2)}`}
              </span>
              <span className="text-xs text-ink-3">[-1.0 ~ +1.0]</span>
            </div>
            <p className="text-xs text-ink-3">
              由后端 SentimentCollector 结合真实费率/舆情计算
            </p>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>全网恐慌贪婪指数</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">FNG</span>
            </div>
            <div className="flex items-baseline space-x-2">
              <span className="text-3xl font-extrabold font-mono text-ink">
                {fearGreed !== null ? fearGreed : '--'}
              </span>
              <span className="text-xs text-ink-3">/ 100</span>
            </div>
            <div className="text-xs text-ink-3 flex items-center space-x-1">
              <TrendingUp className="w-3.5 h-3.5 shrink-0 text-success" />
              <span>{fearGreed !== null ? '来自实时恐慌贪婪数据源' : '未返回恐慌指数（已做中性降级）'}</span>
            </div>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{sym} 巨鲸动向定性</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{whaleActivity || '--'}</span>
            </div>
            <div className="text-xl font-extrabold font-mono text-ink pt-1">
              {whaleActivity === 'ACCUMULATION'
                ? '吸筹净流出'
                : whaleActivity === 'DISTRIBUTION'
                ? '派发净流入'
                : whaleActivity
                ? '中性观望'
                : '--'}
            </div>
            <p className="text-xs text-ink-3">
              无链上巨鲸 API 密钥时严格返回 NEUTRAL，绝不伪造净流出
            </p>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{sym} 衍生品拥挤度</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{crowdingRisk || '--'}</span>
            </div>
            <div className="text-xl font-extrabold font-mono text-ink pt-1">
              {crowdingRisk || '--'}
            </div>
            <p className="text-xs text-ink-3">
              用于识别资金费率极端拥挤与多空踩踏风险
            </p>
          </div>
        </div>

        {/* 2. 真实快讯与链上异动列表（无数据时诚实空态） */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-surface p-5 rounded-xl border border-line shadow-xs space-y-3">
            <div className="flex items-center justify-between pb-2 border-b border-line-soft">
              <h3 className="text-xs font-bold text-ink flex items-center space-x-2">
                <Newspaper className="w-4 h-4 text-brand" />
                <span>{sym} 实时采集资讯快讯</span>
              </h3>
              <span className="text-xs text-ink-3">共 {newsItems.length} 条真实记录</span>
            </div>

            {newsItems.length === 0 ? (
              <div className="p-6 text-center text-xs text-ink-3 bg-panel rounded-lg border border-dashed border-line-strong">
                当前未采集到外部突发快讯（系统已禁用静态假利好兜底，保证零幻觉决策）。
              </div>
            ) : (
              <div className="space-y-2.5">
                {newsItems.map((news, idx) => (
                  <div key={idx} className="p-3 bg-panel rounded-lg border border-line-soft space-y-1">
                    <div className="flex items-center justify-between text-xs text-ink-3">
                      <span className="font-semibold text-ink-2">{news.source || provenance}</span>
                      <span>{news.time || news.published_at || '--'}</span>
                    </div>
                    <div className="text-xs font-semibold text-ink">
                      {typeof news === 'string' ? news : news.title || JSON.stringify(news)}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="bg-surface p-5 rounded-xl border border-line shadow-xs space-y-3">
            <div className="flex items-center justify-between pb-2 border-b border-line-soft">
              <h3 className="text-xs font-bold text-ink flex items-center space-x-2">
                <Activity className="w-4 h-4 text-success" />
                <span>{sym} 链上大额异动监测 (Whale Tracker)</span>
              </h3>
              <span className="text-xs font-mono text-ink-3">共 {whaleTransfers.length} 条记录</span>
            </div>

            {whaleTransfers.length === 0 ? (
              <div className="p-6 text-center text-xs text-ink-3 bg-panel rounded-lg border border-dashed border-line-strong">
                当前未配置付费链上大额监控源，暂无实时大额充提流水。
              </div>
            ) : (
              <div className="space-y-2.5 font-mono">
                {whaleTransfers.map((tx, idx) => (
                  <div key={idx} className="p-3 bg-panel rounded-lg border border-line-soft flex items-center justify-between text-xs">
                    <div>
                      <div className="font-bold text-ink">{tx.amount}</div>
                      <div className="text-xs text-ink-3 mt-0.5">
                        {tx.from} ➔ {tx.to}
                      </div>
                    </div>
                    <div className="text-right text-xs text-ink-4">
                      <div>{tx.time}</div>
                      <div className="text-brand">{tx.txHash}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
