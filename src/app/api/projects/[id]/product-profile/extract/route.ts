import { configuredProvider, imagesAsDataUrls } from "@/server/ai/runtime";
import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
type Context = { params: Promise<{ id: string }> };
export async function POST(_request: Request, context: Context) { return api(async () => {
  const service = getPromptBuilderService(); const projectId = (await context.params).id;
  const images = service.listReferenceImages(projectId); const provider = configuredProvider(service.getAiSettings());
  return { aiCandidates: service.replaceAiCandidates(projectId, await provider.extractProductFacts(await imagesAsDataUrls(projectId, images))) };
}); }
