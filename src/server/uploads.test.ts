import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { UploadError, deleteUploadedFile, storeUploadedFiles } from "./uploads";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);

describe("secure image storage", () => {
  const roots: string[] = [];
  afterEach(() => roots.splice(0).forEach((root) => rmSync(root, { recursive: true, force: true })));

  it("stores a valid image under a random server filename and preserves original name as metadata", async () => {
    const root = mkdtempSync(join(tmpdir(), "uploads-")); roots.push(root);
    const [stored] = await storeUploadedFiles(root, "project-1", [new File([PNG], "../../shoe.png", { type: "image/png" })]);
    expect(stored.originalFilename).toBe("shoe.png");
    expect(stored.storagePath).toMatch(/^project-1\/[0-9a-f-]+\.png$/);
    expect([...readFileSync(join(root, stored.storagePath))]).toEqual([...PNG]);
  });

  it("rejects spoofed MIME data and excessive batch/size", async () => {
    const root = mkdtempSync(join(tmpdir(), "uploads-")); roots.push(root);
    await expect(storeUploadedFiles(root, "p", [new File(["not png"], "x.png", { type: "image/png" })]))
      .rejects.toMatchObject({ code: "IMAGE_TYPE_MISMATCH" });
    const files = Array.from({ length: 21 }, (_, i) => new File([PNG], `${i}.png`, { type: "image/png" }));
    await expect(storeUploadedFiles(root, "p", files)).rejects.toMatchObject({ code: "TOO_MANY_IMAGES" });
    const tooLarge = { name: "big.png", type: "image/png", size: 10 * 1024 * 1024 + 1 } as File;
    await expect(storeUploadedFiles(root, "p", [tooLarge])).rejects.toMatchObject({ code: "IMAGE_TOO_LARGE" });
  });

  it("refuses deletion outside the selected project upload root", async () => {
    const root = mkdtempSync(join(tmpdir(), "uploads-")); roots.push(root);
    await expect(deleteUploadedFile(root, "project-1", "../other/secret.png"))
      .rejects.toBeInstanceOf(UploadError);
  });
});
