// API client: fetch wrapper + token management
// token 首次从 URL ?t= 抽到 localStorage, 然后清掉 URL 防泄漏

const TOKEN_KEY = 'court-dashboard-token';

export interface ProcessInfo {
  alive: boolean;
  pid: number | null;
  port?: number;
}

export interface TmuxSession {
  name: string;
  windows: number;
  attached: boolean;
}

export interface Court {
  id: string;
  window: string;
  window_index: number | null;
  repo: string | null;
  issue: number | null;
  active: boolean | null;
  panes: number | null;
  stage: string | null;
  status: string;
}

export interface Pending {
  slug_id: string;
  repo: string | null;
  number: number | null;
  stage: string | null;
  created_at: string | null;
  channels: string[];
}

export interface OrchestratorRun {
  issue_key: string;
  repo: string;
  number: number;
  state: string;
  stage: string;
  last_action: string;
  winner: string;
  tmux_window: string;
  tmux_window_alive: boolean;
  has_pending_approval: boolean;
  in_retry_queue: boolean;
  retry_attempt: number;
  dispatched_at: string;
}

export interface OrchestratorInconsistency {
  issue_key: string;
  kind: string;
  severity: 'warn' | 'error';
  detail: string;
  suggested_fix: string;
}

export interface OrchestratorView {
  runs: OrchestratorRun[];
  inconsistencies: OrchestratorInconsistency[];
  metrics: Record<string, number>;
  orphan_tmux_windows: string[];
}

export interface Status {
  courts: Court[];
  tmux_sessions: TmuxSession[];
  pending: Pending[];
  seen_issues_count: number;
  watcher: ProcessInfo;
  receiver: ProcessInfo;
  // SY-3 v2 渐进切换: 后端开始把 orchestrator 统一视图塞 /api/status;
  // 老字段保持不变, 此字段可选, 老前端忽略也兼容.
  orchestrator?: OrchestratorView;
  ts: number;
}

export function getToken(): string {
  // 1. URL ?t= 优先 (首次进入)
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get('t');
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl);
    url.searchParams.delete('t');
    window.history.replaceState({}, '', url.toString());
    return fromUrl;
  }
  // 2. localStorage 兜底
  return localStorage.getItem(TOKEN_KEY) ?? '';
}

function authHeaders(): HeadersInit {
  const token = getToken();
  if (!token) return {};
  return { Authorization: `Bearer ${token}` };
}

async function call<T>(
  method: 'GET' | 'POST',
  path: string,
  body?: Record<string, unknown>
): Promise<T> {
  const init: RequestInit = {
    method,
    headers: {
      ...authHeaders(),
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  };
  const res = await fetch(path, init);
  if (!res.ok) {
    const detail = await res.text().catch(() => '');
    throw new ApiError(res.status, detail || res.statusText);
  }
  // 204 / 空 body 容错
  const ct = res.headers.get('content-type') ?? '';
  if (!ct.includes('application/json')) return undefined as unknown as T;
  return (await res.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export function getStatus(): Promise<Status> {
  return call<Status>('GET', '/api/status');
}

export function approve(
  pending: Pick<Pending, 'slug_id' | 'repo' | 'number' | 'stage'>,
  reason = ''
): Promise<{ ok: true; verdict: string; slug_id: string }> {
  return call('POST', '/api/approve', { ...pending, reason });
}

export function reject(
  pending: Pick<Pending, 'slug_id' | 'repo' | 'number' | 'stage'>,
  reason = ''
): Promise<{ ok: true; verdict: string; slug_id: string }> {
  return call('POST', '/api/reject', { ...pending, reason });
}

export function killCourt(window: string): Promise<{ ok: true; killed: string }> {
  return call('POST', '/api/kill', { window, confirm: true });
}

export function sseUrl(): string {
  const token = getToken();
  return `/api/events${token ? `?t=${encodeURIComponent(token)}` : ''}`;
}
