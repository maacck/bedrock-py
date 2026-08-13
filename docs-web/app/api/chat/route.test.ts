import { beforeEach, describe, expect, it, vi } from "vitest";

const reserveBudget = vi.fn();
const settleBudget = vi.fn();
const forfeitBudget = vi.fn();
const releaseBudget = vi.fn();
const openThread = vi.fn();
const appendThread = vi.fn();
const abortThread = vi.fn();
const parseAndValidateChatRequest = vi.fn();
const getClientIp = vi.fn();
const streamText = vi.fn();
const toUIMessageStreamResponse = vi.fn(
  // Copy the headers option into the response so header assertions work.
  (options?: { headers?: Record<string, string> }) =>
    new Response("stream", { status: 200, headers: options?.headers }),
);

vi.mock("@/lib/rate-limit", () => ({
  RATE_LIMIT: { TOKENS_PER_WINDOW: 50_000, RESERVED_OUTPUT_TOKENS: 4_096 },
  estimateTokens: (t: string) => Math.ceil(t.length / 4),
  getClientIp: (...a: unknown[]) => getClientIp(...a),
  isDeclaredBodyTooLarge: () => false,
  parseAndValidateChatRequest: (...a: unknown[]) => parseAndValidateChatRequest(...a),
  reserveBudget: (...a: unknown[]) => reserveBudget(...a),
  settleBudget: (...a: unknown[]) => settleBudget(...a),
  forfeitBudget: (...a: unknown[]) => forfeitBudget(...a),
  releaseBudget: (...a: unknown[]) => releaseBudget(...a),
}));

vi.mock("@/lib/thread-client", () => ({
  openThread: (...a: unknown[]) => openThread(...a),
  appendThread: (...a: unknown[]) => appendThread(...a),
  abortThread: (...a: unknown[]) => abortThread(...a),
}));

vi.mock("@/lib/source", () => ({ source: { getPages: () => [] } }));

vi.mock("@opennextjs/cloudflare", () => ({
  getCloudflareContext: () => ({ env: { AI: {}, RATE_LIMITER: {}, THREAD_STORE: {} } }),
}));

vi.mock("ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("ai")>();
  return { ...actual, streamText: (...a: unknown[]) => streamText(...a) };
});

vi.mock("workers-ai-provider", () => ({ createWorkersAI: () => () => ({}) }));

import { POST } from "./route";

const UUID = "123e4567-e89b-12d3-a456-426614174000";

function post(body: string) {
  return POST(
    new Request("https://example.com/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json", "cf-connecting-ip": "203.0.113.9" },
      body,
    }),
  );
}

describe("POST /api/chat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    parseAndValidateChatRequest.mockReturnValue({
      ok: true,
      query: "what is bedrock?",
      threadId: null,
      deviceId: UUID,
      location: null,
    });
    getClientIp.mockReturnValue("203.0.113.9");
    openThread.mockResolvedValue({ ok: true, thread: { messages: [] } });
    reserveBudget.mockResolvedValue({
      allowed: true,
      reservationId: "r1",
      used: 100,
      remaining: 49_900,
      resetAt: 1_000_000,
    });
  });

  it("rejects requests without cf-connecting-ip (403)", async () => {
    getClientIp.mockReturnValue(null);
    const res = await POST(new Request("https://example.com/api/chat", { method: "POST", body: "{}" }));
    expect(res.status).toBe(403);
    expect(openThread).not.toHaveBeenCalled();
  });

  it("rejects invalid payloads with the validation status (400)", async () => {
    parseAndValidateChatRequest.mockReturnValue({ ok: false, status: 400, code: "invalid_query", message: "bad" });
    const res = await post("{}");
    expect(res.status).toBe(400);
    expect(openThread).not.toHaveBeenCalled();
  });

  it("maps thread ownership failure to 404 and never reserves", async () => {
    parseAndValidateChatRequest.mockReturnValue({
      ok: true,
      query: "hi",
      threadId: UUID,
      deviceId: UUID,
      location: null,
    });
    openThread.mockResolvedValue({ ok: false, status: 404 });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID, thread_id: UUID }));
    expect(res.status).toBe(404);
    expect(reserveBudget).not.toHaveBeenCalled();
  });

  it("maps in-flight conflict to 409", async () => {
    parseAndValidateChatRequest.mockReturnValue({
      ok: true,
      query: "hi",
      threadId: UUID,
      deviceId: UUID,
      location: null,
    });
    openThread.mockResolvedValue({ ok: false, status: 409 });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID, thread_id: UUID }));
    expect(res.status).toBe(409);
  });

  it("aborts the thread lock and returns 429 with X-Thread-Id when rate limited", async () => {
    parseAndValidateChatRequest.mockReturnValue({
      ok: true,
      query: "hi",
      threadId: UUID,
      deviceId: UUID,
      location: null,
    });
    reserveBudget.mockResolvedValue({ allowed: false, reservationId: null, used: 50_000, remaining: 0, resetAt: 1_000_000 });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID, thread_id: UUID }));
    expect(res.status).toBe(429);
    expect(abortThread).toHaveBeenCalledTimes(1);
    expect(res.headers.get("X-Thread-Id")).toBe(UUID);
  });

  it("rate-limits a new thread without minting a ThreadStore DO", async () => {
    reserveBudget.mockResolvedValue({
      allowed: false,
      reservationId: null,
      used: 50_000,
      remaining: 0,
      resetAt: 1_000_000,
    });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID }));
    expect(res.status).toBe(429);
    expect(await res.json()).toMatchObject({ error: "rate_limited" });
    expect(openThread).not.toHaveBeenCalled();
    expect(abortThread).not.toHaveBeenCalled();
    expect(reserveBudget).toHaveBeenCalledTimes(1);
  });

  it("streams with X-Thread-Id and reserves query + history + system + output", async () => {
    parseAndValidateChatRequest.mockReturnValue({
      ok: true,
      query: "hi",
      threadId: UUID,
      deviceId: UUID,
      location: null,
    });
    openThread.mockResolvedValue({
      ok: true,
      thread: { messages: [{ role: "user", content: "x".repeat(100) }] },
    });
    streamText.mockReturnValue({ toUIMessageStreamResponse });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID, thread_id: UUID }));
    expect(res.status).toBe(200);
    expect(res.headers.get("X-Thread-Id")).toBe(UUID);
    expect(reserveBudget).toHaveBeenCalledTimes(1);
    const tokens = reserveBudget.mock.calls[0][2] as number;
    expect(tokens).toBeGreaterThan(4_096); // history (25) + query + system + output reservation
    expect(openThread).toHaveBeenCalledWith(expect.anything(), UUID, UUID);
    expect(streamText).toHaveBeenCalledTimes(1);
  });

  it("calls appendThread + settleBudget on finish, and abortThread + releaseBudget when streamText throws", async () => {
    streamText.mockReturnValue({ toUIMessageStreamResponse });
    const res = await post(JSON.stringify({ query: "hi", device_id: UUID }));
    expect(res.status).toBe(200);
    // Trigger the onFinish callback captured by the mocked streamText.
    const { onFinish } = streamText.mock.calls[0][0] as {
      onFinish: (r: unknown) => Promise<void>;
    };
    await onFinish({ text: "answer", totalUsage: { inputTokens: 10, outputTokens: 20 } });
    expect(appendThread).toHaveBeenCalledTimes(1);
    expect(settleBudget).toHaveBeenCalledWith(expect.anything(), "203.0.113.9", "r1", 30);

    // A synchronous streamText failure: the handler releases the thread lock
    // and the reservation, then RE-THROWS (the framework surfaces the error).
    streamText.mockImplementationOnce(() => {
      throw new Error("sync failure");
    });
    await expect(post(JSON.stringify({ query: "hi", device_id: UUID }))).rejects.toThrow("sync failure");
    expect(abortThread).toHaveBeenCalledTimes(1);
    expect(releaseBudget).toHaveBeenCalledTimes(1);
    expect(settleBudget).toHaveBeenCalledTimes(1); // only the first request settled
  });
});
