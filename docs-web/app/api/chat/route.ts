import {
  convertToModelMessages,
  stepCountIs,
  streamText,
  tool,
  type UIMessage,
} from "ai";
import { z } from "zod";
import { source } from "@/lib/source";
import { Document, type DocumentData } from "flexsearch";
import { createWorkersAI } from "workers-ai-provider";
import { getCloudflareContext } from "@opennextjs/cloudflare";
import { checkRateLimit, trackTokenUsage, getClientIp } from "@/lib/rate-limit";

interface CustomDocument extends DocumentData {
  url: string;
  title: string;
  description: string;
  content: string;
}

export type ChatUIMessage = UIMessage<
  never,
  {
    client: {
      location: string;
    };
  }
>;

const searchServer = createSearchServer();

async function createSearchServer() {
  const search = new Document<CustomDocument>({
    document: {
      id: "url",
      index: ["title", "description", "content"],
      store: true,
    },
  });

  const docs = await chunkedAll(
    source.getPages().map(async (page) => {
      if (!("getText" in page.data)) return null;

      return {
        title: page.data.title,
        description: page.data.description,
        url: page.url,
        content: await page.data.getText("processed"),
      } as CustomDocument;
    }),
  );

  for (const doc of docs) {
    if (doc) search.add(doc);
  }

  return search;
}

async function chunkedAll<O>(promises: Promise<O>[]): Promise<O[]> {
  const SIZE = 50;
  const out: O[] = [];
  for (let i = 0; i < promises.length; i += SIZE) {
    out.push(...(await Promise.all(promises.slice(i, i + SIZE))));
  }
  return out;
}

const systemPrompt = [
  "You are the Bedrock AI assistant — a specialized helper for the Bedrock Python framework documentation.",
  "You ONLY answer questions related to the Bedrock framework, its modules, database layer, CLI, configuration, API, and usage patterns.",
  "",
  "SCOPE RULES:",
  "- If the user asks about Bedrock (modules, database, CLI, signals, cache, settings, migrations, etc.) → answer thoroughly using the `search` tool to find relevant docs.",
  "- If the user asks about general Python, unrelated libraries, or off-topic subjects → politely decline and redirect them back to Bedrock topics.",
  "- If the user's question is ambiguous but could be related to Bedrock → assume Bedrock context and answer.",
  "",
  "When answering:",
  "- Use the `search` tool to retrieve relevant docs context before answering when needed.",
  "- The `search` tool returns raw JSON results from documentation. Use those results to ground your answer and cite sources as markdown links using the document `url` field when available.",
  "- If you cannot find the answer in search results, say you do not know and suggest a better search query.",
  "- Keep answers concise and practical. Show code examples when relevant.",
].join("\n");

export async function POST(req: Request, ctx: RouteContext<"/api/chat">) {
  const ip = getClientIp(req);
  const { env } = getCloudflareContext();
  const rateLimit = await checkRateLimit(env.RATE_LIMIT_KV, ip);
  if (!rateLimit.allowed) {
    return Response.json(
      {
        error: "Rate limit exceeded",
        message: `You have used ${rateLimit.current} tokens this hour. Limit is 50,000 tokens/hour. Resets at ${new Date(rateLimit.resetAt * 1000).toISOString()}.`,
        resetAt: rateLimit.resetAt,
      },
      {
        status: 429,
        headers: {
          "X-RateLimit-Limit": "50000",
          "X-RateLimit-Remaining": "0",
          "X-RateLimit-Reset": String(rateLimit.resetAt),
          "Retry-After": String(
            rateLimit.resetAt - Math.floor(Date.now() / 1000),
          ),
        },
      },
    );
  }

  const reqJson = await req.json();
  const workersai = createWorkersAI({ binding: env.AI });

  const result = streamText({
    model: workersai(process.env.WORKER_AI_MODEL ?? "@cf/moonshotai/kimi-k2.5"),
    stopWhen: stepCountIs(5),
    tools: {
      search: searchTool,
    },
    messages: [
      { role: "system", content: systemPrompt },
      //@ts-ignore
      ...(await convertToModelMessages<ChatUIMessage>(reqJson.messages ?? [], {
        convertDataPart(part) {
          if (part.type === "data-client")
            return {
              type: "text",
              text: `[Client Context: ${JSON.stringify(part.data)}]`,
            };
        },
      })),
    ],
    toolChoice: "auto",
    onFinish: async ({ usage }) => {
      await trackTokenUsage(
        env.RATE_LIMIT_KV,
        ip,
        usage.inputTokens ?? 0,
        usage.outputTokens ?? 0,
      );
    },
    onError: (error) => {
      console.error(error);
    },
  });

  return result.toUIMessageStreamResponse({
    headers: {
      "X-RateLimit-Limit": "50000",
      "X-RateLimit-Remaining": String(rateLimit.remaining),
      "X-RateLimit-Reset": String(rateLimit.resetAt),
    },
  });
}

export type SearchTool = typeof searchTool;

const searchTool = tool({
  description: "Search the docs content and return raw JSON results.",
  inputSchema: z.object({
    query: z.string(),
    limit: z.number().int().min(1).max(100).default(10),
  }),
  async execute({ query, limit }) {
    const search = await searchServer;
    return await search.searchAsync(query, {
      limit,
      merge: true,
      enrich: true,
    });
  },
});
