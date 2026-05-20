// 前后两次 status snapshot 差分, 派生 ActivityEvent 时间线
import type { Status } from './api';
import type { ActivityEvent } from './store';

let activitySeq = 0;
const newId = () => `${Date.now()}-${++activitySeq}`;

export function deriveActivity(prev: Status | null, next: Status): ActivityEvent[] {
  const events: ActivityEvent[] = [];
  const ts = next.ts * 1000;
  if (!prev) {
    return events;
  }
  const prevCourtIds = new Set(prev.courts.map((c) => c.id));
  const nextCourtIds = new Set(next.courts.map((c) => c.id));
  for (const c of next.courts) {
    if (!prevCourtIds.has(c.id)) {
      events.push({ id: newId(), ts, kind: 'court_started', label: `court ${c.id} 启动` });
    }
  }
  for (const c of prev.courts) {
    if (!nextCourtIds.has(c.id)) {
      events.push({ id: newId(), ts, kind: 'court_ended', label: `court ${c.id} 结束` });
    }
  }
  const prevPendingIds = new Set(prev.pending.map((p) => p.slug_id));
  const nextPendingIds = new Set(next.pending.map((p) => p.slug_id));
  for (const p of next.pending) {
    if (!prevPendingIds.has(p.slug_id)) {
      events.push({
        id: newId(),
        ts,
        kind: 'pending_new',
        label: `待审批: ${p.repo ?? '?'}#${p.number ?? '?'} (${p.stage ?? ''})`,
      });
    }
  }
  for (const p of prev.pending) {
    if (!nextPendingIds.has(p.slug_id)) {
      events.push({
        id: newId(),
        ts,
        kind: 'pending_resolved',
        label: `已审批: ${p.repo ?? '?'}#${p.number ?? '?'} (${p.stage ?? ''})`,
      });
    }
  }
  if (prev.watcher.alive !== next.watcher.alive) {
    events.push({
      id: newId(),
      ts,
      kind: next.watcher.alive ? 'watcher_up' : 'watcher_down',
      label: next.watcher.alive ? 'watcher 上线' : 'watcher 下线',
    });
  }
  if (prev.receiver.alive !== next.receiver.alive) {
    events.push({
      id: newId(),
      ts,
      kind: next.receiver.alive ? 'receiver_up' : 'receiver_down',
      label: next.receiver.alive ? 'receiver 上线' : 'receiver 下线',
    });
  }
  return events;
}
