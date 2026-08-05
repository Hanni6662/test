import { afterEach, describe, expect, it, vi } from "vitest";
import { AiGatewayError, OpenAiCompatibleProvider } from "./openai-compatible";

const settings = { baseUrl: "https://ai.example/v1", visionModel: "vision", textModel: "text", timeoutMs: 20 };

describe("OpenAI-compatible provider", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses structured image classifications", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ classifications: [{ imageId: "img-1", classification: "front", confidence: 0.91 }] }) } }] }), { status: 200 }));
    const provider = new OpenAiCompatibleProvider(settings, "secret", fetcher);
    await expect(provider.classifyImages([{ id: "img-1", dataUrl: "data:image/png;base64,eA==" }])).resolves.toEqual([
      { imageId: "img-1", classification: "front", confidence: 0.91 },
    ]);
    expect(fetcher).toHaveBeenCalledWith("https://ai.example/v1/chat/completions", expect.objectContaining({ headers: expect.objectContaining({ Authorization: "Bearer secret" }) }));
    const requestBody = JSON.parse(fetcher.mock.calls[0][1].body as string);
    expect(requestBody.messages[0].content).toContainEqual({ type: "text", text: "Image ID: img-1" });
    expect(requestBody.messages[0].content.some((part: Record<string, unknown>) => "imageId" in part)).toBe(false);
  });

  it.each([[401, "AI_AUTH_FAILED"], [403, "AI_AUTH_FAILED"]])("maps %i authentication failures", async (status, code) => {
    const provider = new OpenAiCompatibleProvider(settings, "bad", vi.fn().mockResolvedValue(new Response("no", { status })));
    await expect(provider.classifyImages([])).rejects.toMatchObject({ code });
  });

  it("maps timeout, invalid JSON, and incompatible response structures", async () => {
    const timeoutFetch = vi.fn((_url: string | URL | Request, init?: RequestInit) => new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))));
    await expect(new OpenAiCompatibleProvider(settings, "k", timeoutFetch).classifyImages([]))
      .rejects.toMatchObject({ code: "AI_TIMEOUT" });
    const invalid = vi.fn().mockResolvedValue(new Response("not json", { status: 200 }));
    await expect(new OpenAiCompatibleProvider(settings, "k", invalid).classifyImages([]))
      .rejects.toMatchObject({ code: "AI_INVALID_JSON" });
    const incompatible = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ choices: [] }), { status: 200 }));
    await expect(new OpenAiCompatibleProvider(settings, "k", incompatible).classifyImages([]))
      .rejects.toBeInstanceOf(AiGatewayError);
    await expect(new OpenAiCompatibleProvider(settings, "k", incompatible).classifyImages([]))
      .rejects.toMatchObject({ code: "AI_INCOMPATIBLE_RESPONSE" });
  });

  it("rejects field-incompatible candidate values as an incompatible AI response", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ candidates: [{ field: "colors", value: "red", confidence: 0.8, evidenceImageIds: ["img-1"] }] }) } }] }), { status: 200 }));
    await expect(new OpenAiCompatibleProvider(settings, "k", fetcher).extractProductFacts([]))
      .rejects.toMatchObject({ code: "AI_INCOMPATIBLE_RESPONSE" });
  });

  it("keeps the timeout active while reading the response body", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({ pull() { return new Promise(() => undefined); }, cancel() { cancelled = true; } });
    const provider = new OpenAiCompatibleProvider({ ...settings, timeoutMs: 10 }, "k", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    await expect(provider.classifyImages([])).rejects.toMatchObject({ code: "AI_TIMEOUT" });
    expect(cancelled).toBe(true);
  });

  it("does not hang if a noncompliant fetcher returns headers after the signal already aborted", async () => {
    const body = new ReadableStream<Uint8Array>({ pull() { return new Promise(() => undefined); } });
    const fetcher = vi.fn(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); return new Response(body, { status: 200 }); });
    await expect(new OpenAiCompatibleProvider({ ...settings, timeoutMs: 5 }, "k", fetcher).classifyImages([]))
      .rejects.toMatchObject({ code: "AI_TIMEOUT" });
  }, 200);

  it("cancels and rejects response bodies larger than the hard limit", async () => {
    let cancelled = false;
    const chunk = new Uint8Array(2 * 1024 * 1024 + 1);
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(chunk); }, cancel() { cancelled = true; } });
    const provider = new OpenAiCompatibleProvider(settings, "k", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    await expect(provider.classifyImages([])).rejects.toMatchObject({ code: "AI_RESPONSE_TOO_LARGE" });
    expect(cancelled).toBe(true);
  });

  it("uses manual redirects and rejects every 3xx without following it", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 302, headers: { location: "https://attacker.example" } }));
    const provider = new OpenAiCompatibleProvider(settings, "k", fetcher);
    await expect(provider.classifyImages([])).rejects.toMatchObject({ code: "AI_REDIRECT_REJECTED" });
    expect(fetcher).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ redirect: "manual" }));
  });
});
