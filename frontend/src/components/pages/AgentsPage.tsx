import { Bot } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export function AgentsPage() {
  const { t } = useTranslation();
  return (
    <div className="p-8 max-w-3xl">
      <div className="rounded-xl bg-bg-card border border-border-base p-8">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 rounded-lg bg-accent-purple/15 text-accent-purple
                          flex items-center justify-center flex-shrink-0">
            <Bot className="w-5 h-5" />
          </div>
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-2">
              <h2 className="text-base font-semibold text-fg-primary">
                {t('agents.placeholder_title')}
              </h2>
              <span className="text-[10px] px-1.5 py-0.5 rounded
                               bg-accent-warn/15 text-accent-warn border border-accent-warn/30">
                {t('common.coming_soon')}
              </span>
            </div>
            <p className="text-sm text-fg-secondary leading-relaxed">
              {t('agents.placeholder_desc')}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
