import { describe, expect, it } from "vitest";
import { RATE_LIMIT, TokenBudgetWindow } from "./rate-limit-budget";

const LIMIT = RATE_LIMIT.TOKENS_PER_WINDOW;

function makeBudget(now: () => number = () => Date.now()) {
  let seq = 0;
  return new TokenBudgetWindow(now, () => `r-${++seq}`);
}

describe("TokenBudgetWindow", () => {
  it("allows reservations up to the window limit and denies beyond it", () => {
    const budget = makeBudget();
    const first = budget.reserve(30_000);
    expect(first.allowed).toBe(true);
    expect(first.remaining).toBe(LIMIT - 30_000);

    const second = budget.reserve(20_000);
    expect(second.allowed).toBe(true);
    expect(second.remaining).toBe(0);

    const third = budget.reserve(1);
    expect(third.allowed).toBe(false);
    expect(third.reservationId).toBeNull();
    expect(third.remaining).toBe(0);
  });

  it("admits at most the budgeted number of concurrent same-IP requests", async () => {
    const budget = makeBudget();
    const perRequest = 6_000;
    // 10 concurrent streaming requests from the same IP, like the route
    // firing reserve() before each model call.
    const results = await Promise.all(
      Array.from({ length: 10 }, async () => budget.reserve(perRequest)),
    );
    const allowed = results.filter((r) => r.allowed);
    expect(allowed).toHaveLength(Math.floor(LIMIT / perRequest)); // 8
    expect(results.filter((r) => !r.allowed)).toHaveLength(2);
    expect(budget.committed).toBe(8 * perRequest);
  });

  it("refunds the unused portion of a reservation on settle", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(10_000);
    budget.settle(reservationId!, 4_000);
    expect(budget.committed).toBe(4_000);
  });

  it("never drives the balance negative, even on settle overage or double settle", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(1_000);
    budget.settle(reservationId!, 5_000); // actual exceeded the reservation
    expect(budget.committed).toBe(5_000);
    budget.settle(reservationId!, 5_000); // duplicate settle is a no-op
    expect(budget.committed).toBe(5_000);
    budget.release("unknown-id");
    expect(budget.committed).toBe(5_000);
  });

  it("returns the full reservation on release", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(8_000);
    budget.release(reservationId!);
    expect(budget.committed).toBe(0);
  });

  it("charges the full reservation on forfeit (abort/error path)", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(8_000);
    budget.forfeit(reservationId!);
    expect(budget.committed).toBe(8_000);
  });

  it("handles interleaved reserve/settle/release without negative counts", () => {
    const budget = makeBudget();
    const ids: string[] = [];
    for (let i = 0; i < 20; i++) {
      const r = budget.reserve(4_000);
      if (r.allowed) ids.push(r.reservationId!);
      if (ids.length > 2) budget.settle(ids.shift()!, 1_000);
      expect(budget.committed).toBeGreaterThanOrEqual(0);
      expect(budget.committed).toBeLessThanOrEqual(LIMIT);
    }
    for (const id of ids) budget.release(id);
    expect(budget.committed).toBeGreaterThanOrEqual(0);
  });

  it("resets the budget when the window rolls over", () => {
    let now = 1_800_000_000_000;
    const budget = makeBudget(() => now);
    budget.reserve(LIMIT);
    expect(budget.reserve(1).allowed).toBe(false);
    now += RATE_LIMIT.WINDOW_SECONDS * 1000;
    expect(budget.reserve(1).allowed).toBe(true);
  });

  it("round-trips state through snapshot/restore", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(7_500);
    budget.settle(reservationId!, 2_500);
    budget.reserve(1_000);

    const restored = makeBudget();
    restored.restore(budget.snapshot());
    expect(restored.committed).toBe(budget.committed);
    expect(restored.resetAt).toBe(budget.resetAt);
  });

  it("fails closed on non-finite reserve amounts", () => {
    const budget = makeBudget();
    for (const bad of [NaN, Infinity, -Infinity]) {
      const result = budget.reserve(bad);
      expect(result.allowed).toBe(false);
      expect(result.reservationId).toBeNull();
    }
    expect(budget.committed).toBe(0);
    // The limiter still works afterwards.
    expect(budget.reserve(1_000).allowed).toBe(true);
  });

  it("charges the full reservation on non-finite settle amounts", () => {
    const budget = makeBudget();
    const { reservationId } = budget.reserve(8_000);
    budget.settle(reservationId!, NaN);
    expect(budget.committed).toBe(8_000);
  });

  it("charges usage to the new window when a reservation straddles the boundary", () => {
    let now = 3_599_000; // 1s before the hourly boundary
    const budget = new TokenBudgetWindow(() => now, () => "r1");
    budget.reserve(10_000);
    now = 3_601_000; // rolled into the next window
    budget.settle("r1", 9_000);
    expect(budget.committed).toBe(9_000);
  });
});
