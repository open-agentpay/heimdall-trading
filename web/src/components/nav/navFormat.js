/** 净值页共用的格式化工具：成交价来源标签、金额、价格偏差 */

// 成交价来源标签（对应后端 place_paper_order 返回的 price_source / reference_price_source）
export const PRICE_SOURCE_META = {
  LIVE: { label: '跨所实时中位价', className: 'bg-success-soft text-success border-success-line' },
  USER: { label: '手动指定价', className: 'bg-subtle text-ink-2 border-line-strong' },
  SIMULATED: { label: '离线仿真价', className: 'bg-warning-soft text-warning border-warning-line' },
};

export function priceSourceMeta(source) {
  return PRICE_SOURCE_META[String(source || '').toUpperCase()] || {
    label: source || '未知来源',
    className: 'bg-subtle text-ink-3 border-line-strong',
  };
}

export function formatUsd(value) {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '--';
  return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
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
