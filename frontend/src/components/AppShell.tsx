import { useHash } from '../lib/router';
import { useStore } from '../lib/store';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { GitBoardPage } from './pages/GitBoardPage';
import { AgentsPage } from './pages/AgentsPage';
import { CourtRuntimePage } from './pages/CourtRuntimePage';
import { MessagesPage } from './pages/MessagesPage';

export function AppShell() {
  const [route, setRoute, platform] = useHash();
  const connected = useStore((s) => s.connected);
  const status = useStore((s) => s.status);
  const updatedTs = status?.ts ?? null;

  return (
    <div className="h-full flex">
      <Sidebar route={route} platform={platform} onNavigate={setRoute} />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar route={route} platform={platform} connected={connected} updatedTs={updatedTs} />
        <main className="flex-1 overflow-auto bg-bg-base">
          {route === '/git-board' && <GitBoardPage />}
          {route === '/agents' && <AgentsPage />}
          {route === '/messages' && <MessagesPage platform={platform} />}
          {route === '/court-runtime' && <CourtRuntimePage />}
        </main>
      </div>
    </div>
  );
}
