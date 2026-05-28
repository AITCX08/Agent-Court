import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { DateBucket } from '../../lib/messageGrouping';

type Props = {
  bucket: DateBucket;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
};

export function DateGroupHeader({ bucket, count, collapsed, onToggle }: Props) {
  const { t } = useTranslation();
  const Chevron = collapsed ? ChevronRight : ChevronDown;
  return (
    <button
      type="button"
      onClick={onToggle}
      className="sticky top-0 z-10 w-full flex items-center gap-2 px-4 py-1.5
                 bg-bg-base/95 backdrop-blur border-b border-border-base
                 text-[11px] font-medium text-fg-muted uppercase tracking-wide
                 hover:text-fg-secondary transition"
    >
      <Chevron className="w-3 h-3" />
      <span>{t(`messages.tab.bucket.${bucket}`)}</span>
      <span className="text-fg-muted/60">({count})</span>
    </button>
  );
}
