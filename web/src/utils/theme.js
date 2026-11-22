/**
 * 主题工具：颜色变量定义在 index.css（:root 为暖沙亮色，html[data-theme="dark"] 为暗色），
 * Tailwind 颜色（bg-surface、text-ink-2、border-line …）都引用这些变量。
 * 画布 / 第三方图表拿不到 CSS 类时，用 cssColor() 读取当前主题下的实际颜色。
 */
import { useEffect, useState } from 'react';

export const getThemeName = () =>
  (typeof document !== 'undefined' && document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');

/** 当前主题名（light / dark），切换主题时触发重新渲染 */
export function useThemeName() {
  const [name, setName] = useState(getThemeName);
  useEffect(() => {
    const el = document.documentElement;
    const obs = new MutationObserver(() => setName(getThemeName()));
    obs.observe(el, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);
  return name;
}

/** 读取颜色变量 --c-<token> 的当前值，返回 rgb(r, g, b) / rgba(r, g, b, a)（画布与第三方组件都能识别） */
export function cssColor(token, alpha = 1) {
  if (typeof document === 'undefined') return 'transparent';
  const raw = getComputedStyle(document.documentElement).getPropertyValue(`--c-${token}`).trim();
  const parts = raw.split(/\s+/).filter(Boolean);
  if (parts.length !== 3) return 'transparent';
  return alpha >= 1 ? `rgb(${parts.join(', ')})` : `rgba(${parts.join(', ')}, ${alpha})`;
}
