import { useState, useEffect } from 'react';
import { Sun, Moon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { applyTheme, getTheme, type Theme } from '../lib/theme';

export function ThemeToggle() {
  const { t } = useTranslation();
  const [theme, setTheme] = useState<Theme>(getTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const next: Theme = theme === 'dark' ? 'light' : 'dark';
  const Icon = theme === 'dark' ? Sun : Moon;
  const title = theme === 'dark' ? t('topbar.theme.to_light') : t('topbar.theme.to_dark');

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      title={title}
      aria-label={title}
      className="w-8 h-8 inline-flex items-center justify-center rounded-md
                 text-fg-secondary hover:text-fg-primary
                 hover:bg-bg-card-hover transition"
    >
      <Icon className="w-4 h-4" />
    </button>
  );
}
