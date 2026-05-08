const TOKENS_PER_WINDOW = 100_000;
const WINDOW_SECONDS = 3600;

interface RateLimitResult {
  allowed: boolean;
  remaining: number;
  resetAt: number;
  current: number;
}

interface WindowData {
  tokens: number;
  resetAt: number;
}

function getWindowKey(ip: string): string {
  const now = Math.floor(Date.now() / 1000);
  const windowStart = now - (now % WINDOW_SECONDS);
  return `rl:${ip}:${windowStart}`;
}

export async function checkRateLimit(
  kv: KVNamespace,
  ip: string,
): Promise<RateLimitResult> {
  const key = getWindowKey(ip);
  const resetAt =
    Math.floor(Date.now() / 1000 / WINDOW_SECONDS) * WINDOW_SECONDS +
    WINDOW_SECONDS;

  const raw = await kv.get<WindowData>(key, { type: "json" });
  const current = raw?.tokens ?? 0;

  if (current >= TOKENS_PER_WINDOW) {
    return { allowed: false, remaining: 0, resetAt, current };
  }

  return {
    allowed: true,
    remaining: TOKENS_PER_WINDOW - current,
    resetAt,
    current,
  };
}

export async function trackTokenUsage(
  kv: KVNamespace,
  ip: string,
  inputTokens: number,
  outputTokens: number,
): Promise<void> {
  const key = getWindowKey(ip);
  const ttl = WINDOW_SECONDS * 2;

  const raw: WindowData | null = await kv.get(key, { type: "json" });
  const current = raw?.tokens ?? 0;
  const total = inputTokens + outputTokens;

  await kv.put(
    key,
    JSON.stringify({ tokens: current + total, resetAt: raw?.resetAt ?? 0 }),
    {
      expirationTtl: ttl,
    },
  );
}

export function getClientIp(req: Request): string {
  return (
    req.headers.get("cf-connecting-ip") ??
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    "unknown"
  );
}
