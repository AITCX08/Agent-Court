import { Sparkles, Bot } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ChipBadge } from './ChipBadge';
import type { BoardCard } from '../../lib/api';

interface Props {
  card: BoardCard;
  onSpawnRequest?: (card: BoardCard) => void;
  onJumpToTeam?: (teamId: string) => void;
}

export function IssueCard({ card, onSpawnRequest, onJumpToTeam }: Props) {
  const { t } = useTranslation();
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
          <button
            type="button"
            onClick={() => onJumpToTeam?.(card.linked_team!)}
            title={t('git_board.card.linked_to_team', { team: card.linked_team })}
            aria-label={t('git_board.card.linked_to_team', { team: card.linked_team })}
            className="text-[10px] px-1.5 py-0.5 rounded
                       bg-accent-purple/15 text-accent-purple border border-accent-purple/30
                       hover:bg-accent-purple/25 transition flex-shrink-0
                       inline-flex items-center gap-1"
          >
            <Bot className="w-3 h-3" />
          </button>
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

      {card.tags.length > 0 && (
        <div className="flex items-center gap-1 flex-wrap">
          {card.tags.map((tag) => (
            <ChipBadge key={tag} tone={tag === 'open' ? 'open' : 'gray'}>{tag}</ChipBadge>
          ))}
        </div>
      )}
    </div>
  );
}
