/**
 * Atomic per-client token budget for the docs chat API.
 *
 * All mutating methods are synchronous. Combined with the Durable Object
 * single-threaded execution model (one instance per client IP), every
 * reserve/settle/release operation is atomic: no interleaving can occur
 * between reading and updating the budget, which eliminates the TOCTOU
 * races of the previous Workers KV read-modify-write implementation.
 */

export const RATE_LIMIT = {
  /** Single source of truth for the per-IP hourly token budget. */
  TOKENS_PER_WINDOW: 50_000,
  WINDOW_SECONDS: 3_600,
  MAX_BODY_BYTES: 16 * 1_024,
  /** Maximum characters accepted for a single user query. */
  MAX_QUERY_CHARS: 2_000,
  /** Maximum estimated user input tokens accepted per request. */
  MAX_INPUT_TOKENS: 16_000,
  /** Output budget reserved per request and refunded on settlement. */
  RESERVED_OUTPUT_TOKENS: 4_096,
  /** Server-side conversation history bounds (kept in the thread store). */
  HISTORY_MAX_TOKENS: 8_192,
  HISTORY_MAX_MESSAGES: 20,
} as const;

export interface ReserveResult {
  allowed: boolean;
  reservationId: string | null;
  /** Tokens already committed (settled + outstanding) in this window. */
  used: number;
  remaining: number;
  /** Unix seconds at which the current window resets. */
  resetAt: number;
}

export interface BudgetSnapshot {
  windowStart: number;
  settled: number;
  outstanding: Record<string, number>;
}

export class TokenBudgetWindow {
  private windowStart: number;
  private settled = 0;
  private readonly outstanding = new Map<string, number>();

  constructor(
    private readonly now: () => number = () => Date.now(),
    private readonly createId: () => string = () => crypto.randomUUID(),
  ) {
    this.windowStart = this.currentWindowStart();
  }

  private currentWindowStart(): number {
    const seconds = Math.floor(this.now() / 1000);
    return seconds - (seconds % RATE_LIMIT.WINDOW_SECONDS);
  }

  private rollWindow(): void {
    const start = this.currentWindowStart();
    if (start !== this.windowStart) {
      this.windowStart = start;
      this.settled = 0;
      // Reservations from an expired window are dropped; their budget is
      // returned by the reset.
      this.outstanding.clear();
    }
  }

  /** Committed tokens (settled usage + outstanding reservations). */
  get committed(): number {
    let total = this.settled;
    for (const amount of this.outstanding.values()) total += amount;
    return total;
  }

  get resetAt(): number {
    return this.windowStart + RATE_LIMIT.WINDOW_SECONDS;
  }

  reserve(amount: number): ReserveResult {
    this.rollWindow();
    const used = this.committed;
    // Fail closed on non-finite input: a NaN/Infinity reservation would
    // otherwise poison every comparison and disable the limit.
    if (!Number.isFinite(amount)) {
      return {
        allowed: false,
        reservationId: null,
        used,
        remaining: Math.max(0, RATE_LIMIT.TOKENS_PER_WINDOW - used),
        resetAt: this.resetAt,
      };
    }
    const tokens = Math.max(0, Math.ceil(amount));
    if (used + tokens > RATE_LIMIT.TOKENS_PER_WINDOW) {
      return {
        allowed: false,
        reservationId: null,
        used,
        remaining: Math.max(0, RATE_LIMIT.TOKENS_PER_WINDOW - used),
        resetAt: this.resetAt,
      };
    }
    const reservationId = this.createId();
    this.outstanding.set(reservationId, tokens);
    const committed = used + tokens;
    return {
      allowed: true,
      reservationId,
      used: committed,
      remaining: Math.max(0, RATE_LIMIT.TOKENS_PER_WINDOW - committed),
      resetAt: this.resetAt,
    };
  }

  /**
   * Convert a reservation into final usage: refund the unused portion of
   * the reservation and charge the actual consumption. An unknown
   * reservation id (already settled/released) is a no-op so the balance
   * can never be refunded twice or driven negative.
   */
  settle(reservationId: string, actualTokens: number): void {
    const reserved = this.outstanding.get(reservationId);
    this.rollWindow(); // may clear outstanding, incl. this reservation (window rollover)
    if (reserved === undefined) return; // unknown id → no-op (never refund twice)
    this.outstanding.delete(reservationId);
    // Fail conservative on non-finite input: charge the full reservation
    // rather than letting NaN poison the settled balance.
    this.settled += Number.isFinite(actualTokens)
      ? Math.max(0, Math.ceil(actualTokens))
      : reserved;
  }

  /** Charge the full reservation (used for aborted/errored streams). */
  forfeit(reservationId: string): void {
    this.settle(reservationId, this.outstanding.get(reservationId) ?? 0);
  }

  /** Return a reservation in full (e.g. the model was never called). */
  release(reservationId: string): void {
    this.rollWindow();
    this.outstanding.delete(reservationId);
  }

  snapshot(): BudgetSnapshot {
    return {
      windowStart: this.windowStart,
      settled: this.settled,
      outstanding: Object.fromEntries(this.outstanding),
    };
  }

  restore(snapshot: BudgetSnapshot): void {
    this.windowStart = snapshot.windowStart;
    this.settled = Math.max(0, snapshot.settled);
    this.outstanding.clear();
    for (const [id, amount] of Object.entries(snapshot.outstanding)) {
      this.outstanding.set(id, Math.max(0, amount));
    }
    this.rollWindow();
  }
}
