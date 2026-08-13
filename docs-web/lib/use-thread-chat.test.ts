import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { expect, it } from "vitest";
import {
  beginNewThread,
  buildChatRequest,
  isCurrentGeneration,
  mergeStreamMessage,
  readStoredThreadId,
  useThreadChat,
} from "@/lib/use-thread-chat";
import type { UIMessage } from "ai";

const UUID = "123e4567-e89b-12d3-a456-426614174000";

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] } as UIMessage;
}

it("builds the chat request body", () => {
  const unset = buildChatRequest("hi", null, UUID);
  expect(unset).toEqual({
    query: "hi",
    device_id: UUID,
    context: null,
  });
  expect(unset).not.toHaveProperty("thread_id");
  expect(buildChatRequest("hi", "t", UUID, "Shanghai")).toMatchObject({
    thread_id: "t",
    context: { location: "Shanghai" },
  });
});

it("replaces the placeholder assistant message with each streamed snapshot", () => {
  const prev = [textMessage("u1", "user", "hi"), { id: "placeholder", role: "assistant", parts: [] } as UIMessage];
  const incoming = textMessage("sdk-id", "assistant", "answer");
  const out = mergeStreamMessage(prev, incoming);
  expect(out).toHaveLength(2);
  expect(out[1].id).toBe("sdk-id");
  expect((out[1].parts[0] as { text: string }).text).toBe("answer");
});

it("updates only the latest assistant placeholder across turns (multi-turn)", () => {
  const prev = [
    textMessage("u1", "user", "first"),
    textMessage("a1", "assistant", "first answer"),
    textMessage("u2", "user", "second"),
    { id: "placeholder", role: "assistant", parts: [] } as UIMessage,
  ];
  const incoming = textMessage("sdk-id", "assistant", "second answer");
  const out = mergeStreamMessage(prev, incoming);
  expect(out[1].id).toBe("a1");
  expect((out[1].parts[0] as { text: string }).text).toBe("first answer"); // untouched
  expect(out[3].id).toBe("sdk-id");
  expect((out[3].parts[0] as { text: string }).text).toBe("second answer");
});

it("appends when no assistant placeholder exists", () => {
  const out = mergeStreamMessage([textMessage("u1", "user", "hi")], textMessage("a1", "assistant", "answer"));
  expect(out).toHaveLength(2);
  expect(out[1].id).toBe("a1");
});

function Probe() {
  useThreadChat();
  return null;
}

it("does not read localStorage when the hook is constructed on the server", () => {
  expect(() => renderToString(createElement(Probe))).not.toThrow();
});

it("returns no stored thread id when localStorage is unavailable", () => {
  expect(readStoredThreadId()).toBeNull();
});

it("aborts the in-flight request and bumps generation when starting a new thread", () => {
  const controller = new AbortController();
  const abortRef = { current: controller };
  const generationRef = { current: 3 };
  const threadIdRef = { current: "old-thread" as string | null };

  beginNewThread(threadIdRef, abortRef, generationRef);

  expect(controller.signal.aborted).toBe(true);
  expect(generationRef.current).toBe(4);
  expect(threadIdRef.current).toBeNull();
  expect(isCurrentGeneration(3, generationRef.current)).toBe(false);
  expect(isCurrentGeneration(4, generationRef.current)).toBe(true);
});
