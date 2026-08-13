/** @vitest-environment jsdom */
import { createElement, useEffect, type ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { useThreadChat } from "@/lib/use-thread-chat";

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
  let resolveFetchStarted!: (init: RequestInit | undefined) => void;
  const fetchStarted = new Promise<RequestInit | undefined>((resolve) => {
    resolveFetchStarted = resolve;
  });
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    resolveFetchStarted(init);
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
  const init = await fetchStarted;
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
