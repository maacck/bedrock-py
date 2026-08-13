import { DurableObject } from "cloudflare:workers";
import {
  abortThreadState,
  appendThreadState,
  openThreadState,
  type ThreadSnapshot,
} from "./thread-logic";

const STORAGE_KEY = "thread";

/**
 * Per-thread conversation state. One instance per thread via `idFromName(threadId)`.
 * All decision logic lives in the pure `thread-logic.ts` state functions; this
 * class only loads/saves the snapshot and maps results to HTTP responses.
 */
export class ThreadStore extends DurableObject<CloudflareEnv> {
  private async load(): Promise<ThreadSnapshot | null> {
    return (await this.ctx.storage.get<ThreadSnapshot>(STORAGE_KEY)) ?? null;
  }

  override async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    let payload: Record<string, unknown>;
    try {
      payload = (await req.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }
    const deviceId = String(payload.deviceId ?? "");
    const thread = await this.load();

    switch (url.pathname) {
      case "/open": {
        const result = openThreadState(thread, deviceId, Date.now(), {
          create: payload.create !== false,
        });
        if (result.status === 404) return Response.json({ error: "thread_not_found" }, { status: 404 });
        if (result.status === 409) return Response.json({ error: "thread_busy" }, { status: 409 });
        await this.ctx.storage.put(STORAGE_KEY, result.thread);
        return Response.json({ ok: true, thread: { messages: result.thread.messages } });
      }
      case "/append": {
        const result = appendThreadState(
          thread,
          deviceId,
          String(payload.userMessage ?? ""),
          String(payload.assistantMessage ?? ""),
          Date.now(),
        );
        if (result.status === 404) return Response.json({ error: "thread_not_found" }, { status: 404 });
        await this.ctx.storage.put(STORAGE_KEY, result.thread);
        return Response.json({ ok: true });
      }
      case "/abort": {
        const result = abortThreadState(thread, deviceId, Date.now());
        if (result.status === 404) return Response.json({ error: "thread_not_found" }, { status: 404 });
        await this.ctx.storage.put(STORAGE_KEY, result.thread);
        return Response.json({ ok: true });
      }
      default:
        return Response.json({ error: "not_found" }, { status: 404 });
    }
  }
}
