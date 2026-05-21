import { useTranslation } from 'react-i18next';
import type { GitBoardScope } from '../../lib/api';

interface Props {
  active: GitBoardScope;
  onChange: (s: GitBoardScope) => void;
}

const SCOPES: GitBoardScope[] = [
  'related', 'created', 'assigned', 'review', 'participating', 'all',
];

export function ScopeTabs({ active, onChange }: Props) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {SCOPES.map((s) => {
        const isActive = active === s;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onChange(s)}
            className={`px-3 py-1 text-xs rounded-md transition
                        ${isActive
                          ? 'bg-accent-primary text-white'
                          : 'text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover'}`}
          >
            {t(`git_board.scope.${s}`)}
          </button>
        );
      })}
    </div>
  );
}
