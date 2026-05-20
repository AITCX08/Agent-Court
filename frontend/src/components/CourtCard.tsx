import { Skull, GitBranch, Hash, Clock } from 'lucide-react';
import type { Court } from '../lib/api';

interface Props {
  court: Court;
  onKill: () => void;
}

const STATUS_STYLES: Record<string, string> = {
  running: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30',
  awaiting_approval: 'bg-amber-500/20 text-amber-300 border-amber-500/30',
  awaiting_plan: 'bg-sky-500/20 text-sky-300 border-sky-500/30',
};

const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  awaiting_approval: '待审批',
  awaiting_plan: '等计划',
};

export function CourtCard({ court, onKill }: Props) {
  const statusClass = STATUS_STYLES[court.status] ?? 'bg-slate-500/20 text-slate-300 border-slate-500/30';
  const statusLabel = STATUS_LABEL[court.status] ?? court.status;
  return (
    <div className="glass-strong rounded-lg p-4 flex flex-col gap-2 hover:border-white/25 transition">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm font-medium text-slate-100 truncate" title={court.id}>
            {court.id}
          </div>
          <div className="flex items-center gap-3 text-[11px] text-slate-400 mt-0.5">
            <span className="flex items-center gap-1">
              <GitBranch className="w-3 h-3" />
              {court.repo ?? <span className="text-slate-600">未关联 repo</span>}
            </span>
            {court.issue !== null && (
              <span className="flex items-center gap-1">
                <Hash className="w-3 h-3" />
                {court.issue}
              </span>
            )}
          </div>
        </div>
        <span className={`text-[10px] px-2 py-0.5 rounded-full border ${statusClass} whitespace-nowrap`}>
          {statusLabel}
        </span>
      </div>
      <div className="flex items-center justify-between text-[11px] text-slate-500">
        <span className="flex items-center gap-1">
          <Clock className="w-3 h-3" />
          panes {court.panes ?? '-'} · #{court.window_index ?? '-'}
        </span>
        <button
          type="button"
          onClick={onKill}
          className="flex items-center gap-1 px-2 py-1 rounded-md text-rose-300 hover:text-rose-200 hover:bg-rose-500/10 transition"
          title="kill window"
        >
          <Skull className="w-3.5 h-3.5" />
          kill
        </button>
      </div>
    </div>
  );
}
