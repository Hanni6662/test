import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    service.upsertProductProfile(project.id, { facts: { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: ["black"], features: ["laces"], backViewEvidence: false }, referenceImages: [] });
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
    service.upsertProductProfile(project.id, { facts: { title: "Shoe", category: "Shoes", audience: "Adults", material: "Leather", colors: ["black"], features: ["laces"], backViewEvidence: false }, referenceImages: [] });
    const design = service.createDesign(project.id, { name: "Hero", selectedModuleIds: [], parameters: {}, custom: {} });
    const deterministic = service.compile(design.id).prompt; const polished = `${deterministic}\n\nUse fluid commercial phrasing.`;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ prompt: polished }) } }] }), { status: 200 })));
    const route = await import("./designs/[id]/polish/route");
    const response = await route.POST(new Request("http://x/polish", { method: "POST" }), { params: Promise.resolve({ id: design.id }) });
    expect(response.status).toBe(200);
    const [version] = service.listVersions(design.id);
    expect(version.compileSnapshot.prompt).toBe(deterministic);
    expect(version.designSnapshot.custom.aiPolishedPrompt).toBe(polished);
  });
});
