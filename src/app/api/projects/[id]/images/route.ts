import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { deleteUploadedFile } from "@/server/uploads";
import { parseMultipartImages } from "@/server/streaming-upload";
import { uploadRoot } from "@/server/ai/runtime";

type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) { return api(async () => getPromptBuilderService().listReferenceImages((await context.params).id)); }
export async function POST(request: Request, context: Context) {
  return api(async () => {
    const projectId = (await context.params).id;
    getPromptBuilderService().getProject(projectId);
    const stored = await parseMultipartImages(request, uploadRoot(), projectId);
    try { return getPromptBuilderService().addReferenceImages(projectId, stored); }
    catch (error) { await Promise.all(stored.map((image) => deleteUploadedFile(uploadRoot(), projectId, image.storagePath))); throw error; }
  }, 201);
}
