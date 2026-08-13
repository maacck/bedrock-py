import { describe, expect, it } from 'vitest';
import { sanitizeUrl } from './safe-url';

describe('sanitizeUrl', () => {
  describe('rejects dangerous schemes', () => {
    const dangerous = [
      'javascript:alert(1)',
      'JAVASCRIPT:alert(1)',
      'JavaScript:alert(document.domain)',
      'vbscript:msgbox(1)',
      'data:text/html,<script>alert(1)</script>',
      'data:image/png;base64,AAAA',
      'file:///etc/passwd',
      'ftp://example.com/x',
      'blob:https://example.com/uuid',
    ];

    for (const url of dangerous) {
      it(`blocks ${url.slice(0, 40)} for links`, () => {
        expect(sanitizeUrl(url, 'link')).toBeUndefined();
      });
      it(`blocks ${url.slice(0, 40)} for images`, () => {
        expect(sanitizeUrl(url, 'image')).toBeUndefined();
      });
    }
  });

  describe('rejects whitespace/control-character obfuscation', () => {
    const obfuscated = [
      ' javascript:alert(1)',
      'javascript:alert(1) ',
      '  https://example.com',
      'java\tscript:alert(1)',
      'java\nscript:alert(1)',
      'java\rscript:alert(1)',
      'jav\fascript:alert(1)',
      'https://example.com/a b',
      '\t#fragment',
    ];

    for (const url of obfuscated) {
      it(`blocks ${JSON.stringify(url)}`, () => {
        expect(sanitizeUrl(url, 'link')).toBeUndefined();
        expect(sanitizeUrl(url, 'image')).toBeUndefined();
      });
    }
  });

  describe('rejects malformed and non-string input', () => {
    it.each([undefined, null, 123, {}, [], ''])('blocks %s', (value) => {
      expect(sanitizeUrl(value, 'link')).toBeUndefined();
      expect(sanitizeUrl(value, 'image')).toBeUndefined();
    });

    it('blocks unparseable absolute URLs', () => {
      expect(sanitizeUrl('https://', 'link')).toBeUndefined();
    });
  });

  describe('allows safe link URLs', () => {
    const safe = [
      'https://example.com/path?q=1#frag',
      'http://example.com',
      'mailto:user@example.com',
      'mailto:user@example.com?subject=Hi%20there',
      '/docs/getting-started',
      './relative/page',
      '../up/one',
      'page.md',
      '#section-anchor',
      '?query=1',
    ];

    for (const url of safe) {
      it(`allows ${url}`, () => {
        expect(sanitizeUrl(url, 'link')).toBe(url);
      });
    }
  });

  describe('allows safe image URLs', () => {
    const safe = [
      'https://cdn.example.com/x.png',
      'http://cdn.example.com/x.png',
      '/static/x.png',
      './images/x.png',
      'images/x.png',
    ];

    for (const url of safe) {
      it(`allows ${url}`, () => {
        expect(sanitizeUrl(url, 'image')).toBe(url);
      });
    }

    it('blocks mailto: for images', () => {
      expect(sanitizeUrl('mailto:user@example.com', 'image')).toBeUndefined();
    });
  });

  it("rejects protocol-relative links", () => {
    expect(sanitizeUrl("//evil.com/phish", "link")).toBeUndefined();
  });

  it("rejects protocol-relative images", () => {
    expect(sanitizeUrl("//evil.com/beacon.png", "image")).toBeUndefined();
  });

  it("still allows same-origin relative links", () => {
    expect(sanitizeUrl("/docs/cache", "link")).toBe("/docs/cache");
    expect(sanitizeUrl("./guide", "link")).toBe("./guide");
    expect(sanitizeUrl("#section", "link")).toBe("#section");
  });

  it("rejects protocol-relative CDN URLs previously treated as relative", () => {
    expect(sanitizeUrl("//cdn.example.com/lib.js", "link")).toBeUndefined();
  });
});
