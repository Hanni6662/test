import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string; versionId: string }> };

export async function POST(_request: Request, context: Context) {
  return api(async () => {
    const { id, versionId } = await context.params;
    return getPromptBuilderService().restoreVersion(id, versionId);
  }, 201);
}

