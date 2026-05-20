// 最小 Toast 实现, 避开 shadcn/sonner cli init 交互
import { createContext, useCallback, useContext, useEffect, useState, ReactNode } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { CheckCircle2, XCircle } from 'lucide-react';

export type ToastKind = 'ok' | 'err';

interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastApi {
  push: (item: { kind: ToastKind; text: string }) => void;
}

const Ctx = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback(({ kind, text }: { kind: ToastKind; text: string }) => {
    setItems((prev) => [...prev, { id: Date.now() + Math.random(), kind, text }]);
  }, []);

  useEffect(() => {
    if (items.length === 0) return;
    const t = window.setTimeout(() => setItems((p) => p.slice(1)), 3000);
    return () => clearTimeout(t);
  }, [items]);

  return (
    <Ctx.Provider value={{ push }}>
      {children}
      <div className="fixed bottom-4 right-4 z-40 flex flex-col gap-2 pointer-events-none">
        <AnimatePresence>
          {items.map((it) => (
            <motion.div
              key={it.id}
              initial={{ opacity: 0, x: 30, scale: 0.96 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              exit={{ opacity: 0, x: 30, scale: 0.96 }}
              className={`glass-strong rounded-lg px-4 py-2.5 text-sm flex items-center gap-2 max-w-sm pointer-events-auto ${
                it.kind === 'ok' ? 'text-emerald-300' : 'text-rose-300'
              }`}
            >
              {it.kind === 'ok' ? (
                <CheckCircle2 className="w-4 h-4" />
              ) : (
                <XCircle className="w-4 h-4" />
              )}
              <span className="flex-1">{it.text}</span>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useToast must be inside <ToastProvider>');
  return ctx;
}
