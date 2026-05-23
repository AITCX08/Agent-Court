import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, RefreshCw, Loader2, Bot, Plus } from 'lucide-react';
import { getAgentTeams } from '../../lib/api';
import { useStore } from '../../lib/store';
import { AgentTeamCard } from '../agents/AgentTeamCard';
import { NewAgentModal } from '../modals/NewAgentModal';

const AUTO_REFRESH_MS = 5_000;

// PR-19a: 从 location.hash (#/agents?focus=<teamId>) 解析 focus 目标 team
function readFocusTeamFromHash(): string | null {
  const hash = window.location.hash || '';
  const qIdx = hash.indexOf('?');
  if (qIdx < 0) return null;
  const params = new URLSearchParams(hash.slice(qIdx + 1));
  return params.get('focus');
}

export function AgentsPage() {
  const { t } = useTranslation();
  const snap = useStore((s) => s.agentTeams);
  const setAgentTeams = useStore((s) => s.setAgentTeams);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // PR-19a: 跳转高亮目标 team — hash 变化时重读
  const [focusTeamId, setFocusTeamId] = useState<string | null>(readFocusTeamFromHash);
  // PR-19b-2: 启动新 agent modal 开关
  const [modalOpen, setModalOpen] = useState(false);

  useEffect(() => {
    const onHashChange = () => setFocusTeamId(readFocusTeamFromHash());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const fetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getAgentTeams();
      setAgentTeams(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [setAgentTeams]);

  useEffect(() => {
    fetch();
    const id = window.setInterval(fetch, AUTO_REFRESH_MS);
    return () => window.clearInterval(id);
  }, [fetch]);

  const allTeams = snap?.teams ?? [];
  // PR-19d: 按 kind 分组 (tab 切换)
  type AgentTab = 'all' | 'tmux' | 'ghostty';
  const [activeTab, setActiveTab] = useState<AgentTab>('all');
  const tmuxTeams = allTeams.filter((tm) => tm.kind === 'tmux');
  const ghosttyTeams = allTeams.filter((tm) => tm.kind === 'ghostty');
  const teams =
    activeTab === 'tmux' ? tmuxTeams :
    activeTab === 'ghostty' ? ghosttyTeams :
    allTeams;

  return (
    <div className="p-5 flex flex-col gap-4 min-h-full">
      {/* Header: count + subtitle + refresh */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-lg bg-accent-purple/15 text-accent-purple
                          flex items-center justify-center flex-shrink-0">
            <Bot className="w-5 h-5" />
          </div>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-fg-primary">
              {t('agents.title_count')}
              <span className="ml-2 text-fg-muted text-sm font-normal">({teams.length})</span>
            </h2>
            <p className="text-xs text-fg-secondary leading-relaxed mt-0.5 max-w-3xl">
              {t('agents.subtitle')}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* PR-19b-2: 启动新 agent 按钮 */}
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="text-xs px-3 py-1.5 rounded-md
                       bg-accent-purple/15 text-accent-purple border border-accent-purple/30
                       hover:bg-accent-purple/25 transition
                       inline-flex items-center gap-1.5"
          >
            <Plus className="w-3.5 h-3.5" />
            {t('agents.new_agent_button')}
          </button>
          <button
            type="button"
            onClick={fetch}
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

      {/* PR-19d: tab 行 — 全部 / Dashboard 启动 (tmux) / 本地终端 (ghostty) */}
      <div className="flex items-center gap-1 border-b border-border-base">
        {(
          [
            ['all', t('agents.tab_all'), allTeams.length],
            ['tmux', t('agents.tab_tmux'), tmuxTeams.length],
            ['ghostty', t('agents.tab_ghostty'), ghosttyTeams.length],
          ] as const
        ).map(([key, label, count]) => (
          <button
            key={key}
            type="button"
            onClick={() => setActiveTab(key as AgentTab)}
            className={`text-xs px-3 py-1.5 -mb-px border-b-2 transition inline-flex items-center gap-1.5
                       ${activeTab === key
                         ? 'border-accent-primary text-fg-primary font-medium'
                         : 'border-transparent text-fg-muted hover:text-fg-secondary'}`}
          >
            {label}
            <span className="text-fg-muted text-[10px]">({count})</span>
          </button>
        ))}
      </div>

      {/* 错误 banner */}
      {error && (
        <div className="rounded-md bg-accent-danger/10 border border-accent-danger/30
                        text-accent-danger text-xs px-3 py-2 flex items-center gap-2">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
          <span className="flex-1 truncate">
            {t('agents.fetch_error', { detail: error })}
          </span>
        </div>
      )}

      {/* Teams grid */}
      {teams.length === 0 && !loading && !error ? (
        <div className="rounded-lg bg-bg-card border border-border-base p-8 text-center">
          <p className="text-sm text-fg-muted leading-relaxed max-w-md mx-auto">
            {t('agents.empty')}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {teams.map((team) => (
            <AgentTeamCard
              key={team.id}
              team={team}
              onLabelSaved={fetch}
              onTeamKilled={fetch}
              highlighted={team.id === focusTeamId}
            />
          ))}
        </div>
      )}

      {/* PR-19b-2: 启动新 agent modal */}
      <NewAgentModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSpawned={() => fetch()}
      />
    </div>
  );
}
