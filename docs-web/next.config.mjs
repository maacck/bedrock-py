import { readFileSync } from "node:fs";
import { createMDX } from "fumadocs-mdx/next";

const withMDX = createMDX();

// The docs describe the bedrock-core version in this checkout; show it in the nav.
const bedrockVersion = readFileSync(new URL("../packages/bedrock/pyproject.toml", import.meta.url), "utf8")
  .match(/^version = "(.+)"$/m)[1];

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  env: { BEDROCK_VERSION: bedrockVersion },
  transpilePackages: ["shiki"],
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default withMDX(config);
