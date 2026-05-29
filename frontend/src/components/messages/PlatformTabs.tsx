import { useTranslation } from 'react-i18next';
import type { PlatformInfo } from '../../lib/api';

type Props = {
  platforms: PlatformInfo[];
  active: string | null;        // null = 全部
  onSelect: (p: string | null) => void;
};

export function PlatformTabs({ platforms, active, onSelect }: Props) {
  const { t } = useTranslation();
  if (platforms.length === 0) return null;

  const total = platforms.reduce((s, p) => s + p.count, 0);

  const pill = (
    key: string,
    label: string,
    count: number,
    isActive: boolean,
    onClick: () => void,
  ) => (
    <button
      key={key}
      type="button"
      onClick={onClick}
      className={`px-3 py-1 rounded-full text-[13px] transition whitespace-nowrap
                  ${isActive
                    ? 'bg-accent-primary text-white'
                    : 'text-fg-secondary hover:text-fg-primary'}`}
    >
      {label}
      <span className={`ml-1 text-[11px] ${isActive ? 'text-white/80' : 'text-fg-muted'}`}>
        {count}
      </span>
    </button>
  );

  return (
    <div className="px-4 pt-2.5 pb-0.5 border-b border-border-base bg-bg-base">
      <div className="inline-flex items-center gap-1 p-1 rounded-full bg-bg-card border border-border-base">
        {pill('__all__', t('messages.tab.all'), total, active == null, () => onSelect(null))}
        {platforms.map((p) =>
          pill(
            p.platform,
            t(`messages.platform.${p.platform}`, { defaultValue: p.platform }),
            p.count,
            active === p.platform,
            () => onSelect(p.platform),
          ),
        )}
      </div>
    </div>
  );
}
