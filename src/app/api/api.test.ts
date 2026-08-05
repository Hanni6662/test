import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import BetterSqlite3 from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("API error boundary", () => {
  let directory: string;
  let databasePath: string;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "prompt-api-"));
    databasePath = join(directory, "app.db");
    process.env.PROMPT_BUILDER_DB_PATH = databasePath;
    process.env.PROMPT_BUILDER_UPLOAD_ROOT = join(directory, "uploads");
    vi.resetModules();
  });

  afterEach(async () => {
    const { closeRuntimeService } = await import("@/server/persistence/runtime");
    closeRuntimeService();
    delete process.env.PROMPT_BUILDER_DB_PATH;
    delete process.env.PROMPT_BUILDER_UPLOAD_ROOT;
    rmSync(directory, { recursive: true, force: true });
  });

  it("serves an upload-backed image inline for workspace thumbnails", async () => {
    const { getPromptBuilderService } = await import("@/server/persistence/runtime");
    const service = getPromptBuilderService();
    const project = service.createProject({ name: "Thumbnail" });
    const relative = `${project.id}/thumb.png`;
    mkdirSync(join(directory, "uploads", project.id), { recursive: true });
    writeFileSync(join(directory, "uploads", relative), Buffer.from([137, 80, 78, 71]));
    const [image] = service.addReferenceImages(project.id, [{ storagePath: relative, originalFilename: "thumb.png", mimeType: "image/png", size: 4 }]);
    const { GET } = await import("./images/[id]/route");

    const response = await GET(new Request(`http://localhost/api/images/${image.id}`), { params: Promise.resolve({ id: image.id }) });

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(response.headers.get("content-disposition")).toContain("inline");
    expect(Buffer.from(await response.arrayBuffer())).toEqual(Buffer.from([137, 80, 78, 71]));
  });

  it("returns structured 400 JSON for invalid request input without a stack", async () => {
    const { POST } = await import("./projects/route");
    const response = await POST(new Request("http://localhost/api/projects", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "" }),
    }));
    const body = await response.json();

    expect(response.status).toBe(400);
    expect(body.error.code).toBe("INVALID_INPUT");
    expect(body.error.issues).toBeInstanceOf(Array);
    expect(JSON.stringify(body).toLowerCase()).not.toContain("stack");
  });

  it("returns a structured 404 for a missing resource", async () => {
    const { GET } = await import("./projects/[id]/route");
    const response = await GET(new Request("http://localhost/api/projects/missing"), {
      params: Promise.resolve({ id: "missing" }),
    });
    const body = await response.json();

    expect(response.status).toBe(404);
    expect(body).toEqual({ error: { code: "PROJECT_NOT_FOUND", message: "Project not found" } });
  });

  it("maps builtin mutation attempts to a structured 409", async () => {
    const { PATCH } = await import("./modules/route");
    const response = await PATCH(new Request("http://localhost/api/modules", {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        id: "lighting_soft_daylight",
        action: "update",
        patch: { content: "tampered" },
      }),
    }));
    const body = await response.json();

    expect(response.status).toBe(409);
    expect(body.error.code).toBe("BUILTIN_IMMUTABLE");
    expect(JSON.stringify(body).toLowerCase()).not.toContain("stack");
  });

  it("maps corrupted persisted JSON validation to 500 rather than request-input 400", async () => {
    const { getPromptBuilderService } = await import("@/server/persistence/runtime");
    const service = getPromptBuilderService();
    const project = service.createProject({ name: "Corrupted profile" });
    service.upsertProductProfile(project.id, {
      facts: {
        title: "Valid product",
        category: "Dresses",
        audience: "Children",
        material: "Cotton",
        colors: [],
        features: [],
        backViewEvidence: false,
      },
    });
    const corrupter = new BetterSqlite3(databasePath);
    corrupter.prepare("UPDATE product_profiles SET facts_json = ? WHERE project_id = ?")
      .run("{}", project.id);
    corrupter.close();
    const { GET } = await import("./projects/[id]/product-profile/route");

    const response = await GET(new Request(`http://localhost/api/projects/${project.id}/product-profile`), {
      params: Promise.resolve({ id: project.id }),
    });

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({
      error: { code: "INTERNAL_ERROR", message: "An unexpected error occurred" },
    });
  });

  it("does not misreport non-unique SQLite failures as module ID conflicts", async () => {
    const { getPromptBuilderService } = await import("@/server/persistence/runtime");
    getPromptBuilderService();
    const injector = new BetterSqlite3(databasePath);
    injector.exec(`CREATE TRIGGER fail_custom_module_insert
      BEFORE INSERT ON prompt_modules BEGIN SELECT missing_sqlite_function(); END`);
    injector.close();
    const { POST } = await import("./modules/route");

    const response = await POST(new Request("http://localhost/api/modules", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        action: "create",
        module: {
          id: "custom_database_error",
          label: "Database error",
          category: "lighting",
          content: "This insert is deliberately interrupted.",
          capabilities: [],
        },
      }),
    }));

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({
      error: { code: "INTERNAL_ERROR", message: "An unexpected error occurred" },
    });
  });

  it("rejects duplicate selected module IDs for both design create and patch", async () => {
    const duplicateIds = ["purpose_amazon_listing", "purpose_amazon_listing"];
    const { POST } = await import("./projects/[id]/designs/route");
    const createResponse = await POST(new Request("http://localhost/api/projects/missing/designs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "Duplicate IDs", selectedModuleIds: duplicateIds }),
    }), { params: Promise.resolve({ id: "missing" }) });
    const { PATCH } = await import("./designs/[id]/route");
    const patchResponse = await PATCH(new Request("http://localhost/api/designs/missing", {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ selectedModuleIds: duplicateIds }),
    }), { params: Promise.resolve({ id: "missing" }) });

    for (const response of [createResponse, patchResponse]) {
      expect(response.status).toBe(400);
      const body = await response.json();
      expect(body.error.code).toBe("INVALID_INPUT");
      expect(body.error.issues[0]?.message).toBe("Selected module IDs must be unique");
    }
  });
});
