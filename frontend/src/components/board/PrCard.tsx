import { Rocket } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ChipBadge } from './ChipBadge';
import type { BoardCard } from '../../lib/api';

interface Props {
  card: BoardCard;
  onSpawn?: (card: BoardCard) => void;
}

const COLOR_BAR_CLASS: Record<string, string> = {
  purple: 'bg-accent-purple',
  orange: 'bg-accent-warn',
  blue: 'bg-accent-primary',
  gray: 'bg-fg-muted',
};

export function PrCard({ card, onSpawn }: Props) {
  const { t } = useTranslation();
  const barCls = COLOR_BAR_CLASS[card.color_bar] || COLOR_BAR_CLASS.gray;

  // 整卡当 <a> — 任何位置点击都跳; 内部 🚀 按钮 stopPropagation+preventDefault
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
        {onSpawn && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
              onSpawn(card);
            }}
            title={t('git_board.card.spawn_agent')}
            aria-label={t('git_board.card.spawn_agent')}
            className="text-fg-muted hover:text-accent-purple transition
                       flex-shrink-0 w-5 h-5 flex items-center justify-center"
          >
            <Rocket className="w-3 h-3" />
          </button>
        )}
      </div>

      {card.tags.length > 0 && (
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
        </div>
      )}
    </a>
  );
}
