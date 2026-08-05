import { describe, expect, it } from "vitest";
import { ApiClient, ApiError } from "./api-client";

describe("ApiClient", () => {
  it("turns structured server failures into ApiError values", async () => {
    const client = new ApiClient(async () => new Response(JSON.stringify({
      error: { code: "INVALID_INPUT", message: "Request validation failed", issues: [{ path: ["name"], message: "Required" }] },
    }), { status: 400, headers: { "content-type": "application/json" } }));

    await expect(client.get("/api/projects")).rejects.toEqual(expect.objectContaining({
      name: "ApiError",
      code: "INVALID_INPUT",
      status: 400,
      issues: [{ path: ["name"], message: "Required" }],
    } satisfies Partial<ApiError>));
  });

  it("uses a friendly fallback when a response is not JSON", async () => {
    const client = new ApiClient(async () => new Response("gateway down", { status: 502 }));
    await expect(client.get("/api/projects")).rejects.toMatchObject({ code: "HTTP_502", status: 502 });
  });
});
