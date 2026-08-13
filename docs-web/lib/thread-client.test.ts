import { expect, it } from "vitest";
import { openThread, appendThread, abortThread, type ThreadStoreNamespace } from "@/lib/thread-client";

function fakeNamespace(handler: (path: string, body: unknown) => Promise<Response>): ThreadStoreNamespace {
  return {
    idFromName: (name) => name,
    get: (id) => ({
      fetch: async (input: string, init?: RequestInit) => {
        const path = new URL(input).pathname;
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        return handler(path, body);
      },
    }),
  };
}

it("maps 200 /open to ok with messages", async () => {
  const ns = fakeNamespace(async () =>
    Response.json({ thread: { messages: [{ role: "user", content: "hi" }] } }),
  );
  const r = await openThread(ns, "t1", "d1");
  expect(r).toEqual({ ok: true, thread: { messages: [{ role: "user", content: "hi" }] } });
});

it("maps 404 and 409 /open to not-found and busy", async () => {
  const ns404 = fakeNamespace(async () => Response.json({ error: "thread_not_found" }, { status: 404 }));
  expect(await openThread(ns404, "t1", "d1")).toEqual({ ok: false, status: 404 });

  const ns409 = fakeNamespace(async () => Response.json({ error: "thread_busy" }, { status: 409 }));
  expect(await openThread(ns409, "t1", "d1")).toEqual({ ok: false, status: 409 });
});

it("maps DO failures to 502", async () => {
  const ns = fakeNamespace(async () => Response.json({}, { status: 500 }));
  expect(await openThread(ns, "t1", "d1")).toEqual({ ok: false, status: 502 });
});

it("appendThread and abortThread do not throw on failure", async () => {
  const ns = fakeNamespace(async () => Response.json({}, { status: 500 }));
  await expect(appendThread(ns, "t1", "d1", "u", "a")).resolves.toBeUndefined();
  await expect(abortThread(ns, "t1", "d1")).resolves.toBeUndefined();
});
