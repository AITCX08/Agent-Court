import { useTranslation } from 'react-i18next';
import { useStore } from '../../lib/store';
import { CourtsGrid } from '../CourtsGrid';
import { PendingPanel } from '../PendingPanel';
import { WatcherStatusCard } from '../WatcherStatusCard';
import { ReceiverStatusCard } from '../ReceiverStatusCard';
import { TmuxPanel } from '../TmuxPanel';
import { ActivityFeed } from '../ActivityFeed';
import { OrchestratorHealthPanel } from '../OrchestratorHealthPanel';

// PR-16a: 把 PR-15 老 dashboard 收纳到 Court Runtime 菜单. 内部布局沿用
// 老 App.tsx 的 grid + aside 结构, 仅顶部加了一行 subtitle.
export function CourtRuntimePage() {
  const { t } = useTranslation();
  const status = useStore((s) => s.status);
  const activity = useStore((s) => s.activity);

  return (
    <div className="p-5">
      <p className="text-xs text-fg-muted mb-4 px-1">
        {t('court_runtime.subtitle')}
      </p>
      <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-5">
        <div className="space-y-5">
          <CourtsGrid courts={status?.courts ?? []} />
          <ActivityFeed events={activity} />
        </div>
        <aside className="space-y-5">
          <OrchestratorHealthPanel orchestrator={status?.orchestrator} />
          <PendingPanel pending={status?.pending ?? []} />
          <div className="space-y-3">
            <WatcherStatusCard watcher={status?.watcher ?? { alive: false, pid: null }} />
            <ReceiverStatusCard receiver={status?.receiver ?? { alive: false, pid: null }} />
          </div>
          <TmuxPanel sessions={status?.tmux_sessions ?? []} />
        </aside>
      </div>
    </div>
  );
}
