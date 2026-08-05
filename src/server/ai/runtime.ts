import { readFile, stat } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { ServiceError, type PersistedReferenceImage } from "@/server/persistence/service";
import { OpenAiCompatibleProvider } from "./openai-compatible";

export function uploadRoot(): string { return process.env.PROMPT_BUILDER_UPLOAD_ROOT ?? resolve(process.cwd(), "data", "uploads"); }
export const AI_MAX_IMAGE_COUNT = 6;
export const AI_MAX_TOTAL_BYTES = 24 * 1024 * 1024;

export function configuredProvider(settings: { baseUrl: string; visionModel: string; textModel: string }) {
  const key = process.env.OPENAI_COMPAT_API_KEY;
  if (!key || !settings.baseUrl || !settings.visionModel || !settings.textModel) throw new ServiceError("AI_NOT_CONFIGURED", 409, "AI is not configured; continue with manual review");
  let origin: string; let allowed: Set<string>;
  try {
    origin = new URL(settings.baseUrl).origin;
    allowed = new Set((process.env.OPENAI_COMPAT_ALLOWED_ORIGINS ?? "https://api.openai.com").split(",").map((value) => value.trim()).filter(Boolean).map((value) => new URL(value).origin));
  } catch { throw new ServiceError("AI_CONFIG_ERROR", 500, "AI origin configuration is invalid"); }
  if (!allowed.has(origin)) throw new ServiceError("AI_BASE_URL_NOT_ALLOWED", 400, "AI base URL origin is not allowed by the server");
  return new OpenAiCompatibleProvider(settings, key);
}

export interface AiImageReadOptions {
  statFile?: (path: string) => Promise<{ size: number }>;
  readFile?: (path: string) => Promise<Buffer>;
}

export async function imagesAsDataUrls(projectId: string, images: PersistedReferenceImage[], options: AiImageReadOptions = {}) {
  const projectRoot = resolve(uploadRoot(), projectId);
  const selected = images.filter((image) => image.uploadBacked && image.storagePath !== null && image.mimeType !== null);
  if (selected.length > AI_MAX_IMAGE_COUNT) throw new ServiceError("AI_INPUT_TOO_LARGE", 413, "AI analysis supports at most 6 uploaded images");
  const inputs: Array<{ image: PersistedReferenceImage & { storagePath: string; mimeType: NonNullable<PersistedReferenceImage["mimeType"]> }; target: string }> = [];
  let totalBytes = 0;
  for (const image of selected) {
    const target = resolve(uploadRoot(), image.storagePath!);
    const fromProject = relative(projectRoot, target);
    if (fromProject.startsWith(`..${sep}`) || fromProject === ".." || isAbsolute(fromProject)) throw new ServiceError("UNSAFE_UPLOAD_PATH", 400, "Stored image path is unsafe");
    totalBytes += (await (options.statFile ?? stat)(target)).size;
    if (totalBytes > AI_MAX_TOTAL_BYTES) throw new ServiceError("AI_INPUT_TOO_LARGE", 413, "Uploaded images exceed the 24 MB AI analysis budget");
    inputs.push({ image: image as typeof inputs[number]["image"], target });
  }
  const result: Array<{ id: string; dataUrl: string }> = [];
  let actualBytes = 0;
  for (const { image, target } of inputs) {
    const bytes = await (options.readFile ?? readFile)(target);
    actualBytes += bytes.byteLength;
    if (actualBytes > AI_MAX_TOTAL_BYTES) throw new ServiceError("AI_INPUT_TOO_LARGE", 413, "Uploaded images exceed the 24 MB AI analysis budget");
    result.push({ id: image.id, dataUrl: `data:${image.mimeType};base64,${bytes.toString("base64")}` });
  }
  return result;
}
