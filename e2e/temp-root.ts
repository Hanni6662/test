import { mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

const prefix = "prompt-builder-e2e-";
const marker = ".codex-e2e-owner";

export function isOwnedE2eRoot(root: string | undefined, token: string | undefined): root is string {
  if (!root || !token) return false;
  try {
    const canonicalRoot = realpathSync(root);
    const canonicalTemp = realpathSync(tmpdir());
    return resolve(dirname(canonicalRoot)) === resolve(canonicalTemp)
      && basename(canonicalRoot).startsWith(prefix)
      && readFileSync(join(canonicalRoot, marker), "utf8") === token;
  } catch {
    return false;
  }
}

export function createOwnedE2eRoot(token: string): string {
  const root = mkdtempSync(join(tmpdir(), prefix));
  writeFileSync(join(root, marker), token, { encoding: "utf8", flag: "wx" });
  return root;
}

export function removeOwnedE2eRoot(root: string | undefined, token: string | undefined): boolean {
  if (!isOwnedE2eRoot(root, token)) return false;
  rmSync(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  return true;
}
