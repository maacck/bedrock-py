import { source } from "@/lib/source";
import { DocsLayout } from "fumadocs-ui/layouts/notebook";
import { baseOptions } from "@/lib/layout.shared";
import {
  AISearch,
  AISearchPanel,
  AISearchTrigger,
} from "@/components/ai/search";
import { MessageCircleIcon } from "lucide-react";
import { buttonVariants } from "fumadocs-ui/components/ui/button";
import { cn } from "@/lib/utils";
import { getSearchTexts } from "@/i18n/search";

export default async function Layout({
  params,
  children,
}: {
  params: Promise<{ lang: string }>;
  children: React.ReactNode;
}) {
  const { lang } = await params;
  const texts = getSearchTexts(lang);

  return (
    <DocsLayout
      tabs={{
        transform(option, node) {
          const meta = source.getNodeMeta(node);
          if (!meta || !node.icon) return option;

          return {
            ...option,
            icon: (
              <div className="[&_svg]:size-full bg-(--tab-color) rounded-lg size-full text-(--tab-color) max-md:bg-(--tab-color)/10 max-md:border max-md:p-1.5">
                {node.icon}
              </div>
            ),
          };
        },
      }}
      tree={source.getPageTree(lang)}
      {...baseOptions(lang)}
    >
      <AISearch texts={texts}>
        <AISearchPanel />
        <AISearchTrigger
          position="float"
          className={cn(
            buttonVariants({
              variant: "secondary",
              className: "text-fd-muted-foreground rounded-2xl",
            }),
          )}
        >
          <MessageCircleIcon className="size-4.5" />
          {texts.askAI}
        </AISearchTrigger>
      </AISearch>
      {children}
    </DocsLayout>
  );
}
