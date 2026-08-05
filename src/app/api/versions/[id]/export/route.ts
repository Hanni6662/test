import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };

export async function GET(_request: Request, context: Context) {
  const { id } = await context.params;
  const response = await api(() => getPromptBuilderService().exportVersion(id));
  if (response.ok) {
    response.headers.set("content-disposition", `attachment; filename="prompt-version-${id}.json"`);
  }
  return response;
}
