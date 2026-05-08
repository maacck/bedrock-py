import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, gitConfig } from './shared';
import { i18nUI } from './i18n';

export { i18nUI };

export function baseOptions(locale: string): BaseLayoutProps {
  return {
    nav: {
      // JSX supported
      title: <div className={'flex items-center gap-2'}>
        <div className={'size-10 rounded-md'}>
          <img src={'/logo.png'} alt={appName} className={'size-full rounded-md'} />
        </div>
        <span>
          {appName}
        </span>
      </div>,
    },

    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
