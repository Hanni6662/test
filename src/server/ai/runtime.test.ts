import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { PersistedReferenceImage } from "@/server/persistence/service";
import { AI_MAX_IMAGE_COUNT, AI_MAX_TOTAL_BYTES, configuredProvider, imagesAsDataUrls } from "./runtime";

function image(id: string, patch: Partial<PersistedReferenceImage> = {}): PersistedReferenceImage {
  return { id, projectId: "p", identifier: `legacy:${id}`, filename: `${id}.png`, storagePath: null,
    uploadBacked: false, mimeType: null, size: null, aiClassification: null, userClassification: null,
    confidence: null, status: "uploaded", effectiveClassification: "unknown", ...patch };
}

describe("AI image input budgeting", () => {
  const roots: string[] = [];
  afterEach(() => { delete process.env.PROMPT_BUILDER_UPLOAD_ROOT; delete process.env.OPENAI_COMPAT_API_KEY; delete process.env.OPENAI_COMPAT_ALLOWED_ORIGINS; roots.splice(0).forEach((root) => rmSync(root, { recursive: true, force: true })); });

  it("reads only upload-backed images and never treats a legacy identifier as a path", async () => {
    const root = mkdtempSync(join(tmpdir(), "ai-images-")); roots.push(root); process.env.PROMPT_BUILDER_UPLOAD_ROOT = root;
    mkdirSync(join(root, "p")); writeFileSync(join(root, "p", "one.png"), new Uint8Array([1, 2, 3]));
    const result = await imagesAsDataUrls("p", [image("legacy"), image("one", { storagePath: "p/one.png", uploadBacked: true, mimeType: "image/png", size: 3 })]);
    expect(result.map(({ id }) => id)).toEqual(["one"]);
  });

  it("rejects more than six upload-backed images before reading any file", async () => {
    let reads = 0;
    const images = Array.from({ length: AI_MAX_IMAGE_COUNT + 1 }, (_, index) => image(String(index), { storagePath: `p/${index}.png`, uploadBacked: true, mimeType: "image/png", size: 1 }));
    await expect(imagesAsDataUrls("p", images, { statFile: async () => ({ size: 1 }), readFile: async () => { reads += 1; return Buffer.from([1]); } }))
      .rejects.toMatchObject({ code: "AI_INPUT_TOO_LARGE" });
    expect(reads).toBe(0);
  });

  it("rejects actual total bytes above 24 MB and reads allowed files sequentially", async () => {
    const five = Array.from({ length: 5 }, (_, index) => image(String(index), { storagePath: `p/${index}.png`, uploadBacked: true, mimeType: "image/png", size: 1 }));
    let reads = 0;
    await expect(imagesAsDataUrls("p", five, { statFile: async () => ({ size: AI_MAX_TOTAL_BYTES / 5 + 1 }), readFile: async () => { reads += 1; return Buffer.from([1]); } }))
      .rejects.toMatchObject({ code: "AI_INPUT_TOO_LARGE" });
    expect(reads).toBe(0);

    let active = 0; let peak = 0;
    await imagesAsDataUrls("p", five.slice(0, 2), { statFile: async () => ({ size: 1 }), readFile: async () => {
      active += 1; peak = Math.max(peak, active); await new Promise((resolve) => setTimeout(resolve, 2)); active -= 1; return Buffer.from([1]);
    } });
    expect(peak).toBe(1);

    await expect(imagesAsDataUrls("p", five.slice(0, 2), { statFile: async () => ({ size: 1 }), readFile: async () => Buffer.alloc(AI_MAX_TOTAL_BYTES / 2 + 1) }))
      .rejects.toMatchObject({ code: "AI_INPUT_TOO_LARGE" });
  });

  it("maps malformed allowed-origin configuration to an explicit config error", () => {
    process.env.OPENAI_COMPAT_API_KEY = "secret"; process.env.OPENAI_COMPAT_ALLOWED_ORIGINS = "not a valid origin";
    expect(() => configuredProvider({ baseUrl: "https://api.openai.com/v1", visionModel: "vision", textModel: "text" }))
      .toThrowError(expect.objectContaining({ code: "AI_CONFIG_ERROR", status: 500 }));
  });
});
