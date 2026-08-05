import { api } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().listVersions((await context.params).id));
}

export async function POST(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().saveVersion((await context.params).id), 201);
}

