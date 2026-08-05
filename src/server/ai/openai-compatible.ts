import { z } from "zod";

export const imageClassificationSchema = z.enum(["main", "front", "back", "side", "detail", "variant", "unknown"]);
const classificationResultSchema = z.object({
  classifications: z.array(z.object({ imageId: z.string(), classification: imageClassificationSchema, confidence: z.number().min(0).max(1) })),
});
const candidateBase = { confidence: z.number().min(0).max(1), evidenceImageIds: z.array(z.string()).min(1) };
export const productFactCandidateSchema = z.discriminatedUnion("field", [
  z.object({ field: z.literal("title"), value: z.string().trim().min(1).max(200), ...candidateBase }),
  z.object({ field: z.literal("category"), value: z.string().trim().min(1).max(120), ...candidateBase }),
  z.object({ field: z.literal("audience"), value: z.string().trim().min(1).max(120), ...candidateBase }),
  z.object({ field: z.literal("material"), value: z.string().trim().min(1).max(240), ...candidateBase }),
  z.object({ field: z.literal("colors"), value: z.array(z.string().trim().min(1).max(80)).max(20), ...candidateBase }),
  z.object({ field: z.literal("features"), value: z.array(z.string().trim().min(1).max(240)).max(50), ...candidateBase }),
  z.object({ field: z.literal("backViewEvidence"), value: z.boolean(), ...candidateBase }),
]);
/* Candidate values are field-aware so bad AI output is rejected before persistence. */
const candidateSchema = productFactCandidateSchema;
const extractionSchema = z.object({ candidates: z.array(candidateSchema) });
const polishSchema = z.object({ prompt: z.string().min(1) });
const responseSchema = z.object({ choices: z.array(z.object({ message: z.object({ content: z.string() }) })).min(1) });

export type ImageClassification = z.infer<typeof imageClassificationSchema>;
export type ProductFactCandidate = z.infer<typeof candidateSchema> & { id?: string };

export interface AiSettings { baseUrl: string; visionModel: string; textModel: string; timeoutMs?: number }
export interface AiImage { id: string; dataUrl: string }

export class AiGatewayError extends Error {
  constructor(public readonly code: string, public readonly status: number, message: string) {
    super(message); this.name = "AiGatewayError";
  }
}

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;
const MAX_AI_RESPONSE_BYTES = 2 * 1024 * 1024;

async function readBoundedJson(response: Response, signal: AbortSignal): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared && /^\d+$/.test(declared) && Number(declared) > MAX_AI_RESPONSE_BYTES) {
    void response.body?.cancel().catch(() => undefined);
    throw new AiGatewayError("AI_RESPONSE_TOO_LARGE", 502, "AI provider response exceeded the size limit");
  }
  if (!response.body) throw new AiGatewayError("AI_INVALID_JSON", 502, "AI provider returned invalid JSON");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      if (signal.aborted) {
        void reader.cancel().catch(() => undefined);
        throw new DOMException("aborted", "AbortError");
      }
      const read = new Promise<ReadableStreamReadResult<Uint8Array>>((resolveRead, rejectRead) => {
        const abort = () => rejectRead(new DOMException("aborted", "AbortError"));
        signal.addEventListener("abort", abort, { once: true });
        reader.read().then(resolveRead, rejectRead).finally(() => signal.removeEventListener("abort", abort));
      });
      const { done, value } = await read;
      if (done) break;
      total += value.byteLength;
      if (total > MAX_AI_RESPONSE_BYTES) {
        void reader.cancel().catch(() => undefined);
        throw new AiGatewayError("AI_RESPONSE_TOO_LARGE", 502, "AI provider response exceeded the size limit");
      }
      chunks.push(value);
    }
  } catch (error) {
    void reader.cancel().catch(() => undefined);
    throw error;
  }
  try { return JSON.parse(Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), total).toString("utf8")); }
  catch { throw new AiGatewayError("AI_INVALID_JSON", 502, "AI provider returned invalid JSON"); }
}

export class OpenAiCompatibleProvider {
  constructor(private readonly settings: AiSettings, private readonly apiKey: string, private readonly fetcher: Fetcher = fetch) {}

  async classifyImages(images: AiImage[]) {
    const content = [{ type: "text", text: "Classify each product image. Return JSON only." }, ...images.flatMap((image) => [
      { type: "text", text: `Image ID: ${image.id}` }, { type: "image_url", image_url: { url: image.dataUrl } },
    ])];
    return (await this.complete(this.settings.visionModel, content, classificationResultSchema)).classifications;
  }

  async extractProductFacts(images: AiImage[]): Promise<ProductFactCandidate[]> {
    const content = [{ type: "text", text: "Extract only evidenced product facts as candidates. Return JSON only." }, ...images.flatMap((image) => [
      { type: "text", text: `Image ID: ${image.id}` }, { type: "image_url", image_url: { url: image.dataUrl } },
    ])];
    return (await this.complete(this.settings.visionModel, content, extractionSchema)).candidates;
  }

  async polishPrompt(prompt: string): Promise<string> {
    return (await this.complete(this.settings.textModel, `Only adjust whitespace, punctuation, and optional articles (a, an, the, that, which). Preserve all sections, words, numbers, operators, facts, and constraints in order. Return JSON {"prompt":"..."}.\n${prompt}`, polishSchema)).prompt;
  }

  private async complete<T>(model: string, content: unknown, schema: z.ZodType<T>): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.settings.timeoutMs ?? 30_000);
    try {
      const response = await this.fetcher(`${this.settings.baseUrl.replace(/\/$/, "")}/chat/completions`, {
        method: "POST", signal: controller.signal, redirect: "manual",
        headers: { "content-type": "application/json", Authorization: `Bearer ${this.apiKey}` },
        body: JSON.stringify({ model, messages: [{ role: "user", content }], response_format: { type: "json_object" } }),
      });
      if (response.status >= 300 && response.status < 400) throw new AiGatewayError("AI_REDIRECT_REJECTED", 502, "AI provider redirects are not allowed");
      if (response.status === 401 || response.status === 403) throw new AiGatewayError("AI_AUTH_FAILED", 502, "AI provider authentication failed");
      if (!response.ok) throw new AiGatewayError("AI_PROVIDER_ERROR", 502, `AI provider returned HTTP ${response.status}`);
      const parsedEnvelope = responseSchema.safeParse(await readBoundedJson(response, controller.signal));
      if (!parsedEnvelope.success) throw new AiGatewayError("AI_INCOMPATIBLE_RESPONSE", 502, "AI provider response structure is incompatible");
      let structured: unknown;
      try { structured = JSON.parse(parsedEnvelope.data.choices[0].message.content); } catch { throw new AiGatewayError("AI_INVALID_JSON", 502, "AI provider content is not valid JSON"); }
      const parsed = schema.safeParse(structured);
      if (!parsed.success) throw new AiGatewayError("AI_INCOMPATIBLE_RESPONSE", 502, "AI provider structured output is incompatible");
      return parsed.data;
    } catch (error) {
      if (error instanceof AiGatewayError) throw error;
      if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) throw new AiGatewayError("AI_TIMEOUT", 504, "AI request timed out");
      throw new AiGatewayError("AI_UNAVAILABLE", 502, "AI provider is unavailable");
    } finally { clearTimeout(timer); }
  }
}
