import { z } from "zod";
import { imageClassificationSchema } from "@/server/ai/openai-compatible";
import { uploadRoot } from "@/server/ai/runtime";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { deleteUploadedFile } from "@/server/uploads";
type Context = { params: Promise<{ id: string }> };
export async function PATCH(request: Request, context: Context) { return api(async () => getPromptBuilderService().updateReferenceImage((await context.params).id, await parseBody(request, z.object({ userClassification: imageClassificationSchema.nullable() })))); }
export async function DELETE(_request: Request, context: Context) { return api(async () => {
  const service = getPromptBuilderService(); const id = (await context.params).id;
  const image = service.getReferenceImage(id);
  await deleteUploadedFile(uploadRoot(), image.projectId, image.storagePath);
  service.deleteReferenceImage(id);
  return { deleted: true };
}); }
