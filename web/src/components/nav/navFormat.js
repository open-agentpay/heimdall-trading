/** 净值页共用的格式化工具：成交价来源标签、金额、价格偏差（文案见 i18n 语言包 nav.priceSource） */
import i18n, { getLocale } from '../../i18n';

// 成交价来源标签（对应后端 place_paper_order 返回的 price_source / reference_price_source）
export const PRICE_SOURCE_META = {
  LIVE: { labelKey: 'nav:priceSource.live', className: 'bg-success-soft text-success border-success-line' },
  USER: { labelKey: 'nav:priceSource.user', className: 'bg-subtle text-ink-2 border-line-strong' },
  SIMULATED: { labelKey: 'nav:priceSource.simulated', className: 'bg-warning-soft text-warning border-warning-line' },
};

export function priceSourceMeta(source) {
  const meta = PRICE_SOURCE_META[String(source || '').toUpperCase()];
  if (meta) return { label: i18n.t(meta.labelKey), className: meta.className };
  return {
    label: source || i18n.t('nav:priceSource.unknown'),
    className: 'bg-subtle text-ink-3 border-line-strong',
  };
}

export function formatUsd(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '--';
  return `$${num.toLocaleString(getLocale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatDeviation(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return `${(num * 100).toFixed(2)}%`;
}

// 偏差着色：≤0.3% 绿 / ≤0.7% 橙 / 其余红（后端默认容差 1%）
export function deviationClass(value) {
  const num = Math.abs(Number(value) || 0);
  if (num <= 0.003) return 'text-success';
  if (num <= 0.007) return 'text-warning';
  return 'text-danger';
}
