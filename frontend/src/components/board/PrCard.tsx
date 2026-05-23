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

const COLOR_BAR_CLASS: Record<string, string> = {
  purple: 'bg-accent-purple',
  orange: 'bg-accent-warn',
  blue: 'bg-accent-primary',
  gray: 'bg-fg-muted',
};

export function PrCard({ card, onSpawnRequest, onJumpToTeam, onTeamKilled }: Props) {
  const { t } = useTranslation();
  const { push } = useToast();
  const barCls = COLOR_BAR_CLASS[card.color_bar] || COLOR_BAR_CLASS.gray;
  // PR-18e: auto-review state 旁路 join (key 形如 "owner/repo#123")
  const autoReviewStates = useStore((s) => s.autoReviewStates);
  const arKey = `${card.repo}#${card.number}`;
  const arState = autoReviewStates[arKey];
  // PR-19a: 卡片侧关闭 agent — 二次确认 + 调 killAgent
  const [confirmingKill, setConfirmingKill] = useState(false);
  const [killing, setKilling] = useState(false);

  const stop = (e: React.MouseEvent | React.SyntheticEvent) => {
    e.stopPropagation();
    e.preventDefault();
  };

  const doKill = async (e: React.MouseEvent) => {
    stop(e);
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

  // 整卡当 <a> — 任何位置点击都跳; 内部按钮 stopPropagation+preventDefault
  // 防止冒泡触发跳转. <a> 里嵌 <button> 浏览器实测能正常工作 (button 接管点击).
  return (
    <a
      href={card.url}
      target="_blank"
      rel="noopener noreferrer"
      className="relative block rounded-md bg-bg-card hover:bg-bg-card-hover
                 border border-border-base transition pl-3 pr-3 py-2.5
                 flex flex-col gap-1.5 no-underline"
    >
      {/* 左色条 */}
      <span className={`absolute left-0 top-1.5 bottom-1.5 w-1 rounded-r-full ${barCls}`} aria-hidden />

      <div className="text-xs text-fg-primary leading-snug line-clamp-2">
        {card.title}
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1 text-[10px] text-fg-muted truncate min-w-0">
          <span className="truncate">{card.repo}</span>
          <span>·</span>
          <span className="text-accent-primary flex-shrink-0">#{card.number}</span>
        </div>
        {card.linked_team ? (
          // PR-17a: Bot 按钮跳转; PR-19a: 旁加红色 ✕ 关闭 (二次确认)
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
                onClick={(e) => { stop(e); setConfirmingKill(false); }}
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
                onClick={(e) => {
                  stop(e);
                  onJumpToTeam?.(card.linked_team!);
                }}
                title={t('git_board.card.linked_to_team', { team: card.linked_team })}
                aria-label={t('git_board.card.linked_to_team', { team: card.linked_team })}
                className="text-[10px] px-1.5 py-0.5 rounded
                           bg-accent-purple/15 text-accent-purple border border-accent-purple/30
                           hover:bg-accent-purple/25 transition
                           inline-flex items-center gap-1"
              >
                <Bot className="w-3 h-3" />
                <span className="truncate max-w-[100px]">{card.linked_team.replace('agent-team-', '')}</span>
              </button>
              <button
                type="button"
                onClick={(e) => { stop(e); setConfirmingKill(true); }}
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
        ) : (card.state !== 'closed' && onSpawnRequest) ? (
          <button
            type="button"
            onClick={(e) => {
              stop(e);
              onSpawnRequest(card);
            }}
            title={t('git_board.card.spawn_agent')}
            aria-label={t('git_board.card.spawn_agent')}
            className="text-[10px] px-1.5 py-0.5 rounded
                       bg-accent-primary/10 text-accent-primary border border-accent-primary/30
                       hover:bg-accent-primary/20 transition flex-shrink-0
                       inline-flex items-center gap-1"
          >
            <Sparkles className="w-3 h-3" />
            <span>{t('git_board.card.spawn_agent')}</span>
          </button>
        ) : null}
      </div>

      {(card.tags.length > 0 || arState) && (
        <div className="flex items-center gap-1 flex-wrap">
          {card.tags.map((tag) => {
            const tone = tag === 'open' ? 'open'
              : tag === 'review' ? 'review'
              : tag === 'wip' ? 'wip'
              : 'gray';
            const labelKey = `git_board.card.${tag}`;
            return (
              <ChipBadge key={tag} tone={tone}>{t(labelKey, { defaultValue: tag })}</ChipBadge>
            );
          })}
          {arState && <AutoReviewBadge state={arState} />}
        </div>
      )}
    </a>
  );
}
