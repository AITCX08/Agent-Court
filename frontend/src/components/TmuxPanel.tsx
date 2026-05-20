import { Terminal } from 'lucide-react';
import type { TmuxSession } from '../lib/api';

interface Props {
  sessions: TmuxSession[];
}

export function TmuxPanel({ sessions }: Props) {
  return (
    <div className="glass-strong rounded-lg p-4">
      <header className="flex items-center gap-2 text-slate-200 text-sm font-medium mb-2">
        <Terminal className="w-4 h-4 text-accent-500" />
        <span>tmux session</span>
      </header>
      {sessions.length === 0 ? (
        <div className="text-xs text-slate-500 py-2">没有 dashboard session</div>
      ) : (
        <ul className="text-xs text-slate-400 space-y-1">
          {sessions.map((s) => (
            <li key={s.name} className="flex justify-between">
              <code className="text-accent-500/90">{s.name}</code>
              <span>
                {s.windows} window{s.windows === 1 ? '' : 's'}{' '}
                {s.attached && <span className="text-emerald-400">●</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
