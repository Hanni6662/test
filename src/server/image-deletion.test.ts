import { existsSync, mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createPromptBuilderService } from "./persistence/service";
import { deleteReferenceImageConsistently } from "./image-deletion";

describe("quarantined image deletion", () => {
  const roots: string[] = [];
  afterEach(() => roots.splice(0).forEach((root) => rmSync(root, { recursive: true, force: true })));

  it("leaves a cleanup-only quarantine orphan, not a live DB row, when final unlink fails", async () => {
    const root = mkdtempSync(join(tmpdir(), "delete-image-")); roots.push(root); const service = createPromptBuilderService(join(root, "app.db"));
    const project = service.createProject({ name: "Cleanup pending" }); mkdirSync(join(root, "uploads", project.id), { recursive: true });
    writeFileSync(join(root, "uploads", project.id, "one.png"), new Uint8Array([1]));
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 1 }]);
    const result = await deleteReferenceImageConsistently(service, image.id, join(root, "uploads"), { unlinkFile: async () => { throw new Error("locked"); } });
    expect(result).toEqual({ deleted: true, cleanupPending: true });
    expect(service.listReferenceImages(project.id)).toEqual([]);
    expect(existsSync(join(root, "uploads", project.id, "one.png"))).toBe(false);
    expect(existsSync(join(root, "uploads", project.id, ".quarantine"))).toBe(true);
    service.close();
  });

  it("reports pending cleanup without overturning a successful file and DB deletion", async () => {
    const root = mkdtempSync(join(tmpdir(), "delete-image-")); roots.push(root); const service = createPromptBuilderService(join(root, "app.db"));
    const project = service.createProject({ name: "Cleanup directory" }); mkdirSync(join(root, "uploads", project.id), { recursive: true });
    writeFileSync(join(root, "uploads", project.id, "one.png"), new Uint8Array([1]));
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 1 }]);
    const result = await deleteReferenceImageConsistently(service, image.id, join(root, "uploads"), { removeDirectory: async () => { throw new Error("locked directory"); } });
    expect(result).toEqual({ deleted: true, cleanupPending: true });
    expect(service.listReferenceImages(project.id)).toEqual([]);
    service.close();
  });

  it("preserves the original DB failure when only post-recovery directory cleanup fails", async () => {
    const root = mkdtempSync(join(tmpdir(), "delete-image-")); roots.push(root); const actual = createPromptBuilderService(join(root, "app.db"));
    const project = actual.createProject({ name: "Recovery cleanup" }); mkdirSync(join(root, "uploads", project.id), { recursive: true });
    const original = join(root, "uploads", project.id, "one.png"); writeFileSync(original, new Uint8Array([1]));
    const [image] = actual.addReferenceImages(project.id, [{ storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 1 }]);
    const databaseError = new Error("database locked");
    await expect(deleteReferenceImageConsistently({ getReferenceImage: (id) => actual.getReferenceImage(id), deleteReferenceImage: () => { throw databaseError; } }, image.id, join(root, "uploads"), { removeDirectory: async () => { throw new Error("locked directory"); } })).rejects.toBe(databaseError);
    expect(existsSync(original)).toBe(true);
    actual.close();
  });
});
