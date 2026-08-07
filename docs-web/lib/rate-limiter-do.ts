import { DurableObject } from "cloudflare:workers";
import {
  TokenBudgetWindow,
  type BudgetSnapshot,
} from "./rate-limit-budget";

const STORAGE_KEY = "budget";

/**
 * Per-client-IP token budget counter. One instance per IP via
 * `idFromName(ip)`; the single-threaded Durable Object runtime plus the
 * synchronous budget core make every reserve/settle/release atomic.
 */
export class RateLimiter extends DurableObject<CloudflareEnv> {
  private readonly budget = new TokenBudgetWindow();

  constructor(ctx: DurableObjectState, env: CloudflareEnv) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      const snapshot = await ctx.storage.get<BudgetSnapshot>(STORAGE_KEY);
      if (snapshot) this.budget.restore(snapshot);
    });
  }

  private persist(): void {
    this.ctx.waitUntil(this.ctx.storage.put(STORAGE_KEY, this.budget.snapshot()));
  }

  override async fetch(req: Request): Promise<Response> {
    const path = new URL(req.url).pathname;
    let payload: Record<string, unknown>;
    try {
      payload = (await req.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "invalid_json" }, { status: 400 });
    }

    switch (path) {
      case "/reserve": {
        const result = this.budget.reserve(Number(payload.tokens ?? 0));
        this.persist();
        return Response.json(result);
      }
      case "/settle": {
        this.budget.settle(
          String(payload.reservationId ?? ""),
          Number(payload.actualTokens ?? 0),
        );
        this.persist();
        return Response.json({ ok: true });
      }
      case "/forfeit": {
        this.budget.forfeit(String(payload.reservationId ?? ""));
        this.persist();
        return Response.json({ ok: true });
      }
      case "/release": {
        this.budget.release(String(payload.reservationId ?? ""));
        this.persist();
        return Response.json({ ok: true });
      }
      default:
        return Response.json({ error: "not_found" }, { status: 404 });
    }
  }
}
