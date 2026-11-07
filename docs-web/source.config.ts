import { defineConfig, defineDocs } from 'fumadocs-mdx/config';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';
import { remarkMdxMermaid } from 'fumadocs-core/mdx-plugins';
import { remarkMdxFiles } from 'fumadocs-core/mdx-plugins/remark-mdx-files';

type MdNode = { type: string; depth?: number; children?: MdNode[] };

// `<include>` splices a file in as a nested `root` node. Drop the included file's own H1
// (e.g. `# Changelog` from CHANGELOG.md) because DocsTitle already renders the page title.
function remarkDropIncludedTitle() {
  const walk = (node: MdNode, nested: boolean): void => {
    if (!node.children) return;
    if (nested && node.type === 'root') {
      node.children = node.children.filter((child) => !(child.type === 'heading' && child.depth === 1));
    }
    for (const child of node.children) walk(child, true);
  };
  return (tree: MdNode) => walk(tree, false);
}

// You can customize Zod schemas for frontmatter and `meta.json` here
// see https://fumadocs.dev/docs/mdx/collections
export const docs = defineDocs({
  dir: 'content/docs',
  docs: {
    schema: pageSchema,
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: metaSchema,
  },
});

export default defineConfig({
  mdxOptions: {
    // MDX options
    remarkPlugins: [remarkMdxMermaid, remarkMdxFiles, remarkDropIncludedTitle],
  },
});
