import { useTranslation } from 'react-i18next';
import { Search, X } from 'lucide-react';

type Props = {
  value: string;
  onChange: (v: string) => void;
};

export function MessageSearchBar({ value, onChange }: Props) {
  const { t } = useTranslation();
  return (
    <div className="px-4 py-2.5 border-b border-border-base bg-bg-base">
      <div className="relative">
        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-fg-muted" />
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={t('messages.tab.search_placeholder')}
          className="w-full pl-8 pr-8 py-1.5 text-[13px] rounded-lg
                     bg-bg-card border border-border-base text-fg-primary
                     placeholder:text-fg-muted focus:outline-none
                     focus:border-accent-primary transition"
        />
        {value && (
          <button
            type="button"
            onClick={() => onChange('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-fg-muted
                       hover:text-fg-primary"
            aria-label={t('common.cancel')}
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
