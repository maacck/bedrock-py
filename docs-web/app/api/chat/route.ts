import {
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
  parseAndValidateChatRequest,
  releaseBudget,
  reserveBudget,
  settleBudget,
} from "@/lib/rate-limit";
import { abortThread, appendThread, openThread } from "@/lib/thread-client";

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

  const parsed = parseAndValidateChatRequest(await req.text());
  if (!parsed.ok) {
    return Response.json({ error: parsed.code, message: parsed.message }, { status: parsed.status });
  }

  const { env } = getCloudflareContext();

  const contextLine = parsed.location ? `\n[Client Context: location: ${parsed.location}]` : "";
  const userText = parsed.query + contextLine;

  // Server-generated thread id when the client starts a new conversation.
  const threadId = parsed.threadId ?? crypto.randomUUID();
  const isNewThread = parsed.threadId === null;

  function threadOpenError(status: 404 | 409 | 502): Response {
    if (status === 404) {
      return Response.json(
        { error: "thread_not_found", message: "Thread not found for this device." },
        { status: 404 },
      );
    }
    if (status === 409) {
      return Response.json(
        { error: "thread_busy", message: "Another request is already running on this thread." },
        { status: 409 },
      );
    }
    return Response.json({ error: "thread_error", message: "Thread store unavailable." }, { status: 502 });
  }

  function rateLimitedResponse(
    reservation: { used: number; remaining: number; resetAt: number },
  ): Response {
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
            Math.max(0, reservation.resetAt - Math.floor(Date.now() / 1000)),
          ),
          "X-Thread-Id": threadId,
        },
      },
    );
  }

  let history: { role: "user" | "assistant"; content: string }[] = [];
  let reservationTokens =
    estimateTokens(userText) +
    estimateTokens(systemPrompt) +
    RATE_LIMIT.RESERVED_OUTPUT_TOKENS;

  // New threads reserve first so a denied budget never mints a Durable Object.
  // Existing threads must open first to read history into the reservation.
  let reservation;
  if (isNewThread) {
    reservation = await reserveBudget(env.RATE_LIMITER, ip, reservationTokens);
    if (!reservation.allowed || !reservation.reservationId) {
      return rateLimitedResponse(reservation);
    }
    const opened = await openThread(env.THREAD_STORE, threadId, parsed.deviceId);
    if (!opened.ok) {
      await releaseBudget(env.RATE_LIMITER, ip, reservation.reservationId);
      return threadOpenError(opened.status);
    }
    history = opened.thread.messages;
  } else {
    const opened = await openThread(env.THREAD_STORE, threadId, parsed.deviceId);
    if (!opened.ok) {
      return threadOpenError(opened.status);
    }
    // From here on, EVERY exit path must release the thread lock via abortThread.
    history = opened.thread.messages;
    const historyTokens = history.reduce((sum, m) => sum + estimateTokens(m.content), 0);
    reservationTokens += historyTokens;
    reservation = await reserveBudget(env.RATE_LIMITER, ip, reservationTokens);
    if (!reservation.allowed || !reservation.reservationId) {
      await abortThread(env.THREAD_STORE, threadId, parsed.deviceId);
      return rateLimitedResponse(reservation);
    }
  }

  const reservationId = reservation.reservationId;
  let finalized = false;
  const finalize = async (action: () => Promise<void>): Promise<void> => {
    if (finalized) return;
    finalized = true;
    await action();
  };

  const workersai = createWorkersAI({ binding: env.AI });

  try {
    const result = streamText({
      model: workersai(process.env.WORKER_AI_MODEL ?? "@cf/moonshotai/kimi-k2.5"),
      stopWhen: stepCountIs(5),
      abortSignal: req.signal,
      tools: {
        search: searchTool,
      },
      system: systemPrompt,
      // Server-owned history (plain text user/assistant turns) + this turn's query.
      // The client never supplies roles or tool parts — the M-2/M-3 injection
      // surface is closed by the contract.
      messages: [
        ...history.map((m) => ({ role: m.role, content: m.content })),
        { role: "user", content: userText },
      ],
      toolChoice: "auto",
      onFinish: async ({ text, totalUsage }) => {
        await finalize(async () => {
          await appendThread(
            env.THREAD_STORE,
            threadId,
            parsed.deviceId,
            userText,
            text ?? "",
          );
          const actual =
            (totalUsage.inputTokens ?? 0) + (totalUsage.outputTokens ?? 0);
          // L-3: when the provider reports no usage, charge the full reservation
          // (fail conservative) instead of settling zero.
          await settleBudget(
            env.RATE_LIMITER,
            ip,
            reservationId,
            actual > 0 ? actual : reservationTokens,
          );
        });
      },
      // Aborted or errored streams forfeit the full reservation and release the
      // thread lock: refunding on abort would let clients bypass the budget.
      onAbort: async () => {
        await finalize(async () => {
          await abortThread(env.THREAD_STORE, threadId, parsed.deviceId);
          await forfeitBudget(env.RATE_LIMITER, ip, reservationId);
        });
      },
      onError: async (error) => {
        console.error(error);
        await finalize(async () => {
          await abortThread(env.THREAD_STORE, threadId, parsed.deviceId);
          await forfeitBudget(env.RATE_LIMITER, ip, reservationId);
        });
      },
    });

    return result.toUIMessageStreamResponse({
      headers: {
        ...rateLimitHeaders(reservation.remaining, reservation.resetAt),
        "X-Thread-Id": threadId,
      },
    });
  } catch (error) {
    // The model call never started: release the reservation in full and the
    // thread lock, then surface the error.
    await finalize(async () => {
      await abortThread(env.THREAD_STORE, threadId, parsed.deviceId);
      await releaseBudget(env.RATE_LIMITER, ip, reservationId);
    });
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
