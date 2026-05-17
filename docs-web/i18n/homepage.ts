export const homepage = {
  en: {
    // Hero
    badge: 'Production-ready Python framework',
    heroTitle: 'Build with',
    heroSubtitle:
      'A modular Python application framework for teams that want predictable architecture, strong conventions, and tooling that works with humans and AI.',
    ctaGetStarted: 'Get Started',
    ctaGithub: 'View on GitHub',

    // Features
    featuresTitle: 'Everything you need',
    featuresSubtitle: 'Production-ready subsystems built with clean architecture in mind.',
    featureModularTitle: 'Modular Architecture',
    featureModularDesc: 'Manifest-driven module loading with explicit dependency management and lifecycle hooks.',
    featureDatabaseTitle: 'Database Layer',
    featureDatabaseDesc: 'SQLAlchemy 2.0 integration with declarative models, session management, and Alembic migrations.',
    featureSignalTitle: 'Signal System',
    featureSignalDesc: 'Event system with sync/async support for decoupled communication between modules.',
    featureCliTitle: 'CLI Tools',
    featureCliDesc: 'Typer-based CLI for module and database management. Generate, validate, and run.',
    featureAiTitle: 'AI Ready',
    featureAiDesc: 'Built-in agent skills for assisted development. AI-powered code generation and guidance out of the box.',
    featureSpecTitle: 'Spec-driven',
    featureSpecDesc: 'Define your module spec in a few lines and start building. Minimal boilerplate, maximum productivity.',

    // Deep Dive
    deepDiveTitle: 'Deep Dive',
    deepDiveSubtitle: 'Powerful primitives designed for complex, modular applications.',
    deepDiveDbTitle: 'Database Management',
    deepDiveDbTagline: 'Production-grade persistence layer',
    deepDiveDbBullet1Title: 'Modular Database Migration',
    deepDiveDbBullet1Desc: 'Alembic-powered, per-module migration management',
    deepDiveDbBullet2Title: 'Model CRUD Observer',
    deepDiveDbBullet2Desc: 'Automatic event hooks for create, update, delete operations',
    deepDiveDbBullet3Title: 'Thread-safe Global Session',
    deepDiveDbBullet3Desc: 'Async-compatible session scoping with context-local isolation',
    deepDiveLifecycleTitle: 'Lifecycle & Hooks',
    deepDiveLifecycleTagline: 'Predictable module orchestration',
    deepDiveLifecycleBullet1Title: 'Manifest-driven Loading',
    deepDiveLifecycleBullet1Desc: 'Declarative dependency resolution and load ordering',
    deepDiveLifecycleBullet2Title: 'Bootstrap Hooks',
    deepDiveLifecycleBullet2Desc: 'on_load, ready, on_shutdown for each module',
    deepDiveLifecycleBullet3Title: 'Hot-reload Support',
    deepDiveLifecycleBullet3Desc: 'Development-friendly module reloading without restart',
    deepDiveAiTitle: 'AI Integration',
    deepDiveAiTagline: 'Built for the AI era',
    deepDiveAiBullet1Title: 'Module Skills (Playbooks)',
    deepDiveAiBullet1Desc: 'Structured context files that help AI understand module usage',
    deepDiveAiBullet2Title: 'Native SKILL Support',
    deepDiveAiBullet2Desc: 'First-class integration with AI agent skill systems',
    deepDiveAiBullet3Title: 'Spec-driven Generation',
    deepDiveAiBullet3Desc: 'Define module spec, let AI scaffold the implementation',

    // Stats
    statLinesOfCode: 'Lines of Code',
    statSubsystems: 'Core Subsystems',
    statTypeAnnotated: 'Type Annotated',
    statHttpDeps: 'HTTP Dependencies',

    // CTA
    ctaTitle: 'Ready to build?',
    ctaSubtitle: 'Get started with Bedrock in minutes. Install, configure, and ship.',
    ctaInstallationGuide: 'Installation Guide',
  },

  zh: {
    // Hero
    badge: '生产级 Python 框架',
    heroTitle: '用 Bedrock 构建',
    heroSubtitle:
      '面向团队的模块化 Python 应用框架，提供可预测的架构、强开发约定，以及同时支持人类与 AI 编码助手的工具链。',
    ctaGetStarted: '快速开始',
    ctaGithub: '在 GitHub 上查看',

    // Features
    featuresTitle: '你需要的一切',
    featuresSubtitle: '面向生产环境的子系统，以 clean architecture 为设计理念构建。',
    featureModularTitle: '模块化架构',
    featureModularDesc: '基于 Manifest 驱动的模块加载，支持显式依赖管理与生命周期钩子。',
    featureDatabaseTitle: '数据库层',
    featureDatabaseDesc: '集成 SQLAlchemy 2.0，支持声明式模型、会话管理与 Alembic 迁移。',
    featureSignalTitle: '信号系统',
    featureSignalDesc: '支持同步/异步的事件系统，实现模块间解耦通信。',
    featureCliTitle: 'CLI 工具',
    featureCliDesc: '基于 Typer 的 CLI，用于模块和数据库管理。生成、校验、运行。',
    featureAiTitle: 'AI 就绪',
    featureAiDesc: '内置 Agent 技能，支持辅助开发。开箱即用的 AI 代码生成与引导。',
    featureSpecTitle: '规范驱动',
    featureSpecDesc: '用几行代码定义模块规范即可开始构建。最小化样板代码，最大化生产力。',

    // Deep Dive
    deepDiveTitle: '深入探索',
    deepDiveSubtitle: '为复杂模块化应用设计的强大原语。',
    deepDiveDbTitle: '数据库管理',
    deepDiveDbTagline: '生产级持久化层',
    deepDiveDbBullet1Title: '模块化数据库迁移',
    deepDiveDbBullet1Desc: '基于 Alembic，按模块管理迁移',
    deepDiveDbBullet2Title: '模型 CRUD 观察器',
    deepDiveDbBullet2Desc: '自动事件钩子，覆盖创建、更新、删除操作',
    deepDiveDbBullet3Title: '线程安全的全局会话',
    deepDiveDbBullet3Desc: '兼容异步的会话作用域，支持上下文隔离',
    deepDiveLifecycleTitle: '生命周期与钩子',
    deepDiveLifecycleTagline: '可预测的模块编排',
    deepDiveLifecycleBullet1Title: 'Manifest 驱动加载',
    deepDiveLifecycleBullet1Desc: '声明式依赖解析与加载排序',
    deepDiveLifecycleBullet2Title: 'Bootstrap 钩子',
    deepDiveLifecycleBullet2Desc: '每个模块支持 on_load、ready、on_shutdown',
    deepDiveLifecycleBullet3Title: '热重载支持',
    deepDiveLifecycleBullet3Desc: '开发友好的模块重载，无需重启',
    deepDiveAiTitle: 'AI 集成',
    deepDiveAiTagline: '为 AI 时代而生',
    deepDiveAiBullet1Title: '模块技能（Playbook）',
    deepDiveAiBullet1Desc: '结构化上下文文件，帮助 AI 理解模块用法',
    deepDiveAiBullet2Title: '原生 SKILL 支持',
    deepDiveAiBullet2Desc: '与 AI Agent 技能系统的一等公民集成',
    deepDiveAiBullet3Title: '规范驱动生成',
    deepDiveAiBullet3Desc: '定义模块规范，让 AI 生成实现',

    // Stats
    statLinesOfCode: '代码行数',
    statSubsystems: '核心子系统',
    statTypeAnnotated: '类型标注覆盖',
    statHttpDeps: 'HTTP 依赖',

    // CTA
    ctaTitle: '准备好开始了吗？',
    ctaSubtitle: '几分钟内上手 Bedrock。安装、配置、发布。',
    ctaInstallationGuide: '安装指南',
  },
} as const;

export interface HomepageTexts {
  badge: string;
  heroTitle: string;
  heroSubtitle: string;
  ctaGetStarted: string;
  ctaGithub: string;
  featuresTitle: string;
  featuresSubtitle: string;
  featureModularTitle: string;
  featureModularDesc: string;
  featureDatabaseTitle: string;
  featureDatabaseDesc: string;
  featureSignalTitle: string;
  featureSignalDesc: string;
  featureCliTitle: string;
  featureCliDesc: string;
  featureAiTitle: string;
  featureAiDesc: string;
  featureSpecTitle: string;
  featureSpecDesc: string;
  deepDiveTitle: string;
  deepDiveSubtitle: string;
  deepDiveDbTitle: string;
  deepDiveDbTagline: string;
  deepDiveDbBullet1Title: string;
  deepDiveDbBullet1Desc: string;
  deepDiveDbBullet2Title: string;
  deepDiveDbBullet2Desc: string;
  deepDiveDbBullet3Title: string;
  deepDiveDbBullet3Desc: string;
  deepDiveLifecycleTitle: string;
  deepDiveLifecycleTagline: string;
  deepDiveLifecycleBullet1Title: string;
  deepDiveLifecycleBullet1Desc: string;
  deepDiveLifecycleBullet2Title: string;
  deepDiveLifecycleBullet2Desc: string;
  deepDiveLifecycleBullet3Title: string;
  deepDiveLifecycleBullet3Desc: string;
  deepDiveAiTitle: string;
  deepDiveAiTagline: string;
  deepDiveAiBullet1Title: string;
  deepDiveAiBullet1Desc: string;
  deepDiveAiBullet2Title: string;
  deepDiveAiBullet2Desc: string;
  deepDiveAiBullet3Title: string;
  deepDiveAiBullet3Desc: string;
  statLinesOfCode: string;
  statSubsystems: string;
  statTypeAnnotated: string;
  statHttpDeps: string;
  ctaTitle: string;
  ctaSubtitle: string;
  ctaInstallationGuide: string;
}

export function getHomepageTexts(lang: string): HomepageTexts {
  return homepage[lang as keyof typeof homepage] ?? homepage.en;
}
