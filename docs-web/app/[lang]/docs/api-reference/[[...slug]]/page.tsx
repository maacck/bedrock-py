import { source } from "@/lib/source";
import { DocsPage, DocsBody } from "fumadocs-ui/layouts/notebook/page";
import { notFound } from "next/navigation";
import { getMDXComponents } from "@/components/mdx";
import { ServerCodeBlock } from "fumadocs-ui/components/codeblock.rsc";
import { createRelativeLink } from "fumadocs-ui/mdx";
import { DynamicLink } from "fumadocs-core/dynamic-link";
import { Mermaid } from "@/components/mermaid";
import { HighlightBlock } from "@/components/highlight-block";
import { File, Folder, Files } from "fumadocs-ui/components/files";

export default async function Page(props: {
  params: Promise<{ lang: string; slug?: string[] }>;
}) {
  const params = await props.params;
  const page = source.getPage(params.slug, params.lang);
  if (!page) notFound();

  const MDX = page.data.body;

  return (
    <DocsPage toc={page.data.toc} full={page.data.full}>
      <DocsBody>
        <MDX
          components={getMDXComponents({
            a: createRelativeLink(source, page, DynamicLink),
            CodeBlock: ServerCodeBlock,
            Mermaid,
            HighlightBlock,
            File,
            Folder,
            Files,
          })}
        />
      </DocsBody>
    </DocsPage>
  );
}

export async function generateStaticParams() {
  return source.generateParams();
}
