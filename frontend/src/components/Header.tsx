import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Activity, RefreshCw } from 'lucide-react';
import type { Status } from '../lib/api';
import { getStatus } from '../lib/api';
import { useStore } from '../lib/store';

interface Props {
  connected: boolean;
  status: Status | null;
}

export function Header({ connected, status }: Props) {
  const [now, setNow] = useState<number>(Date.now());
  const setStatus = useStore((s) => s.setStatus);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const fresh = await getStatus();
      setStatus(fresh);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <header className="glass px-6 py-3 flex items-center gap-4 sticky top-0 z-20">
      <div className="flex items-center gap-2 font-semibold text-slate-100">
        <Activity className="w-4 h-4 text-accent-500" />
        <span>court-dashboard</span>
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-2 text-xs text-slate-400">
        <motion.span
          className={`inline-block w-2 h-2 rounded-full ${
            connected ? 'bg-emerald-400' : 'bg-rose-500'
          }`}
          animate={connected ? { opacity: [0.6, 1, 0.6] } : { opacity: 1 }}
          transition={{ duration: 1.4, repeat: Infinity }}
        />
        <span>{connected ? '已连接' : '重连中…'}</span>
      </div>
      <button
        type="button"
        onClick={refresh}
        disabled={refreshing}
        className="glass-strong px-3 py-1.5 rounded-md text-xs text-slate-200 hover:text-white transition disabled:opacity-50"
        title="手动刷新"
      >
        <RefreshCw className={`w-3.5 h-3.5 inline-block mr-1 ${refreshing ? 'animate-spin' : ''}`} />
        刷新
      </button>
      <div className="text-xs text-slate-500 tabular-nums w-20 text-right">
        {formatTime(now, status?.ts ?? 0)}
      </div>
    </header>
  );
}

function formatTime(now: number, serverTs: number): string {
  if (!serverTs) return '--:--:--';
  const d = new Date(Math.min(now, serverTs * 1000 + (now - serverTs * 1000)));
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}
