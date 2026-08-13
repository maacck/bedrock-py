import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { createProcessor } from './markdown';

const processor = createProcessor();

async function renderMarkdown(text: string): Promise<string> {
  const node = await processor.process(text);
  return renderToStaticMarkup(<>{node}</>);
}

describe('Markdown URL sanitization', () => {
  it('never renders javascript: links as anchors', async () => {
    const html = await renderMarkdown('[click me](javascript:alert(1))');
    expect(html).not.toContain('javascript:');
    expect(html).not.toContain('<a');
    expect(html.replace(/<[^>]+>/g, '')).toContain('click me');
  });

  it('blocks case-obfuscated javascript: links', async () => {
    const html = await renderMarkdown('[x](JaVaScRiPt:alert(1))');
    expect(html).not.toContain('<a');
  });

  it('blocks data: links', async () => {
    const html = await renderMarkdown('[x](data:text/html;base64,PHNjcmlwdD4=)');
    expect(html).not.toContain('<a');
  });

  it('blocks vbscript: links', async () => {
    const html = await renderMarkdown('[x](vbscript:msgbox(1))');
    expect(html).not.toContain('<a');
  });

  it('never renders data: images', async () => {
    const html = await renderMarkdown('![pixel](data:image/png;base64,iVBORw0KGgo=)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('data:image');
  });

  it('never renders javascript: images', async () => {
    const html = await renderMarkdown('![x](javascript:alert(1))');
    expect(html).not.toContain('<img');
  });

  it('keeps safe https links with noopener for new-window externals', async () => {
    const html = await renderMarkdown('[docs](https://example.com/docs)');
    expect(html).toContain('href="https://example.com/docs"');
    expect(html).toContain('noopener');
    expect(html).toContain('noreferrer');
  });

  it('keeps mailto links', async () => {
    const html = await renderMarkdown('[mail](mailto:user@example.com)');
    expect(html).toContain('href="mailto:user@example.com"');
  });

  it('keeps relative links', async () => {
    const html = await renderMarkdown('[guide](/docs/getting-started)');
    expect(html).toContain('href="/docs/getting-started"');
  });

  it('keeps fragment links', async () => {
    const html = await renderMarkdown('[jump](#installation)');
    expect(html).toContain('href="#installation"');
  });

  it('keeps safe https images', async () => {
    const html = await renderMarkdown('![logo](https://cdn.example.com/logo.png)');
    expect(html).toContain('src="https://cdn.example.com/logo.png"');
  });

  it('keeps relative images', async () => {
    const html = await renderMarkdown('![logo](/static/logo.png)');
    expect(html).toContain('src="/static/logo.png"');
  });

  it('does not break ordinary markdown content', async () => {
    const html = await renderMarkdown(
      'Some **bold** text with [a link](https://example.com) and `code`.',
    );
    expect(html).toContain('bold');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('code');
  });
});
