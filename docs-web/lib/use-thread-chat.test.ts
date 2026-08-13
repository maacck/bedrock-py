import { expect, it } from "vitest";
import { buildChatRequest, mergeStreamMessage } from "@/lib/use-thread-chat";
import type { UIMessage } from "ai";

const UUID = "123e4567-e89b-12d3-a456-426614174000";

function textMessage(id: string, role: "user" | "assistant", text: string): UIMessage {
  return { id, role, parts: [{ type: "text", text }] } as UIMessage;
}

it("builds the chat request body", () => {
  expect(buildChatRequest("hi", null, UUID)).toEqual({
    query: "hi",
    thread_id: null,
    device_id: UUID,
    context: null,
  });
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
