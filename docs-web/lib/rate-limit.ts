import { RATE_LIMIT, type ReserveResult } from "./rate-limit-budget";

export { RATE_LIMIT };

/**
 * Only the Cloudflare-injected `cf-connecting-ip` header is trusted.
 * `x-forwarded-for` is client-controllable and must never be used as a
 * rate-limit identity. Returns null when the trusted header is missing so
 * callers can reject the request instead of falling back to a spoofable
 * or shared identity.
 */
export function getClientIp(req: Request): string | null {
  const ip = req.headers.get("cf-connecting-ip")?.trim();
  return ip ? ip : null;
}

/** Rough token estimate: ~4 characters per token for English/code text. */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

interface MessageLike {
  role?: unknown;
  content?: unknown;
  parts?: unknown;
}

function messageText(message: MessageLike): string {
  if (typeof message.content === "string") return message.content;
  if (!Array.isArray(message.parts)) return "";
  let text = "";
  for (const part of message.parts) {
    if (
      part &&
      typeof part === "object" &&
      (part as { type?: unknown }).type === "text" &&
      typeof (part as { text?: unknown }).text === "string"
    ) {
      text += (part as { text: string }).text;
    }
  }
  return text;
}

export type ChatBodyValidation =
  | { ok: true; messages: MessageLike[]; estimatedInputTokens: number }
  | { ok: false; status: number; code: string; message: string };

/**
 * Early rejection based on the declared content-length, before the body
 * is read. The header can be forged, so the post-read check in
 * parseAndValidateChatBody remains the authoritative bound.
 */
export function isDeclaredBodyTooLarge(req: Request): boolean {
  const declared = Number(req.headers.get("content-length"));
  return Number.isFinite(declared) && declared > RATE_LIMIT.MAX_BODY_BYTES;
}

/**
 * Validate and bound the chat request body before any model call:
 * request size, message count and estimated input tokens are all capped.
 */
export function parseAndValidateChatBody(rawBody: string): ChatBodyValidation {
  if (new TextEncoder().encode(rawBody).length > RATE_LIMIT.MAX_BODY_BYTES) {
    return {
      ok: false,
      status: 413,
      code: "body_too_large",
      message: `Request body exceeds the ${RATE_LIMIT.MAX_BODY_BYTES} byte limit.`,
    };
  }

  let body: unknown;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return {
      ok: false,
      status: 400,
      code: "invalid_json",
      message: "Request body must be valid JSON.",
    };
  }

  const messages = (body as { messages?: unknown })?.messages;
  if (
    !Array.isArray(messages) ||
    messages.length === 0 ||
    messages.some(
      (m) => !m || typeof m !== "object" || typeof m.role !== "string",
    )
  ) {
    return {
      ok: false,
      status: 400,
      code: "invalid_messages",
      message: "`messages` must be a non-empty array of chat messages.",
    };
  }

  if (messages.length > RATE_LIMIT.MAX_MESSAGES) {
    return {
      ok: false,
      status: 400,
      code: "too_many_messages",
      message: `At most ${RATE_LIMIT.MAX_MESSAGES} messages are allowed per request.`,
    };
  }

  const estimatedInputTokens = messages.reduce(
    (sum, m) => sum + estimateTokens(messageText(m as MessageLike)),
    0,
  );
  if (estimatedInputTokens > RATE_LIMIT.MAX_INPUT_TOKENS) {
    return {
      ok: false,
      status: 400,
      code: "input_too_large",
      message: `Estimated input of ${estimatedInputTokens} tokens exceeds the ${RATE_LIMIT.MAX_INPUT_TOKENS} token limit.`,
    };
  }

  return { ok: true, messages: messages as MessageLike[], estimatedInputTokens };
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type ChatRequestValidation =
  | { ok: true; query: string; threadId: string | null; deviceId: string; location: string | null }
  | { ok: false; status: number; code: string; message: string };

/**
 * Validate the chat request: a single `query` string, an optional server-managed
 * `thread_id`, a client-generated `device_id` (ownership check only — NOT an auth
 * boundary), and an optional whitelisted `context`. The client no longer replays
 * message arrays, which eliminates the system-role and tool-part injection surface.
 */
export function parseAndValidateChatRequest(rawBody: string): ChatRequestValidation {
  if (new TextEncoder().encode(rawBody).length > RATE_LIMIT.MAX_BODY_BYTES) {
    return {
      ok: false,
      status: 413,
      code: "body_too_large",
      message: `Request body exceeds the ${RATE_LIMIT.MAX_BODY_BYTES} byte limit.`,
    };
  }

  let body: unknown;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return { ok: false, status: 400, code: "invalid_json", message: "Request body must be valid JSON." };
  }

  const b = body as { query?: unknown; thread_id?: unknown; device_id?: unknown; context?: unknown };

  if (typeof b.query !== "string" || b.query.trim().length === 0) {
    return { ok: false, status: 400, code: "invalid_query", message: "`query` must be a non-empty string." };
  }
  const query = b.query.trim();
  if (query.length > RATE_LIMIT.MAX_QUERY_CHARS) {
    return {
      ok: false,
      status: 400,
      code: "query_too_long",
      message: `\`query\` must be at most ${RATE_LIMIT.MAX_QUERY_CHARS} characters.`,
    };
  }

  if (typeof b.device_id !== "string" || !UUID_RE.test(b.device_id)) {
    return { ok: false, status: 400, code: "invalid_device", message: "`device_id` must be a UUID." };
  }

  let threadId: string | null = null;
  if (b.thread_id != null) {
    if (typeof b.thread_id !== "string" || !UUID_RE.test(b.thread_id)) {
      return { ok: false, status: 400, code: "invalid_thread", message: "`thread_id` must be a UUID." };
    }
    threadId = b.thread_id;
  }

  let location: string | null = null;
  if (b.context != null) {
    if (typeof b.context !== "object" || b.context === null || Array.isArray(b.context)) {
      return { ok: false, status: 400, code: "invalid_context", message: "`context` must be an object." };
    }
    const ctx = b.context as { location?: unknown };
    if (ctx.location != null) {
      if (typeof ctx.location !== "string" || ctx.location.length > 256) {
        return {
          ok: false,
          status: 400,
          code: "invalid_context",
          message: "`context.location` must be a short string.",
        };
      }
      location = ctx.location;
    }
  }

  return { ok: true, query, threadId, deviceId: b.device_id, location };
}

/** Minimal structural types so the client helpers stay unit-testable. */
export interface RateLimiterStub {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

export interface RateLimiterNamespace {
  idFromName(name: string): unknown;
  get(id: unknown): RateLimiterStub;
}

function getStub(ns: RateLimiterNamespace, ip: string): RateLimiterStub {
  return ns.get(ns.idFromName(ip));
}

async function callLimiter(
  stub: RateLimiterStub,
  path: string,
  payload: Record<string, unknown>,
): Promise<Response> {
  return stub.fetch(`https://rate-limiter.internal${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function reserveBudget(
  ns: RateLimiterNamespace,
  ip: string,
  tokens: number,
): Promise<ReserveResult> {
  const res = await callLimiter(getStub(ns, ip), "/reserve", { tokens });
  return (await res.json()) as ReserveResult;
}

export async function settleBudget(
  ns: RateLimiterNamespace,
  ip: string,
  reservationId: string,
  actualTokens: number,
): Promise<void> {
  await callLimiter(getStub(ns, ip), "/settle", {
    reservationId,
    actualTokens,
  });
}

export async function forfeitBudget(
  ns: RateLimiterNamespace,
  ip: string,
  reservationId: string,
): Promise<void> {
  await callLimiter(getStub(ns, ip), "/forfeit", { reservationId });
}

export async function releaseBudget(
  ns: RateLimiterNamespace,
  ip: string,
  reservationId: string,
): Promise<void> {
  await callLimiter(getStub(ns, ip), "/release", { reservationId });
}
