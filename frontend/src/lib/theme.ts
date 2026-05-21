// PR-16a 主题切换. inline script in index.html 在 React 挂载前已经设好
// data-theme; 这里只负责切换 + 持久化.

export type Theme = 'dark' | 'light';

export const THEME_KEY = 'court-theme';

export function getTheme(): Theme {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === 'light' ? 'light' : 'dark';
}

export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(THEME_KEY, theme);
}
