import { useState } from 'react';
import { Terminal, ServerCog, Pencil, Check, X, Cpu, ExternalLink, Square } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AgentTeam } from '../../lib/api';
import { setAgentTeamLabel, killAgent } from '../../lib/api';
import { useToast } from '../Toast';

interface Props {
  team: AgentTeam;
  onLabelSaved?: () => void;
  onTeamKilled?: () => void;
}

function formatStartedAt(iso: string): string {
  if (!iso) return '--';
  // 输入 "2026-05-21T10:19:37" (本地时间, 无 tz). Date(...) 把它当本地时间解析正好.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { hour12: false });
}

export function AgentTeamCard({ team, onLabelSaved, onTeamKilled }: Props) {
  const { t } = useTranslation();
  const { push } = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(team.label);
  const [saving, setSaving] = useState(false);
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [stopping, setStopping] = useState(false);

  const isGhostty = team.kind === 'ghostty';
  const KindIcon = isGhostty ? Terminal : ServerCog;
  const kindLabel = isGhostty ? t('agents.card.kind_ghostty') : t('agents.card.kind_tmux');

  const startEdit = () => {
    setDraft(team.label);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setDraft(team.label);
  };

  const doStop = async () => {
    setStopping(true);
    try {
      await killAgent(team.id);
      push({ kind: 'ok', text: t('agents.card.stop_success') });
      setConfirmingStop(false);
      onTeamKilled?.();
    } catch (err) {
      push({ kind: 'err', text: t('agents.card.stop_error', { detail: (err as Error).message }) });
    } finally {
      setStopping(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await setAgentTeamLabel(team, draft.trim());
      push({ kind: 'ok', text: t('agents.card.label_saved') });
      setEditing(false);
      onLabelSaved?.();
    } catch (err) {
      push({ kind: 'err', text: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg bg-bg-card border border-border-base p-4 flex flex-col gap-3">
      {/* Header: kind icon + label/edit + chip */}
      <div className="flex items-start gap-3">
        <div className={`w-9 h-9 rounded-md flex items-center justify-center flex-shrink-0 ${
          isGhostty
            ? 'bg-accent-primary/15 text-accent-primary'
            : 'bg-accent-purple/15 text-accent-purple'
        }`}>
          <KindIcon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          {editing ? (
            <div className="flex items-center gap-1.5">
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') save();
                  if (e.key === 'Escape') cancelEdit();
                }}
                placeholder={t('agents.card.label_placeholder')}
                autoFocus
                className="flex-1 bg-bg-base border border-border-strong rounded-md
                           px-2 py-1 text-sm text-fg-primary placeholder:text-fg-muted
                           focus:outline-none focus:border-accent-primary/60"
              />
              <button
                type="button"
                onClick={save}
                disabled={saving}
                title={t('agents.card.label_save')}
                aria-label={t('agents.card.label_save')}
                className="w-7 h-7 inline-flex items-center justify-center rounded-md
                           text-accent-success hover:bg-accent-success/15 transition
                           disabled:opacity-40"
              >
                <Check className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                disabled={saving}
                title={t('agents.card.label_cancel')}
                aria-label={t('agents.card.label_cancel')}
                className="w-7 h-7 inline-flex items-center justify-center rounded-md
                           text-fg-muted hover:bg-bg-card-hover transition
                           disabled:opacity-40"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1.5">
              {team.label ? (
                <span className="text-sm text-fg-primary font-medium truncate">{team.label}</span>
              ) : (
                <span className="text-sm text-fg-muted italic truncate">{t('agents.card.label_placeholder')}</span>
              )}
              <button
                type="button"
                onClick={startEdit}
                title={t('agents.card.label_edit')}
                aria-label={t('agents.card.label_edit')}
                className="text-fg-muted hover:text-fg-primary transition
                           w-5 h-5 inline-flex items-center justify-center flex-shrink-0"
              >
                <Pencil className="w-3 h-3" />
              </button>
            </div>
          )}
          <div className="text-[10px] text-fg-muted mt-0.5 flex items-center gap-1.5 flex-wrap">
            <span className="px-1.5 py-0.5 rounded bg-bg-base border border-border-base">
              {kindLabel}
            </span>
            <span>{team.cli}</span>
          </div>
          {team.linked && (
            <a
              href={team.linked.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-accent-primary hover:underline
                         flex items-center gap-1 mt-1"
            >
              <ExternalLink className="w-3 h-3" />
              {t('agents.card.linked_target', {
                repo: team.linked.repo,
                number: team.linked.number,
              })}
            </a>
          )}
        </div>
      </div>

      {/* Body: identity (tty/session) + pid + started */}
      <div className="text-[11px] text-fg-secondary space-y-0.5 leading-snug pl-12">
        {isGhostty ? (
          <div className="flex items-center gap-3 text-fg-muted">
            <span>tty</span>
            <code className="text-fg-secondary">{team.tty || '--'}</code>
            <span className="text-border-strong">·</span>
            <span>{t('agents.card.pid_short')}</span>
            <code className="text-fg-secondary">{team.pid ?? '--'}</code>
          </div>
        ) : (
          <div className="flex items-center gap-3 text-fg-muted">
            <span>{t('agents.card.session_short')}</span>
            <code className="text-fg-secondary truncate">{team.session}</code>
            <span className="text-border-strong">·</span>
            <span>{t('agents.card.windows', { count: team.windows })}</span>
          </div>
        )}
        <div className="text-fg-muted">
          {t('agents.card.started_at')}: <span className="text-fg-secondary">{formatStartedAt(team.started_at)}</span>
        </div>
      </div>

      {/* MCP subprocs (ghostty only) */}
      {isGhostty && (
        <div className="text-[11px] pl-12">
          <div className="text-fg-muted mb-1">{t('agents.card.subprocs')}</div>
          {team.mcp_subprocs.length === 0 ? (
            <div className="text-fg-muted/70 italic">{t('agents.card.no_subprocs')}</div>
          ) : (
            <ul className="flex flex-wrap gap-1.5">
              {team.mcp_subprocs.map((sp) => (
                <li
                  key={sp.pid}
                  title={sp.command}
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded
                             bg-bg-base border border-border-base text-fg-secondary"
                >
                  <Cpu className="w-2.5 h-2.5 text-accent-purple" />
                  <span className="truncate max-w-[140px]">{sp.name}</span>
                  <span className="text-fg-muted">{sp.pid}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Stop button (tmux) or hint (ghostty) */}
      <div className="pt-2 mt-1 border-t border-border-base/50 flex justify-end">
        {team.kind === 'tmux' ? (
          confirmingStop ? (
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-fg-secondary">{t('agents.card.stop_confirm_title')}</span>
              <button
                type="button"
                onClick={doStop}
                disabled={stopping}
                className="text-[11px] px-2 py-1 rounded
                           bg-accent-danger/15 text-accent-danger border border-accent-danger/30
                           hover:bg-accent-danger/25 transition disabled:opacity-40
                           inline-flex items-center gap-1"
              >
                <Square className="w-3 h-3" />
                {t('agents.card.stop_confirm_yes')}
              </button>
              <button
                type="button"
                onClick={() => setConfirmingStop(false)}
                disabled={stopping}
                className="text-[11px] px-2 py-1 rounded text-fg-muted
                           hover:bg-bg-card-hover transition disabled:opacity-40"
              >
                {t('agents.card.stop_confirm_no')}
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingStop(true)}
              title={t('agents.card.stop')}
              aria-label={t('agents.card.stop')}
              className="text-[11px] px-2 py-1 rounded
                         text-fg-muted hover:text-accent-danger
                         hover:bg-accent-danger/10 transition
                         inline-flex items-center gap-1"
            >
              <Square className="w-3 h-3" />
              {t('agents.card.stop')}
            </button>
          )
        ) : (
          <span className="text-[10px] text-fg-muted italic">
            {t('agents.card.stop_ghostty_hint')}
          </span>
        )}
      </div>
    </div>
  );
}
