import { describe, expect, it } from "vitest";
import { TokenBudgetWindow, RATE_LIMIT } from "./rate-limit-budget";
import {
  estimateTokens,
  getClientIp,
  isDeclaredBodyTooLarge,
  parseAndValidateChatBody,
  parseAndValidateChatRequest,
  releaseBudget,
  reserveBudget,
  settleBudget,
  type RateLimiterNamespace,
} from "./rate-limit";

function reqWithHeaders(headers: Record<string, string>): Request {
  return new Request("https://docs.example.com/api/chat", { headers });
}

describe("getClientIp", () => {
  it("trusts only cf-connecting-ip", () => {
    expect(
      getClientIp(reqWithHeaders({ "cf-connecting-ip": "203.0.113.7" })),
    ).toBe("203.0.113.7");
  });

  it("returns null when cf-connecting-ip is missing", () => {
    expect(getClientIp(reqWithHeaders({}))).toBeNull();
  });

  it("ignores a spoofed x-forwarded-for header", () => {
    expect(
      getClientIp(reqWithHeaders({ "x-forwarded-for": "203.0.113.7" })),
    ).toBeNull();
    expect(
      getClientIp(
        reqWithHeaders({
          "cf-connecting-ip": "198.51.100.9",
          "x-forwarded-for": "203.0.113.7",
        }),
      ),
    ).toBe("198.51.100.9");
  });
});

describe("parseAndValidateChatBody", () => {
  const validBody = JSON.stringify({
    messages: [
      { role: "user", parts: [{ type: "text", text: "How do I use signals?" }] },
    ],
  });

  it("accepts a valid chat body and estimates input tokens", () => {
    const result = parseAndValidateChatBody(validBody);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.estimatedInputTokens).toBeGreaterThan(0);
      expect(result.messages).toHaveLength(1);
    }
  });

  it("rejects invalid JSON with a stable error code", () => {
    const result = parseAndValidateChatBody("{not json");
    expect(result).toMatchObject({ ok: false, status: 400, code: "invalid_json" });
  });

  it("rejects missing or empty messages", () => {
    expect(parseAndValidateChatBody("{}")).toMatchObject({
      ok: false,
      code: "invalid_messages",
    });
    expect(parseAndValidateChatBody('{"messages":[]}')).toMatchObject({
      ok: false,
      code: "invalid_messages",
    });
  });

  it("rejects more messages than the cap", () => {
    const messages = Array.from(
      { length: RATE_LIMIT.MAX_MESSAGES + 1 },
      () => ({ role: "user", parts: [{ type: "text", text: "hi" }] }),
    );
    const result = parseAndValidateChatBody(JSON.stringify({ messages }));
    expect(result).toMatchObject({ ok: false, status: 400, code: "too_many_messages" });
  });

  it("rejects input beyond the estimated token cap before any model call", () => {
    const big = "x".repeat(RATE_LIMIT.MAX_INPUT_TOKENS * 4 + 4);
    const result = parseAndValidateChatBody(
      JSON.stringify({
        messages: [{ role: "user", parts: [{ type: "text", text: big }] }],
      }),
    );
    expect(result).toMatchObject({ ok: false, status: 400, code: "input_too_large" });
  });

  it("rejects bodies beyond the byte cap", () => {
    const padding = "y".repeat(RATE_LIMIT.MAX_BODY_BYTES);
    const result = parseAndValidateChatBody(
      `{"messages":[{"role":"user","content":"${padding}"}]}`,
    );
    expect(result).toMatchObject({ ok: false, status: 413, code: "body_too_large" });
  });
});

describe("estimateTokens", () => {
  it("approximates 4 characters per token", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("abcde")).toBe(2);
  });
});

describe("isDeclaredBodyTooLarge", () => {
  it("rejects early when content-length exceeds the cap", () => {
    expect(
      isDeclaredBodyTooLarge(
        reqWithHeaders({ "content-length": String(RATE_LIMIT.MAX_BODY_BYTES + 1) }),
      ),
    ).toBe(true);
  });

  it("accepts missing, invalid, or in-limit content-length", () => {
    expect(isDeclaredBodyTooLarge(reqWithHeaders({}))).toBe(false);
    expect(
      isDeclaredBodyTooLarge(reqWithHeaders({ "content-length": "not-a-number" })),
    ).toBe(false);
    expect(
      isDeclaredBodyTooLarge(
        reqWithHeaders({ "content-length": String(RATE_LIMIT.MAX_BODY_BYTES) }),
      ),
    ).toBe(false);
  });
});

/**
 * Fake namespace routing every call for an IP to one shared budget, the
 * same way the Durable Object serializes all requests for one instance.
 */
function fakeNamespace(shared: TokenBudgetWindow): RateLimiterNamespace {
  return {
    idFromName: (name: string) => name,
    get: () => ({
      async fetch(input: string, init?: RequestInit): Promise<Response> {
        const path = new URL(input).pathname;
        const payload = JSON.parse(String(init?.body ?? "{}"));
        if (path === "/reserve") {
          return Response.json(shared.reserve(payload.tokens));
        }
        if (path === "/settle") {
          shared.settle(payload.reservationId, payload.actualTokens);
          return Response.json({ ok: true });
        }
        if (path === "/release") {
          shared.release(payload.reservationId);
          return Response.json({ ok: true });
        }
        return Response.json({ error: "not_found" }, { status: 404 });
      },
    }),
  };
}

describe("budget client lifecycle", () => {
  it("10 concurrent same-IP requests never exceed the window budget", async () => {
    const budget = new TokenBudgetWindow();
    const ns = fakeNamespace(budget);
    const perRequest = 6_000;

    const reservations = await Promise.all(
      Array.from({ length: 10 }, () => reserveBudget(ns, "203.0.113.7", perRequest)),
    );
    const allowed = reservations.filter((r) => r.allowed);
    expect(allowed).toHaveLength(Math.floor(RATE_LIMIT.TOKENS_PER_WINDOW / perRequest));
    expect(allowed.every((r) => r.reservationId)).toBe(true);

    // All streams settle; the balance stays consistent and non-negative.
    await Promise.all(
      allowed.map((r) => settleBudget(ns, "203.0.113.7", r.reservationId!, 2_000)),
    );
    expect(budget.committed).toBe(allowed.length * 2_000);
  });

  it("release after a failed model start returns the reservation", async () => {
    const budget = new TokenBudgetWindow();
    const ns = fakeNamespace(budget);
    const r = await reserveBudget(ns, "203.0.113.7", 5_000);
    await releaseBudget(ns, "203.0.113.7", r.reservationId!);
    expect(budget.committed).toBe(0);
  });
});

describe("parseAndValidateChatRequest", () => {
  const valid = () => JSON.stringify({ query: "What is Bedrock?", device_id: "123e4567-e89b-12d3-a456-426614174000" });

  it("accepts a valid query + device_id", () => {
    const r = parseAndValidateChatRequest(valid());
    expect(r).toMatchObject({ ok: true, query: "What is Bedrock?", threadId: null, location: null });
  });

  it("rejects empty or missing query", () => {
    expect(parseAndValidateChatRequest(JSON.stringify({ query: "   ", device_id: "123e4567-e89b-12d3-a456-426614174000" })).ok).toBe(false);
    expect(parseAndValidateChatRequest(JSON.stringify({ device_id: "123e4567-e89b-12d3-a456-426614174000" })).ok).toBe(false);
  });

  it("rejects query over MAX_QUERY_CHARS", () => {
    const r = parseAndValidateChatRequest(JSON.stringify({ query: "x".repeat(RATE_LIMIT.MAX_QUERY_CHARS + 1), device_id: "123e4567-e89b-12d3-a456-426614174000" }));
    expect(r).toMatchObject({ ok: false, code: "query_too_long" });
  });

  it("requires a UUID device_id", () => {
    for (const device_id of ["not-a-uuid", "", "123"]) {
      const r = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id }));
      expect(r).toMatchObject({ ok: false, code: "invalid_device" });
    }
  });

  it("accepts a UUID thread_id and rejects malformed ones", () => {
    const ok = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id: "123e4567-e89b-12d3-a456-426614174000", thread_id: "123e4567-e89b-12d3-a456-426614174000" }));
    expect(ok).toMatchObject({ ok: true, threadId: "123e4567-e89b-12d3-a456-426614174000" });
    const bad = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id: "123e4567-e89b-12d3-a456-426614174000", thread_id: "../etc" }));
    expect(bad).toMatchObject({ ok: false, code: "invalid_thread" });
  });

  it("validates context.location and ignores unknown context keys", () => {
    const ok = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id: "123e4567-e89b-12d3-a456-426614174000", context: { location: "Shanghai", other: "ignored" } }));
    expect(ok).toMatchObject({ ok: true, location: "Shanghai" });
    const long = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id: "123e4567-e89b-12d3-a456-426614174000", context: { location: "x".repeat(257) } }));
    expect(long).toMatchObject({ ok: false, code: "invalid_context" });
    const notObj = parseAndValidateChatRequest(JSON.stringify({ query: "hi", device_id: "123e4567-e89b-12d3-a456-426614174000", context: "nope" }));
    expect(notObj).toMatchObject({ ok: false, code: "invalid_context" });
  });

  it("rejects bodies over MAX_BODY_BYTES", () => {
    // ~70 KB body — exceeds the 64 KB cap still in place during Task 6 (the
    // cap only drops to 16 KB in Task 9), so the body check fires BEFORE the
    // query-length check and must return body_too_large.
    const big = JSON.stringify({ query: "x".repeat(70_000), device_id: "123e4567-e89b-12d3-a456-426614174000" });
    expect(parseAndValidateChatRequest(big)).toMatchObject({ ok: false, code: "body_too_large" });
  });

  it("rejects invalid JSON", () => {
    expect(parseAndValidateChatRequest("{not json")).toMatchObject({ ok: false, code: "invalid_json" });
  });
});
