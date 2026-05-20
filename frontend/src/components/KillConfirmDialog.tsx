import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AlertTriangle } from 'lucide-react';
import type { Court } from '../lib/api';
import { killCourt } from '../lib/api';
import { useToast } from './Toast';

interface Props {
  court: Court | null;
  onClose: () => void;
}

export function KillConfirmDialog({ court, onClose }: Props) {
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  const confirm = async () => {
    if (!court) return;
    setBusy(true);
    try {
      await killCourt(court.window);
      push({ kind: 'ok', text: `已 kill ${court.window}` });
      onClose();
    } catch (err) {
      push({ kind: 'err', text: `kill 失败: ${(err as Error).message}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <AnimatePresence>
      {court && (
        <motion.div
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className="glass-strong rounded-xl max-w-md w-full mx-4 p-6 text-slate-200"
            initial={{ scale: 0.92, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.92, opacity: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-6 h-6 text-amber-400 shrink-0 mt-0.5" />
              <div className="flex-1">
                <h3 className="font-semibold text-slate-100 mb-1">确认 kill?</h3>
                <p className="text-sm text-slate-400">
                  即将关闭 tmux window <code className="text-accent-500">{court.window}</code>.
                  正在跑的 claude 进程会被立即终止, 未保存的工作会丢.
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-2 mt-6">
              <button
                type="button"
                onClick={onClose}
                disabled={busy}
                className="px-4 py-1.5 rounded-md text-sm text-slate-300 hover:bg-white/5 transition"
              >
                取消
              </button>
              <button
                type="button"
                onClick={confirm}
                disabled={busy}
                className="px-4 py-1.5 rounded-md text-sm bg-rose-500/80 text-white hover:bg-rose-500 transition disabled:opacity-50"
              >
                {busy ? 'kill 中…' : '确认 kill'}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
