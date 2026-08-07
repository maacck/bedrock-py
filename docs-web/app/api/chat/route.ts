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
import {
  RATE_LIMIT,
  estimateTokens,
  forfeitBudget,
  getClientIp,
  isDeclaredBodyTooLarge,
  parseAndValidateChatBody,
  releaseBudget,
  reserveBudget,
  settleBudget,
} from "@/lib/rate-limit";

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

function rateLimitHeaders(remaining: number, resetAt: number) {
  return {
    "X-RateLimit-Limit": String(RATE_LIMIT.TOKENS_PER_WINDOW),
    "X-RateLimit-Remaining": String(remaining),
    "X-RateLimit-Reset": String(resetAt),
  };
}

export async function POST(req: Request) {
  const ip = getClientIp(req);
  if (!ip) {
    return Response.json(
      {
        error: "untrusted_client",
        message:
          "Missing cf-connecting-ip header. This API is only reachable through Cloudflare.",
      },
      { status: 403 },
    );
  }

  if (isDeclaredBodyTooLarge(req)) {
    return Response.json(
      {
        error: "body_too_large",
        message: `Request body exceeds the ${RATE_LIMIT.MAX_BODY_BYTES} byte limit.`,
      },
      { status: 413 },
    );
  }

  const parsed = parseAndValidateChatBody(await req.text());
  if (!parsed.ok) {
    return Response.json(
      { error: parsed.code, message: parsed.message },
      { status: parsed.status },
    );
  }

  const { env } = getCloudflareContext();

  // Atomically reserve budget before the model is called. The reservation
  // covers the estimated input plus a fixed output allowance; the unused
  // portion is refunded when the stream settles.
  const reservationTokens =
    parsed.estimatedInputTokens +
    estimateTokens(systemPrompt) +
    RATE_LIMIT.RESERVED_OUTPUT_TOKENS;
  const reservation = await reserveBudget(
    env.RATE_LIMITER,
    ip,
    reservationTokens,
  );
  if (!reservation.allowed || !reservation.reservationId) {
    return Response.json(
      {
        error: "rate_limited",
        message: `You have used ${reservation.used} of ${RATE_LIMIT.TOKENS_PER_WINDOW} tokens this hour. Resets at ${new Date(reservation.resetAt * 1000).toISOString()}.`,
        resetAt: reservation.resetAt,
      },
      {
        status: 429,
        headers: {
          ...rateLimitHeaders(reservation.remaining, reservation.resetAt),
          "Retry-After": String(
            reservation.resetAt - Math.floor(Date.now() / 1000),
          ),
        },
      },
    );
  }

  const reservationId = reservation.reservationId;
  let finalized = false;
  const finalize = async (
    action: () => Promise<void>,
  ): Promise<void> => {
    if (finalized) return;
    finalized = true;
    await action();
  };

  const workersai = createWorkersAI({ binding: env.AI });

  try {
    const result = streamText({
      model: workersai(
        process.env.WORKER_AI_MODEL ?? "@cf/moonshotai/kimi-k2.5",
      ),
      stopWhen: stepCountIs(5),
      abortSignal: req.signal,
      tools: {
        search: searchTool,
      },
      messages: [
        { role: "system", content: systemPrompt },
        //@ts-ignore
        ...(await convertToModelMessages<ChatUIMessage>(parsed.messages, {
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
      onFinish: async ({ totalUsage }) => {
        await finalize(() =>
          settleBudget(
            env.RATE_LIMITER,
            ip,
            reservationId,
            (totalUsage.inputTokens ?? 0) + (totalUsage.outputTokens ?? 0),
          ),
        );
      },
      // Aborted or errored streams forfeit the full reservation: the model
      // may already have consumed tokens, and refunding on abort would let
      // clients bypass the budget by cancelling streams.
      onAbort: async () => {
        await finalize(() => forfeitBudget(env.RATE_LIMITER, ip, reservationId));
      },
      onError: async (error) => {
        console.error(error);
        await finalize(() => forfeitBudget(env.RATE_LIMITER, ip, reservationId));
      },
    });

    return result.toUIMessageStreamResponse({
      headers: rateLimitHeaders(reservation.remaining, reservation.resetAt),
    });
  } catch (error) {
    // The model call never started: return the reservation in full.
    await finalize(() => releaseBudget(env.RATE_LIMITER, ip, reservationId));
    throw error;
  }
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
