import { expect, it } from "vitest";
import {
  abortThreadState,
  appendThreadState,
  openThreadState,
  truncateMessages,
  type ThreadMessage,
  type ThreadSnapshot,
} from "@/lib/thread-logic";

function msg(role: ThreadMessage["role"], content: string): ThreadMessage {
  return { role, content };
}

it("drops oldest messages over the count bound", () => {
  const messages = Array.from({ length: 25 }, (_, i) => msg(i % 2 ? "assistant" : "user", `msg ${i}`));
  const out = truncateMessages(messages);
  expect(out.length).toBe(20);
  expect(out[0].content).toBe("msg 5"); // oldest 5 dropped
});

it("drops oldest messages over the token bound down to the two-message floor", () => {
  // 25 x 20_000 chars ≈ 125_000 estimated tokens. Even the last TWO messages
  // alone (≈10_000 tokens) exceed HISTORY_MAX_TOKENS, so trimming must stop at
  // the two-message floor instead of ever emptying the thread.
  const messages = Array.from({ length: 25 }, (_, i) => msg(i % 2 ? "assistant" : "user", "x".repeat(20_000)));
  const out = truncateMessages(messages);
  expect(out.length).toBe(2); // floor: keep the most recent pair even when over budget
  expect(out.reduce((sum, m) => sum + Math.ceil(m.content.length / 4), 0)).toBeGreaterThan(8_192);
});

it("drops oldest messages over the token bound when the tail exceeds it", () => {
  // 10 x 4000 chars ≈ 10_000 tokens: the tail of 6 (≈6000) fits, so 4 are dropped.
  const messages = Array.from({ length: 10 }, (_, i) => msg(i % 2 ? "assistant" : "user", "x".repeat(4000)));
  const out = truncateMessages(messages);
  expect(out.reduce((sum, m) => sum + Math.ceil(m.content.length / 4), 0)).toBeLessThanOrEqual(8_192);
  expect(out.length).toBeGreaterThan(2);
});

it("never empties a thread with recent messages", () => {
  const out = truncateMessages([msg("user", "hi"), msg("assistant", "hello")]);
  expect(out.length).toBe(2);
});

function snap(over: Partial<ThreadSnapshot> = {}): ThreadSnapshot {
  return { deviceId: "d1", messages: [], inFlight: false, createdAt: 1_000, updatedAt: 1_000, ...over };
}

it("open creates a thread and marks it in-flight", () => {
  const r = openThreadState(null, "d1", 1_000);
  expect(r.status).toBe(200);
  if (r.status === 200) {
    expect(r.thread.inFlight).toBe(true);
    expect(r.thread.deviceId).toBe("d1");
    expect(r.thread.messages).toEqual([]);
  }
});

it("open rejects device mismatch with 404", () => {
  expect(openThreadState(snap(), "other-device", 1_000)).toEqual({ status: 404 });
});

it("open returns 409 while another request is in flight", () => {
  expect(openThreadState(snap({ inFlight: true, updatedAt: Date.now() }), "d1", Date.now())).toEqual({ status: 409 });
});

it("open resets a stale in-flight lock older than 10 minutes", () => {
  const r = openThreadState(snap({ inFlight: true, updatedAt: 1_000 }), "d1", 1_000 + 11 * 60_000);
  expect(r.status).toBe(200);
  if (r.status === 200) expect(r.thread.inFlight).toBe(true);
});

it("append adds user+assistant, clears in-flight, and truncates", () => {
  const r = appendThreadState(
    snap({ inFlight: true, messages: [{ role: "user", content: "old" }] }),
    "d1",
    "new user",
    "new answer",
    2_000,
  );
  expect(r.status).toBe(200);
  if (r.status === 200) {
    expect(r.thread.messages.map((m) => m.content)).toEqual(["old", "new user", "new answer"]);
    expect(r.thread.inFlight).toBe(false);
  }
});

it("append rejects device mismatch with 404", () => {
  expect(appendThreadState(snap(), "other", "u", "a", 2_000)).toEqual({ status: 404 });
});

it("abort clears in-flight and rejects device mismatch", () => {
  const r = abortThreadState(snap({ inFlight: true }), "d1", 2_000);
  expect(r.status).toBe(200);
  if (r.status === 200) expect(r.thread.inFlight).toBe(false);
  expect(abortThreadState(snap(), "other", 2_000)).toEqual({ status: 404 });
});
