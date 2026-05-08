import { defineI18n } from 'fumadocs-core/i18n';
import { defineI18nUI } from 'fumadocs-ui/i18n';

export const i18n = defineI18n({
  defaultLanguage: 'en',
  languages: ['en', 'zh'],
  hideLocale: 'default-locale',
  parser: 'dir',
});

export const i18nUI = defineI18nUI(i18n, {
  en: {
    displayName: 'English',
    search: 'Search',
    searchNoResult: 'No results found',
    toc: 'Table of contents',
    tocNoHeadings: 'No headings',
    lastUpdate: 'Last updated',
    chooseLanguage: 'Choose language',
    nextPage: 'Next page',
    previousPage: 'Previous page',
    chooseTheme: 'Choose theme',
    editOnGithub: 'Edit on GitHub',
  },
  zh: {
    displayName: '中文',
    search: '搜尋文檔',
    searchNoResult: '未找到結果',
    toc: '目錄',
    tocNoHeadings: '無標題',
    lastUpdate: '最後更新',
    chooseLanguage: '選擇語言',
    nextPage: '下一頁',
    previousPage: '上一頁',
    chooseTheme: '選擇主題',
    editOnGithub: '在 GitHub 上編輯',
  },
});
