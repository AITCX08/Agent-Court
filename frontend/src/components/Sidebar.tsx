import { useState, useEffect } from 'react';
import { LayoutGrid, Bot, Activity, MessageSquare, ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { Route } from '../lib/router';
import { usePlatforms } from '../lib/usePlatforms';

const COLLAPSED_KEY = 'court-sidebar-collapsed';

interface Props {
  route: Route;
  platform: string | null;
  onNavigate: (r: Route, sub?: string) => void;
}

interface MenuItem {
  route: Route;
  labelKey: string;
  Icon: typeof LayoutGrid;
}

const MENU: MenuItem[] = [
  { route: '/git-board', labelKey: 'sidebar.menu.git_board', Icon: LayoutGrid },
  { route: '/agents', labelKey: 'sidebar.menu.agents', Icon: Bot },
  { route: '/messages', labelKey: 'sidebar.menu.messages', Icon: MessageSquare },
  { route: '/court-runtime', labelKey: 'sidebar.menu.court_runtime', Icon: Activity },
];

export function Sidebar({ route, platform, onNavigate }: Props) {
  const { t } = useTranslation();
  const platforms = usePlatforms();
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem(COLLAPSED_KEY) === '1'
  );

  useEffect(() => {
    localStorage.setItem(COLLAPSED_KEY, collapsed ? '1' : '0');
  }, [collapsed]);

  const width = collapsed ? 'w-14' : 'w-60';

  return (
    <aside
      className={`${width} transition-[width] duration-200 ease-out
                 flex-shrink-0 bg-bg-sidebar border-r border-border-base
                 flex flex-col`}
    >
      {/* Logo / brand + 折叠按钮 */}
      <div className="h-14 flex items-center px-3 border-b border-border-base gap-2">
        <div className="w-8 h-8 rounded-md bg-accent-primary/15 text-accent-primary
                        flex items-center justify-center text-sm font-semibold flex-shrink-0">
          AC
        </div>
        {!collapsed && (
          <div className="ml-1 text-sm text-fg-primary font-medium truncate flex-1">
            agent-court
          </div>
        )}
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? t('sidebar.expand') : t('sidebar.collapse')}
          aria-label={collapsed ? t('sidebar.expand') : t('sidebar.collapse')}
          className="w-6 h-6 inline-flex items-center justify-center rounded-md
                     text-fg-muted hover:text-fg-primary hover:bg-bg-card-hover
                     transition flex-shrink-0"
        >
          {collapsed ? <ChevronRight className="w-3.5 h-3.5" /> : <ChevronLeft className="w-3.5 h-3.5" />}
        </button>
      </div>

      {/* Menu */}
      <nav className="flex-1 px-2 py-3 space-y-1 overflow-y-auto">
        {MENU.map(({ route: r, labelKey, Icon }) => {
          const isMessages = r === '/messages';
          // /messages 父项: 仅当无 platform (= 全部) 时高亮
          const active = route === r && (!isMessages || platform == null);
          return (
            <div key={r}>
              <button
                type="button"
                onClick={() => onNavigate(r)}
                title={collapsed ? t(labelKey) : undefined}
                className={`w-full flex items-center rounded-md px-2.5 py-2
                            text-sm transition relative
                            ${active
                              ? 'bg-bg-card text-fg-primary'
                              : 'text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover'}`}
              >
                {/* 左色条 */}
                <span
                  className={`absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r-full transition
                              ${active ? 'bg-accent-primary' : 'bg-transparent'}`}
                  aria-hidden
                />
                <Icon className="w-4 h-4 flex-shrink-0" />
                {!collapsed && (
                  <span className="ml-3 truncate">{t(labelKey)}</span>
                )}
              </button>

              {/* 通信下: 动态平台子入口 (展开态才显示) */}
              {isMessages && !collapsed && platforms.map((p) => {
                const pActive = route === '/messages' && platform === p.platform;
                return (
                  <button
                    key={p.platform}
                    type="button"
                    onClick={() => onNavigate('/messages', p.platform)}
                    className={`w-full flex items-center rounded-md pl-9 pr-2.5 py-1.5
                                text-[13px] transition mt-0.5
                                ${pActive
                                  ? 'bg-bg-card text-fg-primary'
                                  : 'text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover'}`}
                  >
                    <span className="truncate flex-1 text-left">
                      {t(`messages.platform.${p.platform}`, { defaultValue: p.platform })}
                    </span>
                    <span className="ml-2 text-[10px] text-fg-muted flex-shrink-0">{p.count}</span>
                  </button>
                );
              })}
            </div>
          );
        })}
      </nav>

      {/* Footer */}
      {!collapsed && (
        <div className="p-3 border-t border-border-base">
          <div className="text-[10px] text-fg-muted text-center px-2 truncate">
            {t('sidebar.footer')}
          </div>
        </div>
      )}
    </aside>
  );
}
