import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createOwnedE2eRoot, isOwnedE2eRoot, removeOwnedE2eRoot } from "../../e2e/temp-root";

describe("E2E temporary root guard", () => {
  it("removes only a marked, token-matched directory directly under the system temp directory", () => {
    const owned = createOwnedE2eRoot("test-token");
    expect(isOwnedE2eRoot(owned, "test-token")).toBe(true);
    expect(removeOwnedE2eRoot(owned, "wrong-token")).toBe(false);
    expect(removeOwnedE2eRoot(owned, "test-token")).toBe(true);
  });

  it("rejects an arbitrary similarly named directory without the ownership marker", () => {
    const arbitrary = mkdtempSync(join(tmpdir(), "prompt-builder-e2e-"));
    writeFileSync(join(arbitrary, "keep.txt"), "user data");
    expect(removeOwnedE2eRoot(arbitrary, "test-token")).toBe(false);
    rmSync(arbitrary, { recursive: true, force: true });
  });
});
