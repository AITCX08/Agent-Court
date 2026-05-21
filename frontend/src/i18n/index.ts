import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import zh from './locales/zh.json';
import en from './locales/en.json';

export type Lang = 'zh' | 'en';

export const LANG_KEY = 'court-lang';

function getInitialLang(): Lang {
  const stored = localStorage.getItem(LANG_KEY);
  return stored === 'en' ? 'en' : 'zh';
}

i18n
  .use(initReactI18next)
  .init({
    resources: {
      zh: { translation: zh },
      en: { translation: en },
    },
    lng: getInitialLang(),
    fallbackLng: 'zh',
    interpolation: { escapeValue: false },
    missingKeyHandler: (_lngs, _ns, key) => {
      if (import.meta.env.DEV) {
        console.warn(`[i18n] missing key: ${key}`);
      }
    },
  });

export function setLang(lang: Lang): void {
  i18n.changeLanguage(lang);
  localStorage.setItem(LANG_KEY, lang);
  document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : 'en');
}

export function getLang(): Lang {
  return (i18n.language as Lang) || 'zh';
}

export default i18n;
