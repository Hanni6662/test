import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
import { ServiceError } from "@/server/persistence/service";
import { deleteUploadedFile, storeUploadedFiles } from "@/server/uploads";
import { uploadRoot } from "@/server/ai/runtime";

type Context = { params: Promise<{ id: string }> };
export async function GET(_request: Request, context: Context) { return api(async () => getPromptBuilderService().listReferenceImages((await context.params).id)); }
export async function POST(request: Request, context: Context) {
  return api(async () => {
    const projectId = (await context.params).id;
    getPromptBuilderService().getProject(projectId);
    let form: FormData;
    try { form = await request.formData(); }
    catch { throw new ServiceError("INVALID_MULTIPART", 400, "Request body must be valid multipart form data"); }
    const files = form.getAll("images").filter((part): part is File => part instanceof File);
    const stored = await storeUploadedFiles(uploadRoot(), projectId, files);
    try { return getPromptBuilderService().addReferenceImages(projectId, stored); }
    catch (error) { await Promise.all(stored.map((image) => deleteUploadedFile(uploadRoot(), projectId, image.storagePath))); throw error; }
  }, 201);
}
