import { motion, AnimatePresence } from 'framer-motion';
import { ScrollText } from 'lucide-react';
import type { ActivityEvent } from '../lib/store';

interface Props {
  events: ActivityEvent[];
}

const KIND_COLOR: Record<ActivityEvent['kind'], string> = {
  court_started: 'text-emerald-300',
  court_ended: 'text-slate-400',
  pending_new: 'text-amber-300',
  pending_resolved: 'text-sky-300',
  watcher_up: 'text-emerald-300',
  watcher_down: 'text-rose-300',
  receiver_up: 'text-emerald-300',
  receiver_down: 'text-rose-300',
};

export function ActivityFeed({ events }: Props) {
  return (
    <section className="glass rounded-xl p-5 flex flex-col">
      <header className="flex items-center gap-2 text-slate-200 font-medium mb-3">
        <ScrollText className="w-4 h-4 text-accent-500" />
        <h2>动态</h2>
        <span className="text-xs text-slate-500">最近 {events.length} 条</span>
      </header>
      {events.length === 0 ? (
        <div className="text-sm text-slate-500 py-4 text-center">
          dashboard 启动后会在这里记录变化
        </div>
      ) : (
        <ul className="text-xs space-y-1.5 max-h-72 overflow-y-auto pr-2">
          <AnimatePresence initial={false}>
            {events.map((e) => (
              <motion.li
                key={e.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0 }}
                className="flex items-center gap-2"
              >
                <span className="text-slate-600 tabular-nums w-12 shrink-0">
                  {new Date(e.ts).toLocaleTimeString('zh-CN', { hour12: false })}
                </span>
                <span className={KIND_COLOR[e.kind]}>{e.label}</span>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
    </section>
  );
}
