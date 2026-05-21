import { useSSE } from './lib/useSSE';
import { AppShell } from './components/AppShell';
import { ToastProvider } from './components/Toast';

export default function App() {
  return (
    <ToastProvider>
      <Dashboard />
    </ToastProvider>
  );
}

function Dashboard() {
  useSSE();
  return <AppShell />;
}
