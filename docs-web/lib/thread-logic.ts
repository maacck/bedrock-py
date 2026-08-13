/** A single stored conversation turn (plain text only — no client-supplied parts). */
export interface ThreadMessage {
  role: "user" | "assistant";
  content: string;
}

/** Rough token estimate: ~4 characters per token for English/code text. */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

export const HISTORY_MAX_TOKENS = 8_192;
export const HISTORY_MAX_MESSAGES = 20;

/**
 * Drop the oldest messages until the thread fits the history bounds.
 * The token bound is best-effort: trimming stops at the two-message floor,
 * so a thread whose last two messages alone exceed HISTORY_MAX_TOKENS stays
 * over budget rather than losing the current turn (floor wins).
 */
export function truncateMessages(messages: ThreadMessage[]): ThreadMessage[] {
  const result = [...messages];
  while (result.length > HISTORY_MAX_MESSAGES) result.shift();
  while (
    result.length > 2 &&
    result.reduce((sum, m) => sum + estimateTokens(m.content), 0) > HISTORY_MAX_TOKENS
  ) {
    result.shift();
  }
  return result;
}

export interface ThreadSnapshot {
  deviceId: string;
  messages: ThreadMessage[];
  inFlight: boolean;
  createdAt: number;
  updatedAt: number;
}

/** In-flight locks older than this are considered stale and reset on /open. */
export const STALE_LOCK_MS = 10 * 60_000;

export type OpenThreadResult =
  | { status: 200; thread: ThreadSnapshot }
  | { status: 404 }
  | { status: 409 };

/**
 * /open transition: create-or-get a thread, verify device ownership (404 on
 * mismatch), and take the in-flight lock (409 while busy; stale locks reset).
 * `deviceId` is an ownership key only — NOT an authentication boundary.
 */
export function openThreadState(
  thread: ThreadSnapshot | null,
  deviceId: string,
  now: number,
): OpenThreadResult {
  if (!thread) {
    return {
      status: 200,
      thread: { deviceId, messages: [], inFlight: true, createdAt: now, updatedAt: now },
    };
  }
  if (thread.deviceId !== deviceId) return { status: 404 };
  if (thread.inFlight) {
    if (now - thread.updatedAt <= STALE_LOCK_MS) return { status: 409 };
    thread.inFlight = false; // stale lock from a vanished stream
  }
  return { status: 200, thread: { ...thread, inFlight: true, updatedAt: now } };
}

export type AppendThreadResult = { status: 200; thread: ThreadSnapshot } | { status: 404 };

/** /append transition: store the turn pair, truncate, and release the lock. */
export function appendThreadState(
  thread: ThreadSnapshot | null,
  deviceId: string,
  userMessage: string,
  assistantMessage: string,
  now: number,
): AppendThreadResult {
  if (!thread || thread.deviceId !== deviceId) return { status: 404 };
  const messages = truncateMessages([
    ...thread.messages,
    { role: "user", content: userMessage },
    { role: "assistant", content: assistantMessage },
  ]);
  return { status: 200, thread: { ...thread, messages, inFlight: false, updatedAt: now } };
}

export type AbortThreadResult = { status: 200; thread: ThreadSnapshot } | { status: 404 };

/** /abort transition: release the in-flight lock without appending. */
export function abortThreadState(
  thread: ThreadSnapshot | null,
  deviceId: string,
  now: number,
): AbortThreadResult {
  if (!thread || thread.deviceId !== deviceId) return { status: 404 };
  return { status: 200, thread: { ...thread, inFlight: false, updatedAt: now } };
}
