import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RefreshCw, Loader2 } from 'lucide-react';
import {
  getGitBoard,
  refreshGitBoard,
  spawnAgent,
  fetchAutoReviewStatus,
  type BoardCard,
  type GitBoardColumn,
  type GitBoardScope,
} from '../../lib/api';
import { useStore } from '../../lib/store';
import { useToast } from '../Toast';
import { ScopeTabs } from '../board/ScopeTabs';
import { KanbanColumn } from '../board/KanbanColumn';
import { IssueCard } from '../board/IssueCard';

const COLUMN_ORDER: GitBoardColumn[] = ['reviewed', 'reviewing', 'under_review', 'wip'];
const AUTO_REFRESH_MS = 60_000;
const SCOPE_LS_KEY = 'court-board-scope';

function readStoredScope(): GitBoardScope {
  const stored = localStorage.getItem(SCOPE_LS_KEY);
  const valid: GitBoardScope[] = ['related', 'created', 'assigned', 'review', 'participating', 'all'];
  return (valid as string[]).includes(stored ?? '') ? (stored as GitBoardScope) : 'all';
}

export function GitBoardPage() {
  const { t } = useTranslation();
  const { push } = useToast();
  const [scope, setScope] = useState<GitBoardScope>(readStoredScope);
  const boards = useStore((s) => s.gitBoards);
  const setBoardForScope = useStore((s) => s.setGitBoard);
  const setAutoReviewStates = useStore((s) => s.setAutoReviewStates);
  const board = boards[scope] ?? null;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inflightCancelledRef = useRef<{ cancelled: boolean }>({ cancelled: false });

  const fetchFor = useCallback(async (s: GitBoardScope) => {
    const token = { cancelled: false };
    inflightCancelledRef.current.cancelled = true;
    inflightCancelledRef.current = token;
    setLoading(true);
    setError(null);
    try {
      const data = await getGitBoard(s);
      if (token.cancelled) return;
      setBoardForScope(s, data);
    } catch (err) {
      if (token.cancelled) return;
      setError((err as Error).message);
    } finally {
      if (!token.cancelled) setLoading(false);
    }
  }, [setBoardForScope]);

  useEffect(() => {
    localStorage.setItem(SCOPE_LS_KEY, scope);
    fetchFor(scope);
    // PR-18e: 并行拉 auto-review 状态 (旁路注入到卡片), 失败静默 — 后端未启用时返回 {}
    fetchAutoReviewStatus()
      .then(setAutoReviewStates)
      .catch(() => { /* auto_review optional, swallow */ });
    const id = window.setInterval(() => {
      fetchFor(scope);
      fetchAutoReviewStatus()
        .then(setAutoReviewStates)
        .catch(() => { /* swallow */ });
    }, AUTO_REFRESH_MS);
    return () => {
      inflightCancelledRef.current.cancelled = true;
      window.clearInterval(id);
    };
  }, [scope, fetchFor, setAutoReviewStates]);

  const onRefresh = async () => {
    try {
      await refreshGitBoard(scope);
      await fetchFor(scope);
    } catch (err) {
      push({ kind: 'err', text: (err as Error).message });
    }
  };

  const onSpawnRequest = async (card: BoardCard) => {
    try {
      const result = await spawnAgent({
        repo: card.repo,
        number: card.number,
        kind: card.kind,
        url: card.url,
      });
      if (result.already_spawned) {
        push({ kind: 'ok', text: t('git_board.card.spawn_already') });
        return;
      }
      push({ kind: 'ok', text: t('git_board.card.spawn_success', { team: result.team_id }) });
      // new spawn → linked_team will appear on next snapshot, refresh to pull it
      await refreshGitBoard(scope);
      await fetchFor(scope);
    } catch (err) {
      push({ kind: 'err', text: t('git_board.card.spawn_error', { detail: (err as Error).message }) });
    }
  };

  const onJumpToTeam = (_teamId: string) => {
    window.location.hash = '/agents';
  };

  return (
    <div className="p-5 flex flex-col gap-4 min-h-full">
      {/* Scope + Refresh 行 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <ScopeTabs active={scope} onChange={setScope} />
        <div className="flex items-center gap-3">
          {board?.stale && (
            <span className="text-[10px] px-2 py-0.5 rounded-full border
                             bg-accent-warn/10 text-accent-warn border-accent-warn/30">
              {t('git_board.stale_tag')}
            </span>
          )}
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            title={t('common.refresh')}
            aria-label={t('common.refresh')}
            className="w-8 h-8 inline-flex items-center justify-center rounded-md
                       text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover
                       transition disabled:opacity-40"
          >
            {loading
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <RefreshCw className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {/* 错误 banner */}
      {error && (
        <div className="rounded-md bg-accent-danger/10 border border-accent-danger/30
                        text-accent-danger text-xs px-3 py-2 flex items-center gap-2">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
          <span className="flex-1 truncate">
            {t('git_board.fetch_error', { detail: error })}
          </span>
        </div>
      )}

      {/* 4 列 Kanban */}
      <div className="relative">
        {loading && !board && (
          <div className="absolute inset-0 flex items-center justify-center
                          pointer-events-none z-10">
            <div className="text-xs text-fg-muted bg-bg-card/80 px-3 py-1.5
                            rounded-md border border-border-base flex items-center gap-2">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              {t('common.loading')}
            </div>
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          {COLUMN_ORDER.map((col) => (
            <KanbanColumn
              key={col}
              title={t(`git_board.column.${col}`)}
              cards={board?.columns[col] ?? []}
              emptyText={t('git_board.no_pr')}
              onSpawnRequest={onSpawnRequest}
              onJumpToTeam={onJumpToTeam}
            />
          ))}
        </div>
      </div>

      {/* Issues 行 */}
      <section className="flex flex-col gap-2 mt-2">
        <h2 className="text-xs text-fg-secondary px-1">
          {t('git_board.issues_section')}
          <span className="ml-2 text-fg-muted">({board?.issues_row.length ?? 0})</span>
        </h2>
        {(board?.issues_row.length ?? 0) === 0 ? (
          <div className="text-[11px] text-fg-muted px-1">
            {t('git_board.no_issues')}
          </div>
        ) : (
          <div className="flex gap-2 overflow-x-auto pb-2 pl-1 pr-1">
            {board?.issues_row.map((card) => (
              <IssueCard
                key={`${card.repo}#${card.number}`}
                card={card}
                onSpawnRequest={onSpawnRequest}
                onJumpToTeam={onJumpToTeam}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
