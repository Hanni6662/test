import { z } from "zod";
import { imageClassificationSchema } from "@/server/ai/openai-compatible";
import { uploadRoot } from "@/server/ai/runtime";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { deleteReferenceImageConsistently } from "@/server/image-deletion";
import { readFile } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { ServiceError } from "@/server/persistence/service";
type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) { try {
  const image = getPromptBuilderService().getReferenceImage((await context.params).id);
  if (!image.uploadBacked || !image.storagePath || !image.mimeType) throw new ServiceError("IMAGE_FILE_NOT_FOUND", 404, "Uploaded image file not found");
  const projectRoot = resolve(uploadRoot(), image.projectId); const target = resolve(uploadRoot(), image.storagePath); const fromProject = relative(projectRoot, target);
  if (fromProject.startsWith(`..${sep}`) || fromProject === ".." || isAbsolute(fromProject)) throw new ServiceError("UNSAFE_UPLOAD_PATH", 400, "Stored image path is unsafe");
  return new Response(await readFile(target), { headers: { "content-type": image.mimeType, "content-disposition": `inline; filename="${encodeURIComponent(image.filename)}"`, "cache-control": "private, max-age=3600" } });
} catch (error) { return api(() => { throw error; }); } }
export async function PATCH(request: Request, context: Context) { return api(async () => getPromptBuilderService().updateReferenceImage((await context.params).id, await parseBody(request, z.object({ userClassification: imageClassificationSchema.nullable() })))); }
export async function DELETE(_request: Request, context: Context) { return api(async () => {
  return deleteReferenceImageConsistently(getPromptBuilderService(), (await context.params).id, uploadRoot());
}); }
