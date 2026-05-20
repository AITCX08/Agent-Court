import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Inbox, Check, X } from 'lucide-react';
import type { Pending } from '../lib/api';
import { approve, reject } from '../lib/api';
import { useStore } from '../lib/store';
import { useToast } from './Toast';

interface Props {
  pending: Pending[];
}

export function PendingPanel({ pending }: Props) {
  const setStatus = useStore((s) => s.setStatus);
  const status = useStore((s) => s.status);
  const { push } = useToast();
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const optimisticRemove = (slugId: string) => {
    if (!status) return null;
    const snapshot = status;
    setStatus({ ...status, pending: status.pending.filter((p) => p.slug_id !== slugId) });
    return snapshot;
  };

  const setBusy = (slugId: string, busy: boolean) => {
    setBusyIds((prev) => {
      const next = new Set(prev);
      busy ? next.add(slugId) : next.delete(slugId);
      return next;
    });
  };

  const doVerdict = async (
    p: Pending,
    fn: typeof approve,
    label: '通过' | '拒绝'
  ) => {
    setBusy(p.slug_id, true);
    const rollback = optimisticRemove(p.slug_id);
    try {
      await fn(
        { slug_id: p.slug_id, repo: p.repo, number: p.number, stage: p.stage },
        reasons[p.slug_id] ?? ''
      );
      push({ kind: 'ok', text: `${label}: ${p.repo}#${p.number} ${p.stage}` });
      setReasons((prev) => {
        const next = { ...prev };
        delete next[p.slug_id];
        return next;
      });
    } catch (err) {
      if (rollback) setStatus(rollback);
      push({ kind: 'err', text: `${label}失败: ${(err as Error).message}` });
    } finally {
      setBusy(p.slug_id, false);
    }
  };

  return (
    <section className="glass rounded-xl p-5">
      <header className="flex items-center gap-2 text-slate-200 font-medium mb-4">
        <Inbox className="w-4 h-4 text-accent-500" />
        <h2>待审批</h2>
        <span className="text-xs text-slate-500">({pending.length})</span>
      </header>
      {pending.length === 0 ? (
        <div className="text-sm text-slate-500 py-6 text-center">没有等审批的请求</div>
      ) : (
        <ul className="flex flex-col gap-2">
          <AnimatePresence initial={false}>
            {pending.map((p) => {
              const busy = busyIds.has(p.slug_id);
              return (
                <motion.li
                  key={p.slug_id}
                  layout
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, x: -10 }}
                  className="glass-strong rounded-lg p-3 flex flex-col gap-2"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <div className="text-sm text-slate-100">
                      {p.repo ?? '?'}
                      <span className="text-slate-500"> #</span>
                      <span className="text-accent-500">{p.number ?? '?'}</span>
                    </div>
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-sky-500/20 text-sky-300 border border-sky-500/30">
                      {p.stage ?? '—'}
                    </span>
                  </div>
                  <input
                    type="text"
                    placeholder="备注 (可选)"
                    value={reasons[p.slug_id] ?? ''}
                    onChange={(e) =>
                      setReasons((prev) => ({ ...prev, [p.slug_id]: e.target.value }))
                    }
                    className="bg-ink-700/60 border border-white/10 rounded-md px-2 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-accent-500/40"
                  />
                  <div className="flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => doVerdict(p, reject, '拒绝')}
                      disabled={busy}
                      className="px-3 py-1 rounded-md text-xs text-rose-300 hover:bg-rose-500/15 transition disabled:opacity-40 flex items-center gap-1"
                    >
                      <X className="w-3 h-3" />
                      拒绝
                    </button>
                    <button
                      type="button"
                      onClick={() => doVerdict(p, approve, '通过')}
                      disabled={busy}
                      className="px-3 py-1 rounded-md text-xs bg-emerald-500/80 text-white hover:bg-emerald-500 transition disabled:opacity-50 flex items-center gap-1"
                    >
                      <Check className="w-3 h-3" />
                      通过
                    </button>
                  </div>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      )}
    </section>
  );
}
