import type { Translations } from 'fumadocs-ui/i18n';
import { translations as en } from './en';
import { translations as zh } from './zh';

export const translations: Record<string, Partial<Translations>> = {
  en,
  zh,
};

// Re-export from lib/i18n.ts for centralized configuration
export { i18n, i18nUI } from '../lib/i18n';