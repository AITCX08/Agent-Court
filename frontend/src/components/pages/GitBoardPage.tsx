import { LayoutGrid } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export function GitBoardPage() {
  const { t } = useTranslation();
  return (
    <div className="p-8 max-w-3xl">
      <PlaceholderCard
        Icon={LayoutGrid}
        title={t('git_board.placeholder_title')}
        desc={t('git_board.placeholder_desc')}
        tag={t('common.coming_soon')}
      />
    </div>
  );
}

interface PlaceholderCardProps {
  Icon: typeof LayoutGrid;
  title: string;
  desc: string;
  tag: string;
}

function PlaceholderCard({ Icon, title, desc, tag }: PlaceholderCardProps) {
  return (
    <div className="rounded-xl bg-bg-card border border-border-base p-8">
      <div className="flex items-start gap-4">
        <div className="w-10 h-10 rounded-lg bg-accent-primary/15 text-accent-primary
                        flex items-center justify-center flex-shrink-0">
          <Icon className="w-5 h-5" />
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <h2 className="text-base font-semibold text-fg-primary">{title}</h2>
            <span className="text-[10px] px-1.5 py-0.5 rounded
                             bg-accent-warn/15 text-accent-warn border border-accent-warn/30">
              {tag}
            </span>
          </div>
          <p className="text-sm text-fg-secondary leading-relaxed">{desc}</p>
        </div>
      </div>
    </div>
  );
}
