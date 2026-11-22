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
import { useTranslation } from 'react-i18next';
import {
  Compass, Newspaper, TrendingUp, Activity, ShieldCheck
} from 'lucide-react';

export default function SentimentView({
  symbol = 'ETH',
  sentimentReport = null,
}) {
  const { t } = useTranslation('sentiment');
  const sym = (symbol || 'ETH').toUpperCase();
  const report = sentimentReport || {};

  const hasLiveReport = Boolean(sentimentReport && Object.keys(sentimentReport).length > 0);
  // 字段缺失时显示 “--”，不再把“没有数据”显示成 +0.00 中性 / NEUTRAL / BALANCED 这类看似真实的读数
  const sentimentScore = typeof report.sentiment_score === 'number' && Number.isFinite(report.sentiment_score) ? report.sentiment_score : null;
  const fearGreed = report.fear_greed_index ?? report.fear_greed_score ?? null;
  const whaleActivity = report.whale_activity || null;
  const crowdingRisk = report.crowding_risk || null;
  const provenance = report.provenance || report.data_source || (hasLiveReport ? 'live_collector' : 'awaiting_step');
  const summaryText = report.summary || t('summary.awaiting');
  const newsItems = Array.isArray(report.news_items) ? report.news_items : (Array.isArray(report.headlines) ? report.headlines : []);
  const whaleTransfers = Array.isArray(report.whale_transfers) ? report.whale_transfers : [];

  const scoreLabel = sentimentScore === null ? '--' : sentimentScore >= 0.15 ? t('score.bullish') : sentimentScore <= -0.15 ? t('score.bearish') : t('score.neutral');
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
              <span>{t('header.title', { symbol: sym })}</span>
              <span className="text-xs font-mono bg-brand-soft text-brand border border-brand-line px-2 py-0.5 rounded-full font-semibold">
                {t('header.source', { provenance })}
              </span>
            </h1>
            <p className="text-xs text-ink-3">
              {t('header.subtitle')}
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
            {hasLiveReport ? t('summary.live') : t('summary.awaitingFirst')}
          </span>
        </div>

        {/* 1. 核心四大维度真实指标卡片 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{t('score.title')}</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{scoreLabel}</span>
            </div>
            <div className="flex items-baseline space-x-2">
              <span className={`text-3xl font-extrabold font-mono ${scoreColor}`}>
                {sentimentScore === null ? '--' : `${sentimentScore >= 0 ? '+' : ''}${sentimentScore.toFixed(2)}`}
              </span>
              <span className="text-xs text-ink-3">[-1.0 ~ +1.0]</span>
            </div>
            <p className="text-xs text-ink-3">
              {t('score.hint')}
            </p>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{t('fearGreed.title')}</span>
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
              <span>{fearGreed !== null ? t('fearGreed.live') : t('fearGreed.missing')}</span>
            </div>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{t('whale.title', { symbol: sym })}</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{whaleActivity || '--'}</span>
            </div>
            <div className="text-xl font-extrabold font-mono text-ink pt-1">
              {whaleActivity === 'ACCUMULATION'
                ? t('whale.accumulation')
                : whaleActivity === 'DISTRIBUTION'
                ? t('whale.distribution')
                : whaleActivity
                ? t('whale.neutral')
                : '--'}
            </div>
            <p className="text-xs text-ink-3">
              {t('whale.hint')}
            </p>
          </div>

          <div className="bg-surface p-4 rounded-xl border border-line shadow-xs space-y-2">
            <div className="flex items-center justify-between text-xs font-bold text-ink-3">
              <span>{t('crowding.title', { symbol: sym })}</span>
              <span className="text-xs bg-panel px-2 py-0.5 rounded font-mono">{crowdingRisk || '--'}</span>
            </div>
            <div className="text-xl font-extrabold font-mono text-ink pt-1">
              {crowdingRisk || '--'}
            </div>
            <p className="text-xs text-ink-3">
              {t('crowding.hint')}
            </p>
          </div>
        </div>

        {/* 2. 真实快讯与链上异动列表（无数据时诚实空态） */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="bg-surface p-5 rounded-xl border border-line shadow-xs space-y-3">
            <div className="flex items-center justify-between pb-2 border-b border-line-soft">
              <h3 className="text-xs font-bold text-ink flex items-center space-x-2">
                <Newspaper className="w-4 h-4 text-brand" />
                <span>{t('news.title', { symbol: sym })}</span>
              </h3>
              <span className="text-xs text-ink-3">{t('news.count', { n: newsItems.length })}</span>
            </div>

            {newsItems.length === 0 ? (
              <div className="p-6 text-center text-xs text-ink-3 bg-panel rounded-lg border border-dashed border-line-strong">
                {t('news.empty')}
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
                <span>{t('transfers.title', { symbol: sym })}</span>
              </h3>
              <span className="text-xs font-mono text-ink-3">{t('transfers.count', { n: whaleTransfers.length })}</span>
            </div>

            {whaleTransfers.length === 0 ? (
              <div className="p-6 text-center text-xs text-ink-3 bg-panel rounded-lg border border-dashed border-line-strong">
                {t('transfers.empty')}
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
