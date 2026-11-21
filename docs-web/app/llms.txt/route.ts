import { source } from '@/lib/source';
import { llms } from 'fumadocs-core/source';

export const revalidate = false;

export function GET() {
  const { indexNode } = llms(source);
  const sections = source.getPageTree('en').children.map((node) => indexNode(node, 'en'));
  return new Response([
    '# Bedrock documentation',
    '',
    `> bedrock-core: ${process.env.BEDROCK_VERSION}`,
    '> For breaking changes and upgrade steps, see the [changelog](/llms.mdx/docs/changelog).',
    '',
    ...sections,
  ].join('\n'));
}
