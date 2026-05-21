import type { Config } from 'tailwindcss';

// PR-16a 双主题: 颜色用 CSS 变量映射, 由 :root[data-theme=*] 决定值.
// 旧 ink/accent palette 保留, 给 PR-15 legacy 组件 (Court Runtime page 仍要渲染)
// 不破坏.
const config: Config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // PR-16a tokens
        'bg-base': 'var(--bg-base)',
        'bg-card': 'var(--bg-card)',
        'bg-card-hover': 'var(--bg-card-hover)',
        'bg-sidebar': 'var(--bg-sidebar)',
        'fg-primary': 'var(--fg-primary)',
        'fg-secondary': 'var(--fg-secondary)',
        'fg-muted': 'var(--fg-muted)',
        'border-base': 'var(--border-base)',
        'border-strong': 'var(--border-strong)',
        'accent-primary': 'var(--accent-primary)',
        'accent-warn': 'var(--accent-warn)',
        'accent-danger': 'var(--accent-danger)',
        'accent-success': 'var(--accent-success)',
        'accent-purple': 'var(--accent-purple)',
        // PR-15 legacy
        ink: {
          900: '#0b0d10',
          800: '#10141a',
          700: '#1a1f25',
          600: '#2a3239',
        },
        accent: {
          500: '#a5d4ff',
          400: '#c2e0ff',
        },
      },
      animation: {
        'pulse-soft': 'pulse 2.4s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
};

export default config;
