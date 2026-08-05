import { readFile } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { ServiceError, type PersistedReferenceImage } from "@/server/persistence/service";
import { OpenAiCompatibleProvider } from "./openai-compatible";

export function uploadRoot(): string { return process.env.PROMPT_BUILDER_UPLOAD_ROOT ?? resolve(process.cwd(), "data", "uploads"); }

export function configuredProvider(settings: { baseUrl: string; visionModel: string; textModel: string }) {
  const key = process.env.OPENAI_COMPAT_API_KEY;
  if (!key || !settings.baseUrl || !settings.visionModel || !settings.textModel) throw new ServiceError("AI_NOT_CONFIGURED", 409, "AI is not configured; continue with manual review");
  let origin: string;
  try { origin = new URL(settings.baseUrl).origin; } catch { throw new ServiceError("AI_BASE_URL_NOT_ALLOWED", 400, "AI base URL is invalid"); }
  const allowed = new Set((process.env.OPENAI_COMPAT_ALLOWED_ORIGINS ?? "https://api.openai.com").split(",").map((value) => value.trim()).filter(Boolean).map((value) => new URL(value).origin));
  if (!allowed.has(origin)) throw new ServiceError("AI_BASE_URL_NOT_ALLOWED", 400, "AI base URL origin is not allowed by the server");
  return new OpenAiCompatibleProvider(settings, key);
}

export async function imagesAsDataUrls(projectId: string, images: PersistedReferenceImage[]) {
  const projectRoot = resolve(uploadRoot(), projectId);
  return Promise.all(images.map(async (image) => {
    const target = resolve(uploadRoot(), image.storagePath);
    const fromProject = relative(projectRoot, target);
    if (fromProject.startsWith(`..${sep}`) || fromProject === ".." || isAbsolute(fromProject)) throw new ServiceError("UNSAFE_UPLOAD_PATH", 400, "Stored image path is unsafe");
    return { id: image.id, dataUrl: `data:${image.mimeType};base64,${(await readFile(target)).toString("base64")}` };
  }));
}
