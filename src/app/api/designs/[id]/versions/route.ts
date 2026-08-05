import { z } from "zod";
import { api, parseBody } from "@/server/http";
import { getPromptBuilderService } from "@/server/persistence/runtime";

type Context = { params: Promise<{ id: string }> };

export async function GET(_request: Request, context: Context) {
  return api(async () => getPromptBuilderService().listVersions((await context.params).id));
}

export async function POST(request: Request, context: Context) {
  return api(async () => {
    const { expectedRevision } = await parseBody(request, z.object({ expectedRevision: z.number().int().nonnegative() }).strict());
    return getPromptBuilderService().saveVersion((await context.params).id, expectedRevision);
  }, 201);
}
