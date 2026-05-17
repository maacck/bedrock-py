import Link from 'next/link';
import { ArrowRight, Database, Shield, Terminal, Blocks, Sparkles, FileCode, Workflow, Bot } from 'lucide-react';
import { Installation } from '@/components/installation';
import { getHomepageTexts } from '@/i18n/homepage';

export default async function HomePage({
  params,
}: {
  params: Promise<{ lang: string }>;
}) {
  const { lang } = await params;
  const t = getHomepageTexts(lang);

  const deepDiveFeatures = [
    {
      icon: Database,
      title: t.deepDiveDbTitle,
      tagline: t.deepDiveDbTagline,
      bullets: [
        `${t.deepDiveDbBullet1Title} — ${t.deepDiveDbBullet1Desc}`,
        `${t.deepDiveDbBullet2Title} — ${t.deepDiveDbBullet2Desc}`,
        `${t.deepDiveDbBullet3Title} — ${t.deepDiveDbBullet3Desc}`,
      ],
      codeSnippet: 'with db.session() as session:\n    session.add(product)',
    },
    {
      icon: Workflow,
      title: t.deepDiveLifecycleTitle,
      tagline: t.deepDiveLifecycleTagline,
      bullets: [
        `${t.deepDiveLifecycleBullet1Title} — ${t.deepDiveLifecycleBullet1Desc}`,
        `${t.deepDiveLifecycleBullet2Title} — ${t.deepDiveLifecycleBullet2Desc}`,
        `${t.deepDiveLifecycleBullet3Title} — ${t.deepDiveLifecycleBullet3Desc}`,
      ],
      codeSnippet: 'def ready(*, app):\n    """Called when all modules are ready"""',
    },
    {
      icon: Bot,
      title: t.deepDiveAiTitle,
      tagline: t.deepDiveAiTagline,
      bullets: [
        `${t.deepDiveAiBullet1Title} — ${t.deepDiveAiBullet1Desc}`,
        `${t.deepDiveAiBullet2Title} — ${t.deepDiveAiBullet2Desc}`,
        `${t.deepDiveAiBullet3Title} — ${t.deepDiveAiBullet3Desc}`,
      ],
      codeSnippet: 'bedrock app generate --spec module.yaml',
    },
  ];

  const features = [
    { icon: Blocks, title: t.featureModularTitle, description: t.featureModularDesc },
    { icon: Database, title: t.featureDatabaseTitle, description: t.featureDatabaseDesc },
    { icon: Shield, title: t.featureSignalTitle, description: t.featureSignalDesc },
    { icon: Terminal, title: t.featureCliTitle, description: t.featureCliDesc },
    { icon: Sparkles, title: t.featureAiTitle, description: t.featureAiDesc },
    { icon: FileCode, title: t.featureSpecTitle, description: t.featureSpecDesc },
  ];
  return (
    <main className="relative overflow-hidden">
      <div className="pointer-events-none absolute inset-0 select-none">
        <div className="absolute left-1/2 top-0 -translate-x-1/2 -translate-y-1/2 h-[800px] w-[1200px] rounded-full bg-gradient-to-b from-primary/20 to-transparent blur-3xl" />
        <div className="absolute right-0 top-1/3 h-[600px] w-[600px] rounded-full bg-gradient-to-l from-primary/10 to-transparent blur-3xl" />
      </div>

      <section className="relative flex flex-col items-center justify-center px-4 pt-32 pb-20 md:pt-40 md:pb-32">
        <div className="animate-fade-in-up mb-8 flex items-center gap-2 rounded-full border border-primary/20 bg-primary/5 px-4 py-2 text-sm text-primary backdrop-blur-sm">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
          </span>
          {t.badge}
        </div>

        <h1 className="animate-fade-in-up animation-delay-100 text-center text-5xl font-bold tracking-tight text-foreground sm:text-6xl md:text-7xl lg:text-8xl">
          {t.heroTitle}
          <span className="block bg-gradient-to-r from-primary via-primary/80 to-primary/60 bg-clip-text text-transparent">
            Bedrock
          </span>
        </h1>

        <p className="animate-fade-in-up animation-delay-200 mt-6 max-w-2xl text-center text-lg text-muted-foreground sm:text-xl md:text-2xl">
          {t.heroSubtitle}
        </p>

        <div className="animate-fade-in-up animation-delay-300 mt-10 flex flex-col gap-4 sm:flex-row">
          <Link
            href="/docs"
            className="group inline-flex items-center justify-center gap-2 rounded-lg bg-primary px-6 py-3 text-sm font-medium text-primary-foreground shadow-lg shadow-primary/25 transition-all hover:shadow-xl hover:shadow-primary/30 hover:-translate-y-0.5"
          >
            {t.ctaGetStarted}
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </Link>
          <a
            href="https://github.com/maacck/bedrock-py"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center justify-center gap-2 rounded-lg border bg-background/50 px-6 py-3 text-sm font-medium text-foreground backdrop-blur-sm transition-all hover:bg-accent hover:-translate-y-0.5"
          >
            {t.ctaGithub}
          </a>
        </div>
        <div className="animate-fade-in-up animation-delay-400 mt-16 w-full max-w-2xl overflow-hidden rounded-xl border bg-card/50 shadow-2xl backdrop-blur-sm">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <div className="flex gap-1.5">
              <div className="h-3 w-3 rounded-full bg-red-500/80" />
              <div className="h-3 w-3 rounded-full bg-yellow-500/80" />
              <div className="h-3 w-3 rounded-full bg-green-500/80" />
            </div>
            <span className="ml-2 text-xs text-muted-foreground">app.py</span>
          </div>
          <pre className="overflow-x-auto p-6 text-sm">
            <code className="font-mono">
              <span className="text-purple-400">import</span>{' '}
              <span className="text-blue-400">bedrock</span>
              {'\n\n'}
              <span className="text-muted-foreground"># Initialize the runtime</span>
              {'\n'}
              <span className="text-blue-400">bedrock</span>
              <span className="text-foreground">.</span>
              <span className="text-yellow-400">setup</span>
              <span className="text-foreground">()</span>
              {'\n\n'}
              <span className="text-muted-foreground"># Access key singletons</span>
              {'\n'}
              <span className="text-foreground">apps</span>{' '}
              <span className="text-foreground">=</span>{' '}
              <span className="text-blue-400">bedrock</span>
              <span className="text-foreground">.apps</span>{' '}
              <span className="text-muted-foreground"># ModuleRegistry</span>
              {'\n'}
              <span className="text-foreground">db</span>{' '}
              <span className="text-foreground">=</span>{' '}
              <span className="text-blue-400">bedrock</span>
              <span className="text-foreground">.db</span>{' '}
              <span className="text-muted-foreground"># DatabaseManager</span>
              {'\n'}
              <span className="text-foreground">cache</span>{' '}
              <span className="text-foreground">=</span>{' '}
              <span className="text-blue-400">bedrock</span>
              <span className="text-foreground">.cache</span>{' '}
              <span className="text-muted-foreground"># CacheService</span>
            </code>
          </pre>
        </div>
      </section>

      <section className="relative px-4 py-20 md:py-32">
        <div className="mx-auto max-w-6xl">
          <div className="mb-16 text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl">
              {t.featuresTitle}
            </h2>
            <p className="mt-4 text-lg text-muted-foreground">
              {t.featuresSubtitle}
            </p>
          </div>

          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {features.map((feature, index) => (
              <div
                key={feature.title}
                className="animate-fade-in-up group relative overflow-hidden rounded-xl border bg-card/50 p-6 backdrop-blur-sm transition-all hover:shadow-lg hover:-translate-y-1"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className="absolute inset-0 bg-gradient-to-br from-primary/5 to-transparent opacity-0 transition-opacity group-hover:opacity-100" />
                <div className="relative">
                  <div className="mb-4 inline-flex rounded-lg bg-primary/10 p-3 text-primary">
                    <feature.icon className="h-6 w-6" />
                  </div>
                  <h3 className="mb-2 text-lg font-semibold">{feature.title}</h3>
                  <p className="text-sm text-muted-foreground">{feature.description}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="relative px-4 py-20 md:py-32 bg-gradient-to-b from-transparent via-muted/30 to-transparent">
        <div className="mx-auto max-w-7xl">
          <div className="mb-16 text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl">
              {t.deepDiveTitle}
            </h2>
            <p className="mt-4 text-lg text-muted-foreground">
              {t.deepDiveSubtitle}
            </p>
          </div>

          <div className="grid gap-8 lg:grid-cols-3">
            {deepDiveFeatures.map((feature, index) => (
              <div
                key={feature.title}
                className="animate-fade-in-up group relative flex flex-col overflow-hidden rounded-2xl border bg-card/50 p-8 shadow-sm backdrop-blur-sm transition-all hover:shadow-xl hover:-translate-y-1"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className="absolute inset-0 bg-gradient-to-br from-primary/5 to-transparent opacity-0 transition-opacity group-hover:opacity-100" />
                <div className="absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-primary/40 to-transparent opacity-50 transition-all group-hover:opacity-100 group-hover:from-primary" />
                
                <div className="relative flex flex-1 flex-col">
                  <div className="mb-6 w-fit rounded-xl bg-primary/10 p-3.5 text-primary ring-1 ring-primary/20">
                    <feature.icon className="h-7 w-7" />
                  </div>
                  <h3 className="text-xl font-bold text-foreground">{feature.title}</h3>
                  <p className="mb-6 mt-1 text-sm font-medium text-primary/80">{feature.tagline}</p>
                  
                  <ul className="mb-8 flex flex-1 flex-col gap-3">
                    {feature.bullets.map((bullet, i) => {
                      const [title, desc] = bullet.split(' — ');
                      return (
                        <li key={i} className="flex items-start gap-2.5 text-sm">
                          <div className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/50" />
                          <span className="text-muted-foreground leading-relaxed">
                            <strong className="font-semibold text-foreground/90">{title}</strong> — {desc}
                          </span>
                        </li>
                      );
                    })}
                  </ul>

                  <div className="mt-auto rounded-lg border bg-background/50 p-4 font-mono text-xs text-muted-foreground backdrop-blur-md">
                    <pre className="overflow-x-auto"><code>{feature.codeSnippet}</code></pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="relative border-y bg-muted/30 px-4 py-16 md:py-24">
        <div className="mx-auto max-w-4xl">
          <div className="grid grid-cols-2 gap-8 md:grid-cols-4">
            {[
              { value: '7,440+', label: t.statLinesOfCode },
              { value: '5', label: t.statSubsystems },
              { value: '100%', label: t.statTypeAnnotated },
              { value: '0', label: t.statHttpDeps },
            ].map((stat, index) => (
              <div
                key={stat.label}
                className="animate-fade-in-up text-center"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className="text-3xl font-bold text-primary md:text-4xl">{stat.value}</div>
                <div className="mt-2 text-sm text-muted-foreground">{stat.label}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="relative px-4 py-20 md:py-32">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
            {t.ctaTitle}
          </h2>
          <p className="mt-4 text-lg text-muted-foreground">
            {t.ctaSubtitle}
          </p>
          <div className="mt-8 flex flex-col items-center justify-center gap-4">
            <Installation />
            <Link
              href="/docs/getting-started/installation"
              className="group inline-flex items-center justify-center gap-2 rounded-lg bg-primary px-6 py-3 text-sm font-medium text-primary-foreground shadow-lg shadow-primary/25 transition-all hover:shadow-xl hover:shadow-primary/30 hover:-translate-y-0.5"
            >
              {t.ctaInstallationGuide}
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}
