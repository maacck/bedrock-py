import type { ThreadMessage } from "./thread-logic";

export interface ThreadStoreStub {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

export interface ThreadStoreNamespace {
  idFromName(name: string): unknown;
  get(id: unknown): ThreadStoreStub;
}

export type OpenThreadResult =
  | { ok: true; thread: { messages: ThreadMessage[] } }
  | { ok: false; status: 404 | 409 | 502 };

function getStub(ns: ThreadStoreNamespace, threadId: string): ThreadStoreStub {
  return ns.get(ns.idFromName(threadId));
}

async function call(
  stub: ThreadStoreStub,
  path: string,
  payload: Record<string, unknown>,
): Promise<Response> {
  return stub.fetch(`https://thread-store.internal${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function openThread(
  ns: ThreadStoreNamespace,
  threadId: string,
  deviceId: string,
  options?: { create?: boolean },
): Promise<OpenThreadResult> {
  try {
    const res = await call(getStub(ns, threadId), "/open", {
      deviceId,
      create: options?.create !== false,
    });
    if (res.status === 200) {
      const body = (await res.json()) as { thread: { messages: ThreadMessage[] } };
      return { ok: true, thread: body.thread };
    }
    if (res.status === 404 || res.status === 409) return { ok: false, status: res.status };
    return { ok: false, status: 502 };
  } catch (error) {
    // no-excuse-ok: catch — DO transport boundary; map any failure to 502.
    console.error("thread-store /open failed", error);
    return { ok: false, status: 502 };
  }
}

export async function appendThread(
  ns: ThreadStoreNamespace,
  threadId: string,
  deviceId: string,
  userMessage: string,
  assistantMessage: string,
): Promise<void> {
  try {
    await call(getStub(ns, threadId), "/append", { deviceId, userMessage, assistantMessage });
  } catch (error) {
    // no-excuse-ok: catch — history loss on the next turn is preferable to failing the stream budget.
    console.error("thread-store /append failed", error);
  }
}

export async function abortThread(
  ns: ThreadStoreNamespace,
  threadId: string,
  deviceId: string,
): Promise<void> {
  try {
    await call(getStub(ns, threadId), "/abort", { deviceId });
  } catch (error) {
    // no-excuse-ok: catch — lock release is best-effort; stale locks reset after STALE_LOCK_MS.
    console.error("thread-store /abort failed", error);
  }
}
