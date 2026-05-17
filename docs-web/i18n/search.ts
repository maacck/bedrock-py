export const search = {
  en: {
    suggestions: [
      'What is bedrock?',
      'How to install bedrock?',
      'How to use bedrock?',
    ],
    aiChat: 'AI Chat',
    aiDisclaimer: 'AI can be inaccurate, please verify the answers.',
    retry: 'Retry',
    clearChat: 'Clear Chat',
    askPlaceholder: 'Ask a question',
    answeringPlaceholder: 'AI is answering...',
    abortAnswer: 'Abort Answer',
    startChat: 'Start a new chat below.',
    searching: 'Searching\u2026',
    searchResults: '{{count}} search results',
    failedToSearch: 'Failed to search',
    quotaExceeded: 'Quota Exceeded',
    quotaMessage: 'You\u2019ve reached the hourly token limit. Please wait before sending more messages.',
    requestFailed: 'Request Failed',
    askAI: 'Ask AI',
  },
  zh: {
    suggestions: [
      '什么是 Bedrock？',
      '如何安装 Bedrock？',
      '如何使用 Bedrock？',
    ],
    aiChat: 'AI 对话',
    aiDisclaimer: 'AI 可能不准确，请核实答案。',
    retry: '重试',
    clearChat: '清空对话',
    askPlaceholder: '输入问题',
    answeringPlaceholder: 'AI 正在回答...',
    abortAnswer: '中止回答',
    startChat: '在下方开始新对话。',
    searching: '搜索中\u2026',
    searchResults: '{{count}} 条搜索结果',
    failedToSearch: '搜索失败',
    quotaExceeded: '配额已用尽',
    quotaMessage: '已达到每小时 token 限制，请稍后再发送消息。',
    requestFailed: '请求失败',
    askAI: '问问 AI',
  },
} as const;

export interface SearchTexts {
  suggestions: readonly string[];
  aiChat: string;
  aiDisclaimer: string;
  retry: string;
  clearChat: string;
  askPlaceholder: string;
  answeringPlaceholder: string;
  abortAnswer: string;
  startChat: string;
  searching: string;
  searchResults: string;
  failedToSearch: string;
  quotaExceeded: string;
  quotaMessage: string;
  requestFailed: string;
  askAI: string;
}

export function getSearchTexts(lang: string): SearchTexts {
  return search[lang as keyof typeof search] ?? search.en;
}
