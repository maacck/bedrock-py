import { copyFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Turbopack rejects loader inputs that live outside the Next.js project root, so the
// repository CHANGELOG.md is mirrored into docs-web before the changelog page is compiled.
const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.join(appRoot, "..", "CHANGELOG.md");
const target = path.join(appRoot, ".generated", "CHANGELOG.md");

await mkdir(path.dirname(target), { recursive: true });
await copyFile(source, target);
