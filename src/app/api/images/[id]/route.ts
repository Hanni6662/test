import { z } from "zod";
import { imageClassificationSchema } from "@/server/ai/openai-compatible";
import { uploadRoot } from "@/server/ai/runtime";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { deleteReferenceImageConsistently } from "@/server/image-deletion";
type Context = { params: Promise<{ id: string }> };
export async function PATCH(request: Request, context: Context) { return api(async () => getPromptBuilderService().updateReferenceImage((await context.params).id, await parseBody(request, z.object({ userClassification: imageClassificationSchema.nullable() })))); }
export async function DELETE(_request: Request, context: Context) { return api(async () => {
  return deleteReferenceImageConsistently(getPromptBuilderService(), (await context.params).id, uploadRoot());
}); }
