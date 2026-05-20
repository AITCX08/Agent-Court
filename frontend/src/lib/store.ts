import { create } from 'zustand';
import type { Status } from './api';

export interface ActivityEvent {
  id: string;
  ts: number;
  kind: 'court_started' | 'court_ended' | 'pending_new' | 'pending_resolved' | 'watcher_up' | 'watcher_down' | 'receiver_up' | 'receiver_down';
  label: string;
}

const ACTIVITY_RING_SIZE = 50;

interface StoreState {
  status: Status | null;
  connected: boolean;
  lastUpdateTs: number;
  activity: ActivityEvent[];
  setStatus: (s: Status) => void;
  setConnected: (c: boolean) => void;
  pushActivity: (events: ActivityEvent[]) => void;
}

export const useStore = create<StoreState>((set) => ({
  status: null,
  connected: false,
  lastUpdateTs: 0,
  activity: [],
  setStatus: (s) => set({ status: s, lastUpdateTs: Date.now() }),
  setConnected: (c) => set({ connected: c }),
  pushActivity: (events) =>
    set((state) => ({
      activity: [...events, ...state.activity].slice(0, ACTIVITY_RING_SIZE),
    })),
}));
