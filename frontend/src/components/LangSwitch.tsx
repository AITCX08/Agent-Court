import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { setLang, type Lang } from '../i18n';

export function LangSwitch() {
  const { t, i18n } = useTranslation();
  const [active, setActive] = useState<Lang>((i18n.language as Lang) || 'zh');

  const choose = (lang: Lang) => {
    setActive(lang);
    setLang(lang);
  };

  return (
    <div className="inline-flex rounded-md border border-border-base overflow-hidden text-xs">
      <button
        type="button"
        onClick={() => choose('zh')}
        className={`px-2.5 py-1 transition ${
          active === 'zh'
            ? 'bg-accent-primary text-white'
            : 'text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover'
        }`}
      >
        {t('topbar.lang.zh')}
      </button>
      <button
        type="button"
        onClick={() => choose('en')}
        className={`px-2.5 py-1 transition border-l border-border-base ${
          active === 'en'
            ? 'bg-accent-primary text-white'
            : 'text-fg-secondary hover:text-fg-primary hover:bg-bg-card-hover'
        }`}
      >
        {t('topbar.lang.en')}
      </button>
    </div>
  );
}
