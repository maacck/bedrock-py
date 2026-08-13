const LINK_PROTOCOLS = new Set(['https:', 'http:', 'mailto:']);
const IMAGE_PROTOCOLS = new Set(['https:', 'http:']);

const SCHEME_PATTERN = /^[A-Za-z][A-Za-z0-9+.-]*:/;
// Any ASCII whitespace or C0/DEL control character makes the URL unsafe:
// they can smuggle schemes past naive checks (e.g. "java\tscript:").
const UNSAFE_CHARS_PATTERN = /[\u0000-\u0020\u007F]/;

export type UrlKind = 'link' | 'image';

/**
 * Validates a URL coming from untrusted Markdown (e.g. AI-generated answers).
 *
 * Returns the URL unchanged when it is safe to render, otherwise `undefined`.
 * Links allow `https:`, `http:`, `mailto:`, relative paths and fragments;
 * images allow `https:`, `http:` and relative paths only.
 */
export function sanitizeUrl(value: unknown, kind: UrlKind): string | undefined {
  if (typeof value !== 'string' || value.length === 0) return undefined;
  if (UNSAFE_CHARS_PATTERN.test(value)) return undefined;

  if (!SCHEME_PATTERN.test(value)) {
    // Protocol-relative URLs ("//host/path") resolve against the current scheme
    // to an EXTERNAL host — not same-origin. Treat them as unsafe.
    if (value.startsWith("//")) return undefined;
    // Relative path, query, or fragment — resolved against the current origin.
    return value;
  }

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return undefined;
  }

  const allowed = kind === 'link' ? LINK_PROTOCOLS : IMAGE_PROTOCOLS;
  return allowed.has(parsed.protocol) ? value : undefined;
}
