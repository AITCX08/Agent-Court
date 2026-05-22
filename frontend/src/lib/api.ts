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
  method: 'GET' | 'POST' | 'DELETE',
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

// PR-16b: Git Board
export type GitBoardScope =
  | 'related' | 'created' | 'assigned' | 'review' | 'participating' | 'all';
export type GitBoardColumn = 'wip' | 'under_review' | 'reviewing' | 'reviewed';

export interface BoardCard {
  kind: 'pr' | 'issue';
  repo: string;
  number: number;
  title: string;
  state: string;
  tags: string[];
  color_bar: string;
  url: string;
  updated_at: string;
  linked_team: string | null;
}

export interface GitBoard {
  scope: GitBoardScope;
  updated_at: string;
  stale: boolean;
  columns: Record<GitBoardColumn, BoardCard[]>;
  issues_row: BoardCard[];
  error?: string;
}

export function getGitBoard(scope: GitBoardScope): Promise<GitBoard> {
  return call<GitBoard>('GET', `/api/git-board?scope=${encodeURIComponent(scope)}`);
}

export function refreshGitBoard(scope?: GitBoardScope): Promise<{ ok: true }> {
  return call('POST', '/api/git-board/refresh', scope ? { scope } : {});
}

// PR-18e: Auto-review status (旁路注入到 PR/issue 卡片上)
// 后端 SQLite state file 不存在时返回 {} (auto-review 未启用), 前端显示无 badge
export interface AutoReviewState {
  state: 'discovered' | 'queued' | 'running' | 'review_done' | 'posted' | 'failed' | 'dedupe_skipped';
  kind: 'pr' | 'issue';
  runtime: string | null;
  head_sha: string | null;
  last_event_at: string;
  error_message: string | null;
}

export type AutoReviewStatusMap = Record<string, AutoReviewState>;

export function fetchAutoReviewStatus(): Promise<AutoReviewStatusMap> {
  return call<AutoReviewStatusMap>('GET', '/api/auto-review/status');
}

// PR-17a: Agent Teams
export type AgentKind = 'ghostty' | 'tmux';

export interface AgentTeamLink {
  repo: string;
  number: number;
  kind: 'pr' | 'issue';
  url: string;
}

export interface McpSubproc {
  pid: number;
  command: string;
  name: string;
}

export interface AgentPane {
  index: number;
  pid: number;
  command: string;
  started_at: string;
}

export interface AgentTeam {
  id: string;
  kind: AgentKind;
  label: string;
  cli: string;
  pid: number | null;
  started_at: string;
  cwd: string;
  tty: string;
  session: string;
  windows: number;
  panes: AgentPane[];
  mcp_subprocs: McpSubproc[];
  linked: AgentTeamLink | null;
  can_stream: boolean;
  can_stop: boolean;
}

export interface AgentTeamsSnapshot {
  updated_at: string;
  teams: AgentTeam[];
}

export function getAgentTeams(): Promise<AgentTeamsSnapshot> {
  return call<AgentTeamsSnapshot>('GET', '/api/agent-teams');
}

export function setAgentTeamLabel(
  team: Pick<AgentTeam, 'id' | 'cli' | 'started_at'>,
  label: string,
): Promise<{ ok: true }> {
  return call('POST', '/api/agent/team-label', {
    id: team.id,
    label,
    cli: team.cli,
    started_at: team.started_at,
  });
}

export interface SpawnResult {
  ok: true;
  team_id: string;
  session: string;
  already_spawned: boolean;
  linked: AgentTeamLink | null;
}

export function spawnAgent(input: {
  repo: string;
  number: number;
  kind: 'pr' | 'issue';
  url: string;
}): Promise<SpawnResult> {
  return call<SpawnResult>('POST', '/api/agent/spawn', input);
}

export function killAgent(teamId: string): Promise<{ ok: true; team_id: string; session: string }> {
  return call('DELETE', `/api/agent/${encodeURIComponent(teamId)}`, { confirm: true });
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
