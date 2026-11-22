import { useEffect, useState } from 'react';

/** 亮色 / 暗色主题：同步到 html[data-theme]（颜色变量见 index.css）并记住选择 */
export function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem('heimdall_theme') || 'light');

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('heimdall_theme', theme);
  }, [theme]);

  const toggleTheme = () => setTheme((prev) => (prev === 'dark' ? 'light' : 'dark'));

  return { theme, toggleTheme };
}
