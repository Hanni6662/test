import { configuredProvider } from "@/server/ai/runtime";
import { validatePolishedPrompt } from "@/server/ai/polish";
import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";
type Context = { params: Promise<{ id: string }> };
export async function POST(_request: Request, context: Context) { return api(async () => {
  const service = getPromptBuilderService(); const designId = (await context.params).id;
  const initial = service.compileEnvelope(designId); const compiled = initial.compile; const provider = configuredProvider(service.getAiSettings());
  const polishedPrompt = validatePolishedPrompt(compiled, await provider.polishPrompt(compiled.prompt));
  const saved = service.savePolishedVersion(designId, polishedPrompt, initial.revision, compiled.prompt);
  return { ...saved, compiledEnglish: saved.compile.prompt, polishedPrompt, versionId: saved.version.id, versionNumber: saved.version.versionNumber, warnings: saved.compile.warnings, errors: saved.compile.errors };
}); }
