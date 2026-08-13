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
