import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("API error boundary", () => {
  let directory: string;

  beforeEach(() => {
    directory = mkdtempSync(join(tmpdir(), "prompt-api-"));
    process.env.PROMPT_BUILDER_DB_PATH = join(directory, "app.db");
    vi.resetModules();
  });

  afterEach(async () => {
    const { closeRuntimeService } = await import("@/server/persistence/runtime");
    closeRuntimeService();
    delete process.env.PROMPT_BUILDER_DB_PATH;
    rmSync(directory, { recursive: true, force: true });
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
});
