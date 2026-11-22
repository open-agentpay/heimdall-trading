import React from 'react';
import { useTranslation } from 'react-i18next';
import { Activity } from 'lucide-react';
import { numOrNull } from '../../data/agentRoles';

/** 单位净值走势图：只画后端真实快照，不足两个点时显示空状态 */
export default function NavChart({ navData, navError }) {
  const { t } = useTranslation('nav');
  // 净值曲线只画后端真实快照；不足两个点时显示空状态，不再补“初始 1.0000”这类虚拟点
  const series = (navData && Array.isArray(navData.nav_series))
    ? navData.nav_series.filter((p) => numOrNull(p.nav) !== null)
    : [];
  const hasCurve = series.length >= 2;

  // 简易 SVG 净值曲线计算
  const navValues = hasCurve ? series.map(s => Number(s.nav)) : [1.0];
  const minNav = Math.min(...navValues, 0.99) * 0.99;
  const maxNav = Math.max(...navValues, 1.01) * 1.01;
  const range = maxNav - minNav || 0.1;

  const points = series.map((s, idx) => {
    const x = series.length > 1 ? (idx / (series.length - 1)) * 600 : 300;
    const y = 180 - ((Number(s.nav) - minNav) / range) * 160;
    return `${x},${y}`;
  }).join(' ');

  return (
    <div className="bg-surface p-5 rounded-2xl border border-line shadow-xs space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-2">
          <Activity className="w-4 h-4 text-brand" />
          <h3 className="text-xs font-bold text-ink">{t('chart.title')}</h3>
        </div>
        <div className="flex items-center space-x-3 text-xs text-ink-3">
          <span className="flex items-center space-x-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-brand"></span>
            <span className="font-semibold text-ink">{t('chart.strategy')}</span>
          </span>
          <span className="flex items-center space-x-1.5">
            <span className="w-2.5 h-2.5 rounded-full bg-muted-strong"></span>
            <span>{t('chart.baseline')}</span>
          </span>
        </div>
      </div>

      {/* SVG 矢量走势图 */}
      {!hasCurve ? (
        <div className="w-full h-48 bg-panel rounded-xl border border-dashed border-line flex items-center justify-center text-xs text-ink-4">
          {navError ? t('chart.unavailable', { error: navError }) : t('chart.notEnough')}
        </div>
      ) : (
      <div className="w-full h-48 bg-panel rounded-xl border border-line-soft p-3 relative flex flex-col justify-between">
        <svg viewBox="0 0 600 180" className="w-full h-full overflow-visible">
          {/* 基准虚线 1.000 */}
          <line 
            x1="0" 
            y1={180 - ((1.000 - minNav) / range) * 160} 
            x2="600" 
            y2={180 - ((1.000 - minNav) / range) * 160} 
            style={{ stroke: 'rgb(var(--c-line-strong))' }} 
            strokeDasharray="4 4" 
            strokeWidth="1.5"
          />

          {/* 曲线下方渐变填充 */}
          <defs>
            <linearGradient id="navGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" style={{ stopColor: 'rgb(var(--c-brand))', stopOpacity: 0.25 }} />
              <stop offset="100%" style={{ stopColor: 'rgb(var(--c-brand))', stopOpacity: 0.0 }} />
            </linearGradient>
          </defs>
          <polygon 
            points={`0,180 ${points} 600,180`} 
            fill="url(#navGradient)" 
          />

          {/* 主走势曲线 */}
          <polyline
            fill="none"
            style={{ stroke: 'rgb(var(--c-brand))' }}
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            points={points}
          />
        </svg>

        <div className="flex justify-between text-xs text-ink-4 font-mono pt-1 border-t border-line-soft">
          {series.map((s, i) => (
            <span key={i}>{s.date || (s.timestamp ? s.timestamp.slice(5, 10) : '')}</span>
          ))}
        </div>
      </div>
      )}
    </div>
  );
}
