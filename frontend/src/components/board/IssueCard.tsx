import { useState } from 'react';
import { Sparkles, Bot, X as XIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ChipBadge } from './ChipBadge';
import { AutoReviewBadge } from './AutoReviewBadge';
import { useStore } from '../../lib/store';
import { killAgent } from '../../lib/api';
import { useToast } from '../Toast';
import type { BoardCard } from '../../lib/api';

interface Props {
  card: BoardCard;
  onSpawnRequest?: (card: BoardCard) => void;
  onJumpToTeam?: (teamId: string) => void;
  /** PR-19a: 卡片侧关掉 spawn 的 team 成功后回调父刷新看板 */
  onTeamKilled?: () => void;
}

export function IssueCard({ card, onSpawnRequest, onJumpToTeam, onTeamKilled }: Props) {
  const { t } = useTranslation();
  const { push } = useToast();
  // PR-18e: auto-review state 旁路 join (key 形如 "owner/repo#123")
  const autoReviewStates = useStore((s) => s.autoReviewStates);
  const arKey = `${card.repo}#${card.number}`;
  const arState = autoReviewStates[arKey];
  // PR-19a: 卡片侧关闭 agent — 二次确认 + 调 killAgent
  const [confirmingKill, setConfirmingKill] = useState(false);
  const [killing, setKilling] = useState(false);

  const doKill = async () => {
    if (!card.linked_team) return;
    setKilling(true);
    try {
      await killAgent(card.linked_team);
      push({ kind: 'ok', text: t('agents.card.stop_success') });
      setConfirmingKill(false);
      onTeamKilled?.();
    } catch (err) {
      push({ kind: 'err', text: t('agents.card.stop_error', { detail: (err as Error).message }) });
    } finally {
      setKilling(false);
    }
  };

  return (
    <div className="relative shrink-0 w-60 rounded-md bg-bg-card hover:bg-bg-card-hover
                    border border-border-base transition pl-3 pr-3 py-2.5
                    flex flex-col gap-1.5">
      <span className="absolute left-0 top-1.5 bottom-1.5 w-1 rounded-r-full bg-accent-primary" aria-hidden />

      <a
        href={card.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-xs text-fg-primary leading-snug line-clamp-2 hover:underline"
      >
        {card.title}
      </a>

      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 text-[10px] text-fg-muted truncate min-w-0">
          <span className="truncate">{card.repo}</span>
          <span>·</span>
          <span className="text-accent-primary flex-shrink-0">#{card.number}</span>
        </div>
        {card.linked_team ? (
          // PR-19a: Bot + 旁红色 ✕ 关闭 (二次确认)
          confirmingKill ? (
            <div className="flex items-center gap-1 flex-shrink-0">
              <span className="text-[10px] text-fg-secondary">{t('git_board.card.kill_confirm_title')}</span>
              <button
                type="button"
                onClick={doKill}
                disabled={killing}
                className="text-[10px] px-1.5 py-0.5 rounded
                           bg-accent-danger/15 text-accent-danger border border-accent-danger/30
                           hover:bg-accent-danger/25 transition disabled:opacity-40
                           inline-flex items-center gap-1"
              >
                {t('git_board.card.kill_confirm_yes')}
              </button>
              <button
                type="button"
                onClick={() => setConfirmingKill(false)}
                disabled={killing}
                className="text-[10px] px-1.5 py-0.5 rounded text-fg-muted
                           hover:bg-bg-card-hover transition disabled:opacity-40"
              >
                {t('git_board.card.kill_confirm_no')}
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1 flex-shrink-0">
              <button
                type="button"
                onClick={() => onJumpToTeam?.(card.linked_team!)}
                title={t('git_board.card.linked_to_team', { team: card.linked_team })}
                aria-label={t('git_board.card.linked_to_team', { team: card.linked_team })}
                className="text-[10px] px-1.5 py-0.5 rounded
                           bg-accent-purple/15 text-accent-purple border border-accent-purple/30
                           hover:bg-accent-purple/25 transition
                           inline-flex items-center gap-1"
              >
                <Bot className="w-3 h-3" />
              </button>
              <button
                type="button"
                onClick={() => setConfirmingKill(true)}
                title={t('git_board.card.kill_agent')}
                aria-label={t('git_board.card.kill_agent')}
                className="w-5 h-5 rounded
                           text-fg-muted hover:text-accent-danger
                           hover:bg-accent-danger/10 transition
                           inline-flex items-center justify-center"
              >
                <XIcon className="w-3 h-3" />
              </button>
            </div>
          )
        ) : onSpawnRequest ? (
          <button
            type="button"
            onClick={() => onSpawnRequest(card)}
            title={t('git_board.card.spawn_agent')}
            aria-label={t('git_board.card.spawn_agent')}
            className="text-[10px] px-1.5 py-0.5 rounded
                       bg-accent-primary/10 text-accent-primary border border-accent-primary/30
                       hover:bg-accent-primary/20 transition flex-shrink-0
                       inline-flex items-center gap-1"
          >
            <Sparkles className="w-3 h-3" />
          </button>
        ) : null}
      </div>

      {(card.tags.length > 0 || arState) && (
        <div className="flex items-center gap-1 flex-wrap">
          {card.tags.map((tag) => (
            <ChipBadge key={tag} tone={tag === 'open' ? 'open' : 'gray'}>{tag}</ChipBadge>
          ))}
          {arState && <AutoReviewBadge state={arState} />}
        </div>
      )}
    </div>
  );
}
