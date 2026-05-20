import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { LayoutGrid } from 'lucide-react';
import type { Court } from '../lib/api';
import { CourtCard } from './CourtCard';
import { KillConfirmDialog } from './KillConfirmDialog';

interface Props {
  courts: Court[];
}

export function CourtsGrid({ courts }: Props) {
  const [killTarget, setKillTarget] = useState<Court | null>(null);

  return (
    <section className="glass rounded-xl p-5">
      <header className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 text-slate-200 font-medium">
          <LayoutGrid className="w-4 h-4 text-accent-500" />
          <h2>活跃 court</h2>
          <span className="text-xs text-slate-500">({courts.length})</span>
        </div>
      </header>
      {courts.length === 0 ? (
        <div className="text-sm text-slate-500 py-8 text-center">
          dashboard 没有活跃 window
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          <AnimatePresence mode="popLayout" initial={false}>
            {courts.map((court) => (
              <motion.div
                key={court.id}
                layout
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96 }}
                transition={{ duration: 0.18 }}
              >
                <CourtCard court={court} onKill={() => setKillTarget(court)} />
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}
      <KillConfirmDialog
        court={killTarget}
        onClose={() => setKillTarget(null)}
      />
    </section>
  );
}
