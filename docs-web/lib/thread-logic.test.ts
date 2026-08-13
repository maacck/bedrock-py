import { expect, it } from "vitest";
import { truncateMessages, type ThreadMessage } from "@/lib/thread-logic";

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
