import { configuredProvider } from "@/server/ai/runtime";
import { validatePolishedPrompt } from "@/server/ai/polish";
import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
type Context = { params: Promise<{ id: string }> };
export async function POST(_request: Request, context: Context) { return api(async () => {
  const service = getPromptBuilderService(); const designId = (await context.params).id;
  const compiled = service.compile(designId); const provider = configuredProvider(service.getAiSettings());
  const polishedPrompt = validatePolishedPrompt(compiled, await provider.polishPrompt(compiled.prompt));
  const design = service.getDesign(designId);
  service.autosaveDesign(designId, { custom: { ...design.custom, aiPolishedPrompt: polishedPrompt } });
  const version = service.saveVersion(designId);
  return { compiledEnglish: compiled.prompt, polishedPrompt, versionId: version.id, versionNumber: version.versionNumber, warnings: compiled.warnings, errors: compiled.errors };
}); }
