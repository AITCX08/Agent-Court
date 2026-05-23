import type { ReactNode } from 'react';
import type { BoardCard } from '../../lib/api';
import { PrCard } from './PrCard';

interface Props {
  title: ReactNode;
  cards: BoardCard[];
  emptyText: string;
  onSpawnRequest?: (card: BoardCard) => void;
  onJumpToTeam?: (teamId: string) => void;
  /** PR-19a: 卡片侧关 agent 后回调父刷新看板 */
  onTeamKilled?: () => void;
}

export function KanbanColumn({ title, cards, emptyText, onSpawnRequest, onJumpToTeam, onTeamKilled }: Props) {
  return (
    <div className="flex flex-col rounded-lg bg-bg-base border border-border-base min-w-0">
      <header className="flex items-center justify-between px-3 py-2 border-b border-border-base">
        <h3 className="text-xs font-medium text-fg-secondary">{title}</h3>
        <span className="text-[10px] text-fg-muted">{cards.length}</span>
      </header>
      <div className="flex flex-col gap-2 p-2 max-h-[60vh] overflow-y-auto">
        {cards.length === 0 ? (
          <div className="text-[11px] text-fg-muted text-center py-6">
            {emptyText}
          </div>
        ) : (
          cards.map((c) => (
            <PrCard
              key={`${c.repo}#${c.number}`}
              card={c}
              onSpawnRequest={onSpawnRequest}
              onJumpToTeam={onJumpToTeam}
              onTeamKilled={onTeamKilled}
            />
          ))
        )}
      </div>
    </div>
  );
}
