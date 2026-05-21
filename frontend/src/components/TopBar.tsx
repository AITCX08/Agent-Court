import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ThemeToggle } from './ThemeToggle';
import { LangSwitch } from './LangSwitch';
import type { Route } from '../lib/router';

interface Props {
  route: Route;
  connected: boolean;
  updatedTs: number | null;
}

const ROUTE_TITLE_KEY: Record<Route, string> = {
  '/git-board': 'topbar.title.git_board',
  '/agents': 'topbar.title.agents',
  '/court-runtime': 'topbar.title.court_runtime',
};

function formatTime(ts: number | null): string {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString(undefined, { hour12: false });
}

export function TopBar({ route, connected, updatedTs }: Props) {
  const { t } = useTranslation();
  // 每秒重渲染一次, 让 updatedTs 显示能跟 SSE 不一致时也能动
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <header className="h-14 border-b border-border-base bg-bg-base
                       flex items-center px-5 gap-4">
      <h1 className="text-sm font-medium text-fg-primary">
        {t(ROUTE_TITLE_KEY[route])}
      </h1>
      <span className="text-xs text-fg-muted">
        {t('topbar.updated_at', { time: formatTime(updatedTs) })}
      </span>
      <span
        className={`w-2 h-2 rounded-full ${
          connected ? 'bg-accent-success' : 'bg-accent-danger'
        }`}
        aria-label={connected ? 'connected' : 'disconnected'}
      />
      <div className="flex-1" />
      <ThemeToggle />
      <LangSwitch />
    </header>
  );
}
