import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RefreshCw, Loader2 } from 'lucide-react';
import {
  getGitBoard,
  refreshGitBoard,
  type BoardCard,
  type GitBoard,
  type GitBoardColumn,
  type GitBoardScope,
} from '../../lib/api';
import { useToast } from '../Toast';
import { ScopeTabs } from '../board/ScopeTabs';
import { KanbanColumn } from '../board/KanbanColumn';
import { IssueCard } from '../board/IssueCard';

const COLUMN_ORDER: GitBoardColumn[] = ['wip', 'under_review', 'reviewing', 'reviewed'];
const AUTO_REFRESH_MS = 30_000;
const SCOPE_LS_KEY = 'court-board-scope';

function readStoredScope(): GitBoardScope {
  const stored = localStorage.getItem(SCOPE_LS_KEY);
  const valid: GitBoardScope[] = ['related', 'created', 'assigned', 'review', 'participating', 'all'];
  return (valid as string[]).includes(stored ?? '') ? (stored as GitBoardScope) : 'related';
}

export function GitBoardPage() {
  const { t } = useTranslation();
  const { push } = useToast();
  const [scope, setScope] = useState<GitBoardScope>(readStoredScope);
  const [board, setBoard] = useState<GitBoard | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetch = useCallback(async (s: GitBoardScope) => {
    setLoading(true);
    setError(null);
    try {
      const data = await getGitBoard(s);
      setBoard(data);
    } catch (err) {
      const msg = (err as Error).message;
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem(SCOPE_LS_KEY, scope);
    fetch(scope);
    const id = window.setInterval(() => fetch(scope), AUTO_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [scope, fetch]);

  const onRefresh = async () => {
    try {
      await refreshGitBoard(scope);
      await fetch(scope);
    } catch (err) {
      push({ kind: 'err', text: (err as Error).message });
    }
  };

  const onSpawn = (_card: BoardCard) => {
    push({ kind: 'ok', text: t('git_board.spawn_agent_toast') });
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
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
        {COLUMN_ORDER.map((col) => (
          <KanbanColumn
            key={col}
            title={t(`git_board.column.${col}`)}
            cards={board?.columns[col] ?? []}
            emptyText={t('git_board.no_pr')}
            onSpawn={onSpawn}
          />
        ))}
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
              <IssueCard key={`${card.repo}#${card.number}`} card={card} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
