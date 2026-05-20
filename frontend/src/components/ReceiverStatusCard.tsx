import { Webhook } from 'lucide-react';
import type { ProcessInfo } from '../lib/api';

interface Props {
  receiver: ProcessInfo;
}

export function ReceiverStatusCard({ receiver }: Props) {
  const alive = receiver.alive;
  return (
    <div className="glass-strong rounded-lg p-4 flex items-center gap-3">
      <div className={`w-9 h-9 rounded-md flex items-center justify-center ${
        alive ? 'bg-emerald-500/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'
      }`}>
        <Webhook className="w-4 h-4" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-xs text-slate-500">webhook receiver</div>
        <div className="text-sm text-slate-100">
          {alive ? '运行中' : '未运行'}
          {receiver.port && (
            <span className="ml-2 text-[11px] text-slate-500">:{receiver.port}</span>
          )}
        </div>
      </div>
    </div>
  );
}
