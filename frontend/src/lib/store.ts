import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type { Status, GitBoard, GitBoardScope, AgentTeamsSnapshot, AutoReviewStatusMap } from './api';

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
  // PR-17d: cross-tab persistent caches
  gitBoards: Partial<Record<GitBoardScope, GitBoard>>;
  agentTeams: AgentTeamsSnapshot | null;
  // PR-18e: auto-review status map ("repo#number" -> AutoReviewState)
  autoReviewStates: AutoReviewStatusMap;
  setStatus: (s: Status) => void;
  setConnected: (c: boolean) => void;
  pushActivity: (events: ActivityEvent[]) => void;
  setGitBoard: (scope: GitBoardScope, board: GitBoard) => void;
  setAgentTeams: (snap: AgentTeamsSnapshot) => void;
  setAutoReviewStates: (states: AutoReviewStatusMap) => void;
}

export const useStore = create<StoreState>()(
  persist(
    (set) => ({
      status: null,
      connected: false,
      lastUpdateTs: 0,
      activity: [],
      gitBoards: {},
      agentTeams: null,
      autoReviewStates: {},
      setStatus: (s) => set({ status: s, lastUpdateTs: Date.now() }),
      setConnected: (c) => set({ connected: c }),
      pushActivity: (events) =>
        set((state) => ({
          activity: [...events, ...state.activity].slice(0, ACTIVITY_RING_SIZE),
        })),
      setGitBoard: (scope, board) =>
        set((state) => ({ gitBoards: { ...state.gitBoards, [scope]: board } })),
      setAgentTeams: (snap) => set({ agentTeams: snap }),
      setAutoReviewStates: (states) => set({ autoReviewStates: states }),
    }),
    {
      name: 'court-dashboard-store',
      storage: createJSONStorage(() => sessionStorage),
      // Only persist the data caches, not SSE-driven transient state.
      partialize: (state) => ({
        gitBoards: state.gitBoards,
        agentTeams: state.agentTeams,
        autoReviewStates: state.autoReviewStates,
      }),
    },
  ),
);
