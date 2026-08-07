import { remark } from 'remark';
import remarkGfm from 'remark-gfm';
import remarkRehype from 'remark-rehype';
import { toJsxRuntime } from 'hast-util-to-jsx-runtime';
import {
  Children,
  type ComponentProps,
  type ReactElement,
  type ReactNode,
  Suspense,
  use,
  useDeferredValue,
} from 'react';
import { Fragment, jsx, jsxs } from 'react/jsx-runtime';
import { DynamicCodeBlock } from 'fumadocs-ui/components/dynamic-codeblock';
import defaultMdxComponents from 'fumadocs-ui/mdx';
import { visit } from 'unist-util-visit';
import type { ElementContent, Root, RootContent } from 'hast';
import { sanitizeUrl } from '@/lib/safe-url';

export interface Processor {
  process: (content: string) => Promise<ReactNode>;
}

export function rehypeWrapWords() {
  return (tree: Root) => {
    visit(tree, ['text', 'element'], (node, index, parent) => {
      if (node.type === 'element' && node.tagName === 'pre') return 'skip';
      if (node.type !== 'text' || !parent || index === undefined) return;

      const words = node.value.split(/(?=\s)/);

      // Create new span nodes for each word and whitespace
      const newNodes: ElementContent[] = words.flatMap((word) => {
        if (word.length === 0) return [];

        return {
          type: 'element',
          tagName: 'span',
          properties: {
            class: 'animate-fd-fade-in',
          },
          children: [{ type: 'text', value: word }],
        };
      });

      Object.assign(node, {
        type: 'element',
        tagName: 'span',
        properties: {},
        children: newNodes,
      } satisfies RootContent);
      return 'skip';
    });
  };
}

export function createProcessor(): Processor {
  const processor = remark().use(remarkGfm).use(remarkRehype).use(rehypeWrapWords);

  return {
    async process(content) {
      const nodes = processor.parse({ value: content });
      const hast = await processor.run(nodes);

      return toJsxRuntime(hast, {
        development: false,
        jsx,
        jsxs,
        Fragment,
        components: {
          ...defaultMdxComponents,
          pre: Pre,
          a: SafeAnchor,
          img: SafeImage,
        },
      });
    },
  };
}

const DefaultAnchor = defaultMdxComponents.a;

export function SafeAnchor(props: ComponentProps<'a'>) {
  const href = sanitizeUrl(props.href, 'link');

  if (href === undefined) {
    // Unsafe URL: degrade to non-navigable text, never render the href.
    return <span className={props.className}>{props.children}</span>;
  }

  if (props.target === '_blank') {
    // New-window links must always carry noopener noreferrer. Only override
    // rel in this case — passing rel={undefined} would erase the default
    // component's own rel attribute.
    return <DefaultAnchor {...props} href={href} rel="noopener noreferrer" />;
  }

  return <DefaultAnchor {...props} href={href} />;
}

export function SafeImage(props: ComponentProps<'img'>) {
  const src = sanitizeUrl(props.src, 'image');

  if (src === undefined) {
    // Unsafe URL: render nothing, never load the resource.
    return null;
  }

  return <img {...props} src={src} alt={props.alt ?? ''} />;
}

function Pre(props: ComponentProps<'pre'>) {
  const code = Children.only(props.children) as ReactElement;
  const codeProps = code.props as ComponentProps<'code'>;
  const content = codeProps.children;
  if (typeof content !== 'string') return null;

  let lang =
    codeProps.className
      ?.split(' ')
      .find((v) => v.startsWith('language-'))
      ?.slice('language-'.length) ?? 'text';

  if (lang === 'mdx') lang = 'md';

  return <DynamicCodeBlock lang={lang} code={content.trimEnd()} />;
}

const processor = createProcessor();

export function Markdown({ text }: { text: string }) {
  const deferredText = useDeferredValue(text);

  return (
    <Suspense fallback={<p className="invisible">{text}</p>}>
      <Renderer text={deferredText} />
    </Suspense>
  );
}

const cache = new Map<string, Promise<ReactNode>>();

function Renderer({ text }: { text: string }) {
  const result = cache.get(text) ?? processor.process(text);
  cache.set(text, result);

  return use(result);
}
