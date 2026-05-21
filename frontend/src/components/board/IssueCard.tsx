import { ChipBadge } from './ChipBadge';
import type { BoardCard } from '../../lib/api';

interface Props {
  card: BoardCard;
}

export function IssueCard({ card }: Props) {
  return (
    <a
      href={card.url}
      target="_blank"
      rel="noopener noreferrer"
      className="relative shrink-0 w-60 rounded-md bg-bg-card hover:bg-bg-card-hover
                 border border-border-base transition pl-3 pr-3 py-2.5
                 flex flex-col gap-1.5"
    >
      {/* 左蓝色条 (issue 固定) */}
      <span className="absolute left-0 top-1.5 bottom-1.5 w-1 rounded-r-full bg-accent-primary" aria-hidden />

      <div className="text-xs text-fg-primary leading-snug line-clamp-2">
        {card.title}
      </div>

      <div className="flex items-center gap-1 text-[10px] text-fg-muted truncate">
        <span className="truncate">{card.repo}</span>
        <span>·</span>
        <span className="text-accent-primary flex-shrink-0">#{card.number}</span>
      </div>

      {card.tags.length > 0 && (
        <div className="flex items-center gap-1 flex-wrap">
          {card.tags.map((tag) => (
            <ChipBadge key={tag} tone={tag === 'open' ? 'open' : 'gray'}>{tag}</ChipBadge>
          ))}
        </div>
      )}
    </a>
  );
}
