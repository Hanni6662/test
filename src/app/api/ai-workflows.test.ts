import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BetterSqlite3 from "better-sqlite3";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1]);

describe("image and AI workflow routes", () => {
  let directory: string;
  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "workflow-api-"));
    process.env.PROMPT_BUILDER_DB_PATH = join(directory, "app.db");
    process.env.PROMPT_BUILDER_UPLOAD_ROOT = join(directory, "uploads");
    delete process.env.OPENAI_COMPAT_API_KEY;
    delete process.env.OPENAI_COMPAT_ALLOWED_ORIGINS;
    vi.resetModules();
  });
  afterEach(async () => {
    (await import("@/server/persistence/runtime")).closeRuntimeService();
    delete process.env.PROMPT_BUILDER_DB_PATH; delete process.env.PROMPT_BUILDER_UPLOAD_ROOT; delete process.env.OPENAI_COMPAT_API_KEY; delete process.env.OPENAI_COMPAT_ALLOWED_ORIGINS;
    rmSync(directory, { recursive: true, force: true }); vi.restoreAllMocks();
  });

  it("uploads and lists valid multipart images while rejecting spoofed MIME", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    const project = service.createProject({ name: "Images" });
    const route = await import("./projects/[id]/images/route");
    const form = new FormData(); form.append("images", new File([PNG], "front.png", { type: "image/png" }));
    const uploaded = await route.POST(new Request("http://x/images", { method: "POST", body: form }), { params: Promise.resolve({ id: project.id }) });
    expect(uploaded.status).toBe(201);
    expect((await route.GET(new Request("http://x/images"), { params: Promise.resolve({ id: project.id }) })).status).toBe(200);
    const bad = new FormData(); bad.append("images", new File(["fake"], "fake.png", { type: "image/png" }));
    const rejected = await route.POST(new Request("http://x/images", { method: "POST", body: bad }), { params: Promise.resolve({ id: project.id }) });
    expect(rejected.status).toBe(415); expect((await rejected.json()).error.code).toBe("IMAGE_TYPE_MISMATCH");
    expect(existsSync(join(directory, "uploads", project.id))).toBe(true);
  });

  it("cleans streamed files when the metadata transaction fails", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    const project = service.createProject({ name: "DB failure" });
    const injector = new BetterSqlite3(process.env.PROMPT_BUILDER_DB_PATH!);
    injector.exec("CREATE TRIGGER fail_uploaded_metadata BEFORE INSERT ON reference_images BEGIN SELECT RAISE(ABORT, 'metadata failure'); END"); injector.close();
    const route = await import("./projects/[id]/images/route");
    const form = new FormData(); form.append("images", new File([PNG], "front.png", { type: "image/png" }));
    const response = await route.POST(new Request("http://x/images", { method: "POST", body: form }), { params: Promise.resolve({ id: project.id }) });
    expect(response.status).toBe(500);
    const projectRoot = join(process.env.PROMPT_BUILDER_UPLOAD_ROOT!, project.id);
    expect(existsSync(projectRoot)).toBe(false);
    expect(service.listReferenceImages(project.id)).toEqual([]);
  });

  it("patches user classification and deletes both metadata and the bounded file", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService(); const project = service.createProject({ name: "Lifecycle" });
    const uploads = await import("./projects/[id]/images/route"); const form = new FormData(); form.append("images", new File([PNG], "front.png", { type: "image/png" }));
    const created = await (await uploads.POST(new Request("http://x/images", { method: "POST", body: form }), { params: Promise.resolve({ id: project.id }) })).json();
    const imageRoute = await import("./images/[id]/route");
    const patched = await imageRoute.PATCH(new Request("http://x/image", { method: "PATCH", body: JSON.stringify({ userClassification: "detail" }) }), { params: Promise.resolve({ id: created[0].id }) });
    expect(await patched.json()).toMatchObject({ effectiveClassification: "detail", status: "reviewed" });
    const target = join(process.env.PROMPT_BUILDER_UPLOAD_ROOT!, created[0].storagePath); expect(existsSync(target)).toBe(true);
    expect((await imageRoute.DELETE(new Request("http://x/image", { method: "DELETE" }), { params: Promise.resolve({ id: created[0].id }) })).status).toBe(200);
    expect(existsSync(target)).toBe(false); expect(existsSync(join(process.env.PROMPT_BUILDER_UPLOAD_ROOT!, project.id))).toBe(false); expect(service.listReferenceImages(project.id)).toEqual([]);
  });

  it("deletes a legacy reference from DB without interpreting its identifier as a file path", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService(); const project = service.createProject({ name: "Legacy delete" });
    const [legacy] = service.addLegacyReferenceImages(project.id, [{ identifier: "../must-not-touch.txt", filename: "legacy.png" }]);
    const route = await import("./images/[id]/route");
    const response = await route.DELETE(new Request("http://x/image", { method: "DELETE" }), { params: Promise.resolve({ id: legacy.id }) });
    expect(response.status).toBe(200); expect(service.listReferenceImages(project.id)).toEqual([]);
  });

  it("restores a quarantined upload when DB deletion fails", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService(); const project = service.createProject({ name: "Delete rollback" });
    const uploads = await import("./projects/[id]/images/route"); const form = new FormData(); form.append("images", new File([PNG], "front.png", { type: "image/png" }));
    const [created] = await (await uploads.POST(new Request("http://x/images", { method: "POST", body: form }), { params: Promise.resolve({ id: project.id }) })).json();
    const target = join(process.env.PROMPT_BUILDER_UPLOAD_ROOT!, created.storagePath);
    const injector = new BetterSqlite3(process.env.PROMPT_BUILDER_DB_PATH!);
    injector.exec("CREATE TRIGGER fail_image_delete BEFORE DELETE ON reference_images BEGIN SELECT RAISE(ABORT, 'delete failure'); END"); injector.close();
    const route = await import("./images/[id]/route"); const response = await route.DELETE(new Request("http://x/image", { method: "DELETE" }), { params: Promise.resolve({ id: created.id }) });
    expect(response.status).toBe(500); expect(existsSync(target)).toBe(true); expect(service.getReferenceImage(created.id).id).toBe(created.id);
  });

  it("classifies images and extracts evidenced candidates through structured AI routes", async () => {
    process.env.OPENAI_COMPAT_API_KEY = "secret"; process.env.OPENAI_COMPAT_ALLOWED_ORIGINS = "https://ai.example";
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService(); service.updateAiSettings({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text" });
    const project = service.createProject({ name: "AI success" });
    service.upsertProductProfile(project.id, { facts: { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: ["black"], features: ["laces"], backViewEvidence: false } });
    const uploads = await import("./projects/[id]/images/route"); const form = new FormData(); form.append("images", new File([PNG], "front.png", { type: "image/png" }));
    const [image] = await (await uploads.POST(new Request("http://x/images", { method: "POST", body: form }), { params: Promise.resolve({ id: project.id }) })).json();
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ classifications: [{ imageId: image.id, classification: "front", confidence: 0.92 }] }) } }] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ candidates: [{ field: "material", value: "Leather", confidence: 0.88, evidenceImageIds: [image.id] }] }) } }] }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const classify = await import("./projects/[id]/images/classify/route");
    expect(await (await classify.POST(new Request("http://x/classify", { method: "POST" }), { params: Promise.resolve({ id: project.id }) })).json()).toMatchObject([{ aiClassification: "front", effectiveClassification: "front" }]);
    const extract = await import("./projects/[id]/product-profile/extract/route");
    expect(await (await extract.POST(new Request("http://x/extract", { method: "POST" }), { params: Promise.resolve({ id: project.id }) })).json()).toMatchObject({ aiCandidates: [{ field: "material", evidenceImageIds: [image.id] }] });
  });

  it("accepts facts-only profile writes and rejects read-only or unknown fields", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService(); const project = service.createProject({ name: "Profile round trip" });
    const facts = { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: [] as string[], features: [] as string[], backViewEvidence: false };
    service.upsertProductProfile(project.id, { facts });
    const [image] = service.addReferenceImages(project.id, [{ storagePath: `${project.id}/one.png`, originalFilename: "one.png", mimeType: "image/png", size: 8 }]);
    const route = await import("./projects/[id]/product-profile/route");
    const getResponse = await route.GET(new Request("http://x/profile"), { params: Promise.resolve({ id: project.id }) }); const profile = await getResponse.json();
    for (const forbidden of [
      { referenceImages: profile.referenceImages },
      { confirmedFacts: profile.confirmedFacts },
      { aiCandidates: profile.aiCandidates },
      { unknown: true },
    ]) {
      const rejected = await route.POST(new Request("http://x/profile", { method: "POST", body: JSON.stringify({ facts, ...forbidden }) }), { params: Promise.resolve({ id: project.id }) });
      expect(rejected.status).toBe(400); expect(await rejected.json()).toMatchObject({ error: { code: "INVALID_INPUT" } });
    }
    const postResponse = await route.POST(new Request("http://x/profile", { method: "POST", body: JSON.stringify({ facts: { ...facts, title: "Updated Shoe" } }) }), { params: Promise.resolve({ id: project.id }) });
    expect(postResponse.status).toBe(200); expect((await postResponse.json()).facts.title).toBe("Updated Shoe");
    expect(service.listReferenceImages(project.id).map(({ id }) => id)).toEqual([image.id]);
  });

  it("supports a fully manual path when AI is not configured", async () => {
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    const project = service.createProject({ name: "Manual" });
    const route = await import("./projects/[id]/images/classify/route");
    const response = await route.POST(new Request("http://x/classify", { method: "POST" }), { params: Promise.resolve({ id: project.id }) });
    expect(response.status).toBe(409); expect((await response.json()).error.code).toBe("AI_NOT_CONFIGURED");
  });

  it("never returns the API key from settings", async () => {
    process.env.OPENAI_COMPAT_API_KEY = "top-secret";
    const route = await import("./settings/ai/route");
    await route.PATCH(new Request("http://x/settings", { method: "PATCH", body: JSON.stringify({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text" }) }));
    const response = await route.GET(); const json = JSON.stringify(await response.json());
    expect(json).toContain('"configured":true'); expect(json).not.toContain("top-secret"); expect(json.toLowerCase()).not.toContain("apikey");
  });

  it("does not send the API key to a base URL absent from the server allowlist", async () => {
    process.env.OPENAI_COMPAT_API_KEY = "top-secret";
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    service.updateAiSettings({ baseUrl: "https://attacker.example/v1", visionModel: "vision", textModel: "text" });
    const project = service.createProject({ name: "SSRF" });
    const route = await import("./projects/[id]/images/classify/route");
    const response = await route.POST(new Request("http://x/classify", { method: "POST" }), { params: Promise.resolve({ id: project.id }) });
    expect(response.status).toBe(400); expect((await response.json()).error.code).toBe("AI_BASE_URL_NOT_ALLOWED"); expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects AI polish that removes immutable facts and preserves the deterministic prompt", async () => {
    process.env.OPENAI_COMPAT_API_KEY = "secret";
    process.env.OPENAI_COMPAT_ALLOWED_ORIGINS = "https://ai.example";
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    service.updateAiSettings({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text" });
    const project = service.createProject({ name: "Polish" });
    service.upsertProductProfile(project.id, { facts: { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: ["black"], features: ["laces"], backViewEvidence: false } });
    const design = service.createDesign(project.id, { name: "Hero", selectedModuleIds: ["negative_product_integrity"], parameters: {}, custom: {} });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ prompt: "A nice shoe" }) } }] }), { status: 200 })));
    const route = await import("./designs/[id]/polish/route");
    const before = service.compile(design.id).prompt;
    const response = await route.POST(new Request("http://x/polish", { method: "POST" }), { params: Promise.resolve({ id: design.id }) });
    expect(response.status).toBe(422); expect((await response.json()).error.code).toBe("AI_POLISH_UNSAFE");
    expect(service.compile(design.id).prompt).toBe(before); expect(service.getDesign(design.id).custom).toEqual({});
  });

  it("stores an accepted polish in an immutable version without replacing deterministic compile", async () => {
    process.env.OPENAI_COMPAT_API_KEY = "secret"; process.env.OPENAI_COMPAT_ALLOWED_ORIGINS = "https://ai.example";
    const service = (await import("@/server/persistence/runtime")).getPromptBuilderService();
    service.updateAiSettings({ baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text" });
    const project = service.createProject({ name: "Accepted polish" });
    service.upsertProductProfile(project.id, { facts: { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: ["black"], features: ["laces"], backViewEvidence: false } });
    const design = service.createDesign(project.id, { name: "Hero", selectedModuleIds: [], parameters: {}, custom: {} });
    const deterministic = service.compile(design.id).prompt; const polished = deterministic.replace("No additional direction selected.", "No additional direction, selected.");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ prompt: polished }) } }] }), { status: 200 })));
    const route = await import("./designs/[id]/polish/route");
    const response = await route.POST(new Request("http://x/polish", { method: "POST" }), { params: Promise.resolve({ id: design.id }) });
    expect(response.status).toBe(200);
    const [version] = service.listVersions(design.id);
    expect(version.compileSnapshot.prompt).toBe(deterministic);
    expect(version.polishedPrompt).toBe(polished);
    expect(version.polishedBasePromptHash).toMatch(/^[a-f0-9]{64}$/);
    expect(version.polishedPromptValid).toBe(true);
    expect(version.designSnapshot.polishedPrompt).toBe(polished);
    expect(version.designSnapshot.polishedBasePromptHash).toBe(version.polishedBasePromptHash);
    expect(version.designSnapshot.custom).toEqual({});
  });
});
