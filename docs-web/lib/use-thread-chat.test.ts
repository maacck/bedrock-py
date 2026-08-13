/** @vitest-environment jsdom */
import { createElement, useEffect, type ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
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

type ChatApi = ReturnType<typeof useThreadChat>;

function HookHarness({ latestRef }: { latestRef: { current: ChatApi | null } }): ReactNode {
  const api = useThreadChat();
  useEffect(() => {
    latestRef.current = api;
  }, [api, latestRef]);
  return null;
}

async function mountHook(): Promise<{ latestRef: { current: ChatApi }; unmount: () => void }> {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root: Root = createRoot(host);
  const latestRef: { current: ChatApi | null } = { current: null };
  await act(async () => {
    root.render(createElement(HookHarness, { latestRef }));
  });
  if (!latestRef.current) throw new Error("hook did not mount");
  return {
    latestRef: latestRef as { current: ChatApi },
    unmount: () => {
      root.unmount();
      host.remove();
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("ignores a settled fetch after startNewThread clears the in-flight turn", async () => {
  // Given: a deferred /api/chat fetch that we settle after the user clears
  let resolveFetch!: (value: Response) => void;
  const fetchStarted = Promise.withResolvers<RequestInit | undefined>();
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    fetchStarted.resolve(init);
    return new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
  });
  vi.stubGlobal("fetch", fetchMock);

  const { latestRef, unmount } = await mountHook();

  // When: send starts a request, then startNewThread aborts it
  await act(async () => {
    void latestRef.current.send("hi");
  });
  const init = await fetchStarted.promise;
  expect(latestRef.current.status).toBe("streaming");
  expect(latestRef.current.messages).toHaveLength(1);

  await act(async () => {
    latestRef.current.startNewThread();
  });
  expect(init?.signal?.aborted).toBe(true);
  expect(latestRef.current.messages).toHaveLength(0);
  expect(latestRef.current.status).not.toBe("streaming");

  await act(async () => {
    resolveFetch(
      new Response("", {
        status: 200,
        headers: { "X-Thread-Id": "stale-thread" },
      }),
    );
    await Promise.resolve();
  });

  // Then: the stale response must not restore messages, streaming, or thread id
  expect(latestRef.current.messages).toHaveLength(0);
  expect(latestRef.current.status).not.toBe("streaming");
  expect(window.localStorage.getItem("bedrock-chat-thread-id")).toBeNull();
  unmount();
});
