import type { ReactNode } from 'react';
import type { BoardCard } from '../../lib/api';
import { PrCard } from './PrCard';

interface Props {
  title: ReactNode;
  cards: BoardCard[];
  emptyText: string;
  onSpawn?: (card: BoardCard) => void;
}

export function KanbanColumn({ title, cards, emptyText, onSpawn }: Props) {
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
            <PrCard key={`${c.repo}#${c.number}`} card={c} onSpawn={onSpawn} />
          ))
        )}
      </div>
    </div>
  );
}
