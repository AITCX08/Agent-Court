import { useSSE } from './lib/useSSE';
import { useStore } from './lib/store';
import { Layout } from './components/Layout';
import { CourtsGrid } from './components/CourtsGrid';
import { PendingPanel } from './components/PendingPanel';
import { WatcherStatusCard } from './components/WatcherStatusCard';
import { ReceiverStatusCard } from './components/ReceiverStatusCard';
import { TmuxPanel } from './components/TmuxPanel';
import { ActivityFeed } from './components/ActivityFeed';
import { OrchestratorHealthPanel } from './components/OrchestratorHealthPanel';
import { ToastProvider } from './components/Toast';

export default function App() {
  return (
    <ToastProvider>
      <Dashboard />
    </ToastProvider>
  );
}

function Dashboard() {
  const status = useStore((s) => s.status);
  const connected = useStore((s) => s.connected);
  const activity = useStore((s) => s.activity);
  useSSE();

  return (
    <Layout connected={connected} status={status}>
      <div className="p-5 grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-5">
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
    </Layout>
  );
}
