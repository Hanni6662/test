import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };

export async function POST(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().compile((await context.params).id));
}

