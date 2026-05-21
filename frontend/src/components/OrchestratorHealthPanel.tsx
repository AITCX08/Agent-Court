import { AlertTriangle, ShieldCheck, AlertOctagon, Ghost } from 'lucide-react';
import type { OrchestratorView, OrchestratorInconsistency } from '../lib/api';

interface Props {
  orchestrator?: OrchestratorView;
}

// 状态分布要在徽章条里显示的几个核心 state. 其它 state 在 metrics 里也有,
// 但在面板里挤进 4 个能直观看出有几个 run 在跑.
const STATE_BADGES: { key: string; label: string; tone: string }[] = [
  { key: 'executing', label: 'Exec', tone: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' },
  { key: 'dispatched', label: 'Disp', tone: 'bg-sky-500/15 text-sky-300 border-sky-500/30' },
  { key: 'pending_approval', label: 'Wait', tone: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  { key: 'queued', label: 'Queue', tone: 'bg-slate-500/15 text-slate-300 border-slate-500/30' },
  { key: 'failed', label: 'Fail', tone: 'bg-rose-500/15 text-rose-300 border-rose-500/30' },
];

export function OrchestratorHealthPanel({ orchestrator }: Props) {
  if (!orchestrator) {
    return null;
  }
  const { inconsistencies, metrics, orphan_tmux_windows } = orchestrator;
  const errorCount = inconsistencies.filter((i) => i.severity === 'error').length;
  const warnCount = inconsistencies.length - errorCount;
  const totalRuns = metrics.total ?? 0;

  return (
    <section className="glass rounded-xl p-5">
      <header className="flex items-center gap-2 text-slate-200 font-medium mb-3">
        {errorCount > 0 ? (
          <AlertOctagon className="w-4 h-4 text-rose-400" />
        ) : warnCount > 0 ? (
          <AlertTriangle className="w-4 h-4 text-amber-400" />
        ) : (
          <ShieldCheck className="w-4 h-4 text-emerald-400" />
        )}
        <h2>Orchestrator 健康</h2>
        <span className="text-xs text-slate-500">({totalRuns} runs)</span>
      </header>

      <div className="flex flex-wrap gap-1.5 mb-4">
        {STATE_BADGES.map(({ key, label, tone }) => {
          const n = metrics[key] ?? 0;
          if (n === 0) return null;
          return (
            <span
              key={key}
              className={`text-[10px] px-2 py-0.5 rounded-full border ${tone}`}
              title={`${key} = ${n}`}
            >
              {label} {n}
            </span>
          );
        })}
        {totalRuns === 0 && (
          <span className="text-[11px] text-slate-500">还没有 run</span>
        )}
      </div>

      {inconsistencies.length === 0 ? (
        <div className="text-xs text-emerald-300/80 flex items-center gap-2">
          <ShieldCheck className="w-3.5 h-3.5" />
          没有不一致
        </div>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {inconsistencies.map((inc, i) => (
            <InconsistencyRow key={`${inc.kind}-${inc.issue_key}-${i}`} inc={inc} />
          ))}
        </ul>
      )}

      {orphan_tmux_windows.length > 0 && (
        <div className="mt-3 pt-3 border-t border-white/5 text-[11px] text-slate-500 flex items-start gap-1.5">
          <Ghost className="w-3 h-3 mt-0.5 flex-shrink-0" />
          <span>
            孤儿 tmux window: {orphan_tmux_windows.join(', ')}
          </span>
        </div>
      )}
    </section>
  );
}

function InconsistencyRow({ inc }: { inc: OrchestratorInconsistency }) {
  const isErr = inc.severity === 'error';
  return (
    <li className={`glass-strong rounded-md p-2 text-[11px] border ${
      isErr ? 'border-rose-500/30' : 'border-amber-500/20'
    }`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${
          isErr
            ? 'bg-rose-500/20 text-rose-300'
            : 'bg-amber-500/15 text-amber-300'
        }`}>
          {isErr ? 'ERROR' : 'WARN'}
        </span>
        <code className="text-slate-300">{inc.kind}</code>
        {inc.issue_key && (
          <span className="text-slate-500 truncate">{inc.issue_key}</span>
        )}
      </div>
      <div className="text-slate-400 leading-snug">{inc.detail}</div>
      <div className="text-slate-600 mt-0.5">→ {inc.suggested_fix}</div>
    </li>
  );
}
