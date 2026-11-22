/** @type {import('tailwindcss').Config} */
// 颜色都引用 src/index.css 里的颜色变量，亮色 / 暗色主题切换 html[data-theme] 即可，不需要 dark: 前缀
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: 'rgb(var(--c-canvas) / <alpha-value>)',
        panel: 'rgb(var(--c-panel) / <alpha-value>)',
        surface: 'rgb(var(--c-surface) / <alpha-value>)',
        subtle: 'rgb(var(--c-subtle) / <alpha-value>)',
        muted: { DEFAULT: 'rgb(var(--c-muted) / <alpha-value>)', 'strong': 'rgb(var(--c-muted-strong) / <alpha-value>)' },
        line: { DEFAULT: 'rgb(var(--c-line) / <alpha-value>)', 'soft': 'rgb(var(--c-line-soft) / <alpha-value>)', 'strong': 'rgb(var(--c-line-strong) / <alpha-value>)' },
        ink: { DEFAULT: 'rgb(var(--c-ink) / <alpha-value>)', 2: 'rgb(var(--c-ink-2) / <alpha-value>)', 3: 'rgb(var(--c-ink-3) / <alpha-value>)', 4: 'rgb(var(--c-ink-4) / <alpha-value>)' },
        brand: { DEFAULT: 'rgb(var(--c-brand) / <alpha-value>)', 'hover': 'rgb(var(--c-brand-hover) / <alpha-value>)', 'strong': 'rgb(var(--c-brand-strong) / <alpha-value>)', 'soft': 'rgb(var(--c-brand-soft) / <alpha-value>)', 'soft-strong': 'rgb(var(--c-brand-soft-strong) / <alpha-value>)', 'line': 'rgb(var(--c-brand-line) / <alpha-value>)' },
        success: { DEFAULT: 'rgb(var(--c-success) / <alpha-value>)', 'solid': 'rgb(var(--c-success-solid) / <alpha-value>)', 'solid-hover': 'rgb(var(--c-success-solid-hover) / <alpha-value>)', 'strong': 'rgb(var(--c-success-strong) / <alpha-value>)', 'soft': 'rgb(var(--c-success-soft) / <alpha-value>)', 'line': 'rgb(var(--c-success-line) / <alpha-value>)' },
        danger: { DEFAULT: 'rgb(var(--c-danger) / <alpha-value>)', 'solid': 'rgb(var(--c-danger-solid) / <alpha-value>)', 'solid-hover': 'rgb(var(--c-danger-solid-hover) / <alpha-value>)', 'strong': 'rgb(var(--c-danger-strong) / <alpha-value>)', 'soft': 'rgb(var(--c-danger-soft) / <alpha-value>)', 'line': 'rgb(var(--c-danger-line) / <alpha-value>)' },
        warning: { DEFAULT: 'rgb(var(--c-warning) / <alpha-value>)', 'solid': 'rgb(var(--c-warning-solid) / <alpha-value>)', 'solid-hover': 'rgb(var(--c-warning-solid-hover) / <alpha-value>)', 'strong': 'rgb(var(--c-warning-strong) / <alpha-value>)', 'soft': 'rgb(var(--c-warning-soft) / <alpha-value>)', 'line': 'rgb(var(--c-warning-line) / <alpha-value>)' },
        info: { DEFAULT: 'rgb(var(--c-info) / <alpha-value>)', 'solid': 'rgb(var(--c-info-solid) / <alpha-value>)', 'solid-hover': 'rgb(var(--c-info-solid-hover) / <alpha-value>)', 'strong': 'rgb(var(--c-info-strong) / <alpha-value>)', 'soft': 'rgb(var(--c-info-soft) / <alpha-value>)', 'line': 'rgb(var(--c-info-line) / <alpha-value>)' },
        plum: 'rgb(var(--c-plum) / <alpha-value>)',
      },
      borderRadius: {
        'tactile': '14px',
        'arena': '36px',
      }
    },
  },
  plugins: [],
}
